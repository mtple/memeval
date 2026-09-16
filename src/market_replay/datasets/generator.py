"""Deterministic generated-fixture packs.

Everything here is artificial and labeled as such. Randomness is derived from stable
keys (seed, pool, block, index) so adding an event never changes another event.
Scenarios differ in activity, direction regimes, sparsity/observation dropouts and
liquidity changes. They are not designed so that any particular strategy wins.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..domain.models import Rights, Universe
from ..domain.status import AvailabilityBasis, DataOrigin, PoolModel, TokenBehavior, UseStatus
from ..venues.cpmm.math import get_amount_out
from .builder import WEEK_MS, build_pack, make_period
from .execution_params import ExecutionParams, fixture_default_params
from .pack import Pack

FIXTURE_CHAIN_ID = 0
FIXTURE_CHAIN = "fixture"
CASH_KEY = f"{FIXTURE_CHAIN_ID}:cash"
CASH_DECIMALS = 6
TOKEN_DECIMALS = 18
# Fixed artificial calendar anchor for generated packs: Monday 2026-01-05 00:00 UTC.
FIXTURE_EPOCH_MONDAY_MS = 1767571200000


def _u(seed: str, *parts: Any) -> float:
    """Uniform [0,1) from a stable key."""
    h = hashlib.blake2b("|".join([seed, *map(str, parts)]).encode(), digest_size=8).digest()
    return int.from_bytes(h, "big") / float(1 << 64)


def _lognormal_int(u1: float, u2: float, median: int, sigma: float) -> int:
    # Box-Muller from two uniforms; used only to shape fixture flow sizes.
    u1 = min(max(u1, 1e-12), 1 - 1e-12)
    z = math.sqrt(-2.0 * math.log(u1)) * math.cos(2 * math.pi * u2)
    return max(1, int(median * math.exp(sigma * z)))


@dataclass(slots=True)
class PoolSpec:
    index: int
    cash_reserve: int  # raw CASH
    price_cash_per_token: float  # initial
    activity: float  # swaps per block probability
    created_offset_ms: int  # relative to episode start (negative = prehistory)
    regimes: list[tuple[int, float]]  # (start_ms, buy_probability)
    events: list[dict[str, Any]] = field(default_factory=list)  # scripted liquidity/restriction events
    model: PoolModel = PoolModel.FIXTURE_CPMM
    supported: bool = True
    unsupported_reason: str | None = None


@dataclass(slots=True)
class GeneratorConfig:
    name: str
    seed: str
    duration_ms: int = WEEK_MS
    prehistory_ms: int = 86_400_000
    scenario: str = "trending"
    n_pools: int = 8
    week_index: int = 0  # shifts the artificial calendar week
    params: ExecutionParams = field(default_factory=fixture_default_params)
    dropout_windows: list[tuple[int, int]] = field(default_factory=list)  # relative ms windows where publications are dropped
    sync_every: int = 50
    scaled_activity: float = 1.0


SCENARIOS = {
    "trending": "Sustained directional flow with two regime changes across the week.",
    "reversal": "Strong early appreciation followed by a sharp reversal and slow recovery.",
    "sparse_missing": "Thin activity, pools that rarely trade, and windows of unpublished observations (coverage gaps).",
    "liquidity_shift": "Liquidity additions/removals, one drained-and-halted pool, one token that becomes sell-blocked, and an unsupported venue present.",
    "dev_short": "Two-hour development fixture with mixed conditions.",
}


def _pool_specs(cfg: GeneratorConfig) -> list[PoolSpec]:
    specs: list[PoolSpec] = []
    dur = cfg.duration_ms
    for i in range(cfg.n_pools):
        u = _u(cfg.seed, "pool", i)
        cash_reserve = int((500 + 19_500 * u) * 10**CASH_DECIMALS)
        price = 10 ** (-6 + 5 * _u(cfg.seed, "price", i))  # 1e-6 .. 1e-1 CASH per token
        activity = (0.01 + 0.06 * _u(cfg.seed, "act", i)) * cfg.scaled_activity
        created = -cfg.prehistory_ms
        if i >= max(2, cfg.n_pools - 2):
            created = int(dur * (0.15 + 0.6 * _u(cfg.seed, "created", i)))  # new listings during the week
        regimes: list[tuple[int, float]]
        if cfg.scenario == "trending":
            base = 0.45 + 0.15 * _u(cfg.seed, "bias", i)
            regimes = [(0, base), (dur // 3, 1 - base if i % 3 == 0 else base), (2 * dur // 3, 0.5)]
        elif cfg.scenario == "reversal":
            regimes = [(0, 0.66), (dur // 4, 0.30), (dur // 2, 0.52), (3 * dur // 4, 0.5)]
        elif cfg.scenario == "sparse_missing":
            activity *= 0.25
            regimes = [(0, 0.5 + (0.1 if i % 2 else -0.1)), (dur // 2, 0.5)]
        elif cfg.scenario == "liquidity_shift":
            regimes = [(0, 0.55), (dur // 2, 0.45)]
        else:
            regimes = [(0, 0.5 + 0.2 * (_u(cfg.seed, "rb", i) - 0.5))]
        specs.append(PoolSpec(index=i, cash_reserve=cash_reserve, price_cash_per_token=price, activity=activity, created_offset_ms=created, regimes=regimes))
    if cfg.scenario == "liquidity_shift" and cfg.n_pools >= 4:
        # pool 0: add liquidity at 20%, remove 40% at 60%
        specs[0].events = [
            {"kind": "mint", "at": dur // 5, "fraction": 0.5},
            {"kind": "burn", "at": (dur * 3) // 5, "fraction": 0.4},
        ]
        # pool 1: drained (burn 95%) then halted
        specs[1].events = [{"kind": "burn", "at": dur // 2, "fraction": 0.95}, {"kind": "halt", "at": dur // 2 + 60_000}]
        # pool 2: token becomes sell-blocked (fixture restriction) at 45%
        specs[2].events = [{"kind": "restriction", "at": (dur * 45) // 100, "payload": {"sell_blocked": True}}]
        # pool 3: unsupported venue (v3-like), present in the universe but not executable
        specs[3].model = PoolModel.UNISWAP_V3
        specs[3].supported = False
        specs[3].unsupported_reason = "concentrated liquidity is not a plain constant-product pool"
    if cfg.scenario == "dev_short" and cfg.n_pools >= 3:
        specs[2].events = [{"kind": "restriction", "at": (dur * 70) // 100, "payload": {"sell_blocked": True}}]
    return specs


def generate_pack(cfg: GeneratorConfig, out_dir: Path) -> Pack:
    p = cfg.params
    assert p.block_interval_ms, "fixture generator needs a fixed block interval"
    start_utc = FIXTURE_EPOCH_MONDAY_MS + cfg.week_index * WEEK_MS
    end_utc = start_utc + cfg.duration_ms
    pre_start = start_utc - cfg.prehistory_ms
    interval = p.block_interval_ms
    first_block = 1_000_000
    origin_ms = -cfg.prehistory_ms
    n_blocks = (cfg.duration_ms + cfg.prehistory_ms) // interval + p.settlement_tail_blocks + 2
    delay = p.availability_delay_ms
    discovery_delay = 5 * interval

    specs = _pool_specs(cfg)
    assets: list[dict[str, Any]] = [
        {
            "key": CASH_KEY,
            "chain_id": FIXTURE_CHAIN_ID,
            "address": "cash",
            "decimals": CASH_DECIMALS,
            "symbol": "CASH",
            "name": "Fixture settlement asset",
            "created_block": first_block,
            "created_time_utc_ms": pre_start,
            "discovery_available_utc_ms": pre_start,
            "is_numeraire": True,
            "fixture_rules": {},
        }
    ]
    pools: list[dict[str, Any]] = []
    state: dict[int, list[int]] = {}  # index -> [reserve_cash, reserve_token]
    live_from: dict[int, int] = {}
    tape: list[dict[str, Any]] = []
    seq = 0
    coverage_intervals: list[dict[str, Any]] = []
    inventory: dict[str, Any] = {"unsupported": [], "missing": [], "candidate_count": len(specs), "selected_count": 0}
    # Deliberate symbol collisions (MOON at index 0 and 3) prove that symbols are metadata, not identity.
    token_symbols = ["MOON", "PEPE", "DOGE", "MOON", "WIF", "BRETT", "TOSHI", "FROG", "KEK", "NORMIE", "BASED", "DEGEN"]

    def rel_to_utc(ms: int) -> int:
        return start_utc + ms

    def block_of(ms: int) -> int:
        return first_block + (ms - origin_ms) // interval

    for s in specs:
        tok_key = f"{FIXTURE_CHAIN_ID}:tok_{s.index:02d}"
        pool_key = f"{FIXTURE_CHAIN_ID}:fixture_cpmm:pool_{s.index:02d}"
        created_ms = max(s.created_offset_ms, origin_ms)
        created_block = block_of(created_ms)
        created_ms = origin_ms + (created_block - first_block) * interval
        token_reserve = int(s.cash_reserve / 10**CASH_DECIMALS / s.price_cash_per_token * 10**TOKEN_DECIMALS)
        assets.append(
            {
                "key": tok_key,
                "chain_id": FIXTURE_CHAIN_ID,
                "address": f"tok_{s.index:02d}",
                "decimals": TOKEN_DECIMALS,
                # Deliberate symbol collisions (MOON twice) to prove symbols are not identity.
                "symbol": token_symbols[s.index % len(token_symbols)],
                "name": f"Fixture token {s.index}",
                "created_block": created_block,
                "created_time_utc_ms": rel_to_utc(created_ms),
                "discovery_available_utc_ms": rel_to_utc(created_ms + discovery_delay),
                "is_numeraire": False,
                "fixture_rules": {"standard_transfer": True, "scripted_restrictions": [e for e in s.events if e["kind"] == "restriction"]},
            }
        )
        pool_row = {
            "key": pool_key,
            "chain_id": FIXTURE_CHAIN_ID,
            "protocol": "fixture_cpmm",
            "address": f"pool_{s.index:02d}",
            "model": str(s.model),
            "asset0": CASH_KEY,
            "asset1": tok_key,
            "fee_numerator": 997,
            "fee_denominator": 1000,
            "created_block": created_block,
            "created_time_utc_ms": rel_to_utc(created_ms),
            "discovery_available_utc_ms": rel_to_utc(created_ms + discovery_delay),
            "initial_reserve0": str(s.cash_reserve) if s.supported else None,
            "initial_reserve1": str(token_reserve) if s.supported else None,
            "initial_state_block": created_block if s.supported else None,
            "initial_state_basis": "fixture_construction" if s.supported else None,
            "factory": "fixture_factory",
            "supported_by_cpmm": s.supported,
            "unsupported_reason": s.unsupported_reason,
        }
        pools.append(pool_row)
        if not s.supported:
            inventory["unsupported"].append({"pool": pool_key, "reason": s.unsupported_reason, "model": str(s.model)})
            continue
        inventory["selected_count"] += 1
        state[s.index] = [s.cash_reserve, token_reserve]
        live_from[s.index] = created_block
        # coverage: complete from creation to end (+tail), except dropout windows marked partial
        cov_start = rel_to_utc(created_ms)
        cov_end = rel_to_utc(cfg.duration_ms + p.settlement_tail_blocks * interval)
        cuts = sorted((max(a, created_ms), b) for a, b in cfg.dropout_windows if b > created_ms)
        cursor = cov_start
        for a, b in cuts:
            if rel_to_utc(a) > cursor:
                coverage_intervals.append({"object_ref": pool_key, "field": "swaps", "start_utc_ms": cursor, "end_utc_ms": rel_to_utc(a), "state": "completed_and_checked", "evidence": "generated_by_construction"})
            coverage_intervals.append({"object_ref": pool_key, "field": "swaps", "start_utc_ms": rel_to_utc(a), "end_utc_ms": rel_to_utc(b), "state": "partial", "evidence": "fixture_publication_dropout", "gaps": [{"reason": "unpublished_observations"}]})
            cursor = rel_to_utc(b)
        if cursor < cov_end:
            coverage_intervals.append({"object_ref": pool_key, "field": "swaps", "start_utc_ms": cursor, "end_utc_ms": cov_end, "state": "completed_and_checked", "evidence": "generated_by_construction"})

    def in_dropout(ms: int) -> bool:
        return any(a <= ms < b for a, b in cfg.dropout_windows)

    def buy_prob(s: PoolSpec, ms: int) -> float:
        prob = s.regimes[0][1]
        for start, pr in s.regimes:
            if ms >= start:
                prob = pr
        return prob

    scripted: dict[int, list[tuple[int, dict[str, Any]]]] = {}
    for s in specs:
        for ev in s.events:
            scripted.setdefault(block_of(ev["at"]), []).append((s.index, ev))

    since_sync: dict[int, int] = {i: 0 for i in state}
    for b in range(first_block, first_block + n_blocks):
        block_ms = origin_ms + (b - first_block) * interval
        if block_ms >= cfg.duration_ms:
            break
        log_index = 0
        for s in specs:
            i = s.index
            if i not in state or live_from[i] > b:
                continue
            res = state[i]
            # number of swaps this block (0,1,2)
            u0 = _u(cfg.seed, "n", i, b)
            n_sw = 0 if u0 > s.activity else (2 if u0 < s.activity * 0.15 else 1)
            for k in range(n_sw):
                is_buy = _u(cfg.seed, "dir", i, b, k) < buy_prob(s, block_ms)
                if is_buy:
                    median = max(1, res[0] // 2000)
                    amount_in = _lognormal_int(_u(cfg.seed, "sz1", i, b, k), _u(cfg.seed, "sz2", i, b, k), median, 0.9)
                    amount_in = min(amount_in, res[0] // 50)
                    out = get_amount_out(amount_in, res[0], res[1])
                    if out <= 0 or out >= res[1]:
                        continue
                    res[0] += amount_in
                    res[1] -= out
                    asset_in = CASH_KEY
                else:
                    median = max(1, res[1] // 2000)
                    amount_in = _lognormal_int(_u(cfg.seed, "sz1", i, b, k), _u(cfg.seed, "sz2", i, b, k), median, 0.9)
                    amount_in = min(amount_in, res[1] // 50)
                    out = get_amount_out(amount_in, res[1], res[0])
                    if out <= 0 or out >= res[0]:
                        continue
                    res[1] += amount_in
                    res[0] -= out
                    asset_in = f"{FIXTURE_CHAIN_ID}:tok_{i:02d}"
                seq += 1
                wallet_n = int(_u(cfg.seed, "w", i, b, k) * 40)
                dropped = in_dropout(block_ms)
                tape.append(
                    {
                        "seq": seq,
                        "block": b,
                        "log_index": log_index,
                        "time_utc_ms": rel_to_utc(block_ms),
                        "kind": "swap",
                        "pool": f"{FIXTURE_CHAIN_ID}:fixture_cpmm:pool_{i:02d}",
                        "tx": f"fixture_tx_{seq}",
                        "wallet": f"fixture_wallet_{wallet_n}",
                        "asset_in": asset_in,
                        "amount_in": str(amount_in),
                        "amount_out_recorded": str(out),
                        "available_utc_ms": None if dropped else rel_to_utc(block_ms + delay),
                        "availability_basis": str(AvailabilityBasis.FIXTURE_DELAY_MODEL),
                    }
                )
                log_index += 1
                since_sync[i] += 1
            for idx, ev in scripted.get(b, []):
                if idx != i:
                    continue
                seq += 1
                row: dict[str, Any] = {
                    "seq": seq,
                    "block": b,
                    "log_index": log_index,
                    "time_utc_ms": rel_to_utc(block_ms),
                    "pool": f"{FIXTURE_CHAIN_ID}:fixture_cpmm:pool_{i:02d}",
                    "available_utc_ms": rel_to_utc(block_ms + delay),
                    "availability_basis": str(AvailabilityBasis.FIXTURE_DELAY_MODEL),
                }
                if ev["kind"] == "mint":
                    a0 = int(res[0] * ev["fraction"])
                    a1 = int(res[1] * ev["fraction"])
                    res[0] += a0
                    res[1] += a1
                    row.update({"kind": "mint", "amount0": str(a0), "amount1": str(a1), "tx": f"fixture_tx_{seq}", "wallet": "fixture_lp_0"})
                elif ev["kind"] == "burn":
                    a0 = int(res[0] * ev["fraction"])
                    a1 = int(res[1] * ev["fraction"])
                    res[0] -= a0
                    res[1] -= a1
                    row.update({"kind": "burn", "amount0": str(a0), "amount1": str(a1), "tx": f"fixture_tx_{seq}", "wallet": "fixture_lp_0"})
                elif ev["kind"] == "halt":
                    row.update({"kind": "halt", "payload": {"reason": "fixture_drained_pool_halt"}})
                elif ev["kind"] == "restriction":
                    row.update({"kind": "restriction", "payload": {"asset": f"{FIXTURE_CHAIN_ID}:tok_{i:02d}", **ev["payload"]}})
                tape.append(row)
                log_index += 1
                since_sync[i] += 1
            if since_sync[i] >= cfg.sync_every:
                seq += 1
                tape.append(
                    {
                        "seq": seq,
                        "block": b,
                        "log_index": log_index,
                        "time_utc_ms": rel_to_utc(block_ms),
                        "kind": "sync",
                        "pool": f"{FIXTURE_CHAIN_ID}:fixture_cpmm:pool_{i:02d}",
                        "reserve0": str(res[0]),
                        "reserve1": str(res[1]),
                        "available_utc_ms": None,
                        "availability_basis": str(AvailabilityBasis.FIXTURE_DELAY_MODEL),
                    }
                )
                log_index += 1
                since_sync[i] = 0

    coverage = {
        "schema": "coverage_v1",
        "basis": "generated_by_construction",
        "intervals": coverage_intervals,
        "summary": {
            "pools": len(state),
            "tape_events": len(tape),
            "dropout_windows_ms": [[a, b] for a, b in cfg.dropout_windows],
        },
    }
    universe = Universe(
        factories=["fixture_factory"],
        pool_models=[PoolModel.FIXTURE_CPMM] + ([PoolModel.UNISWAP_V3] if any(not s.supported for s in specs) else []),
        quote_asset=CASH_KEY,
        selection_rule_version="fixture_all_generated_pools_v1",
        indexed_block_ranges=[[first_block, first_block + n_blocks - 1]],
        excluded_or_unsupported_counts={"unsupported_mechanics": len(inventory["unsupported"])},
        candidate_count=len(specs),
        selected_count=inventory["selected_count"],
        unsupported_count=len(inventory["unsupported"]),
        missing_count=0,
        description="All pools were constructed by the fixture generator; this is not a market taxonomy.",
    )
    return build_pack(
        out_dir,
        origin=DataOrigin.GENERATED_FIXTURE,
        chain=FIXTURE_CHAIN,
        chain_id=FIXTURE_CHAIN_ID,
        scope_label=f"generated_{cfg.scenario}_v1",
        title_private=f"Generated fixture: {cfg.name} (seed {cfg.seed})",
        period=make_period(start_utc, end_utc, pre_start),
        universe=universe,
        assets=assets,
        pools=pools,
        tape=tape,
        params=p,
        coverage=coverage,
        numeraire=CASH_KEY,
        numeraire_alias="CASH",
        numeraire_decimals=CASH_DECIMALS,
        token_behavior=TokenBehavior.KNOWN_FIXTURE_RULES,
        availability_model={
            "kind": "fixture_constant_delay",
            "delay_ms": delay,
            "note": "Artificial publication delay; unpublished observations exist in dropout windows.",
        },
        rights=Rights(storage_basis="generated_locally", local_processing_basis="generated_locally", redistribution="permitted_generated_fixture", simulator_serving="permitted_generated_fixture", notes="No third-party data."),
        qualification=UseStatus.DEMO,
        inventory=inventory,
        generator={"generator_version": "fixture_generator_v1", "config": {"name": cfg.name, "seed": cfg.seed, "scenario": cfg.scenario, "n_pools": cfg.n_pools, "duration_ms": cfg.duration_ms, "prehistory_ms": cfg.prehistory_ms, "week_index": cfg.week_index, "dropout_windows": cfg.dropout_windows}, "scenario_description": SCENARIOS.get(cfg.scenario, "")},
        provenance_notes=["GENERATED FIXTURE. No historical market data. Not evidence of historical performance."],
        decision_log=["Fixture uses an artificial CASH numeraire and 2000 ms blocks; values are test settings."],
    )


def standard_suite_configs() -> list[GeneratorConfig]:
    """The shipped generated practice suite: four full weeks plus one short development fixture."""
    week = WEEK_MS
    return [
        GeneratorConfig(name="gen_week_trending", seed="mr-gen-2026-trending", scenario="trending", n_pools=8, week_index=0),
        GeneratorConfig(name="gen_week_reversal", seed="mr-gen-2026-reversal", scenario="reversal", n_pools=8, week_index=1),
        GeneratorConfig(
            name="gen_week_sparse_missing",
            seed="mr-gen-2026-sparse",
            scenario="sparse_missing",
            n_pools=6,
            week_index=2,
            dropout_windows=[(week // 3, week // 3 + 7_200_000), (week * 2 // 3, week * 2 // 3 + 3_600_000)],
        ),
        GeneratorConfig(name="gen_week_liquidity_shift", seed="mr-gen-2026-liq", scenario="liquidity_shift", n_pools=8, week_index=3),
    ]


def dev_short_config(**overrides: Any) -> GeneratorConfig:
    params = fixture_default_params(reporting_grid_ms=60_000)
    base = dict(
        name="gen_dev_short",
        seed="mr-gen-dev-short",
        duration_ms=7_200_000,
        prehistory_ms=1_800_000,
        scenario="dev_short",
        n_pools=4,
        week_index=10,
        params=params,
        dropout_windows=[(3_000_000, 3_300_000)],
        scaled_activity=3.0,
    )
    base.update(overrides)
    return GeneratorConfig(**base)  # type: ignore[arg-type]
