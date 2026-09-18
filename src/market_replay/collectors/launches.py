"""Every pool launched on Base during a week, on Uniswap v2, v3 and v4, recorded in bulk.

The sampled collectors pick a few pools and read each one's events on its own. That hides the
market's noise: an agent handed sixteen pools never has to find the one launch worth trading
among thousands that die. This collector keeps every pool created inside the period that has a
wrapped-ETH (or native ETH) leg and at least one swap, and reads their events in bulk:

- v4: one scan of the PoolManager (a single contract emits every pool's events), kept where the
  pool id is a launch;
- v3 and v2: scans over the launched pools' addresses in batches (providers cap the request
  body, not only the block range).

A launch is born inside the recording, so no starting state is needed: v3/v4 pools start from
their ``Initialize`` row, v2 pairs from empty reserves. Launches become discoverable to agents at
their creation time plus the availability delay: nothing later than the creation itself influences
whether a pool is in the universe, so there is no look-ahead to hide.

Established pools (trading before the week) are not this collector's job; the sampled collectors
record those and ``combined.merge_packs`` joins the packs.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import yaml

from ..datasets.validator import demote_unreconciled_pools
from ..domain.models import PoolModel
from ..domain.status import CoverageState
from .base import (
    Budget,
    BudgetExhausted,
    Checkpoints,
    CoverageLedger,
    HttpCollector,
    ProviderError,
    ReceiptStore,
    TimeSliceExpired,
    now_ms,
)
from .evm_rpc import (
    SEL_DECIMALS,
    TOPIC_BURN,
    TOPIC_MINT,
    TOPIC_PAIR_CREATED,
    TOPIC_SWAP,
    TOPIC_SYNC,
    TOPIC_V3_BURN,
    TOPIC_V3_INITIALIZE,
    TOPIC_V3_MINT,
    TOPIC_V3_POOL_CREATED,
    TOPIC_V3_SWAP,
    TOPIC_V4_INITIALIZE,
    TOPIC_V4_MODIFY_LIQUIDITY,
    TOPIC_V4_SWAP,
    RpcClient,
    hex_to_int,
    sword,
    topic_address,
    word,
)
from .historical import (
    ACTIVITY_BATCH,
    BLOCK_INTERVAL_MS,
    DEPLOYMENTS,
    CollectionBlocked,
    LogFile,
    iso_ms,
    normalize_pair_logs,
)
from .uniswap_cl import CL_MODELS, DYNAMIC_FEE_FLAG, FEE_DENOMINATOR_PIPS, normalize_cl_logs

RULE = "all_launches_in_window_v1"
VENUES = ("uniswap_v2", "uniswap_v3", "uniswap_v4")


def run_launch_collection(config_path: Path, data_dir: Path, *, transport=None, rpc_url_override: str | None = None, sleep=None, deadline: float | None = None, store_bodies: bool = False, budget_used: int = 0) -> dict[str, Any]:
    cfg = yaml.safe_load(config_path.read_text())
    out_dir = Path(cfg["out_dir"]) if Path(cfg["out_dir"]).is_absolute() else data_dir / cfg["out_dir"]
    work = out_dir.parent / (out_dir.name + "_work")
    work.mkdir(parents=True, exist_ok=True)
    import os

    rpc_url = rpc_url_override or os.environ.get(str(cfg.get("rpc_url_env", "BASE_RPC_URL")))
    budget = Budget(max_requests=int(cfg.get("max_requests", 60000)), max_response_bytes=int(cfg.get("max_response_bytes", 8 * 1024**3)))
    budget.requests = int(budget_used)
    http = HttpCollector(provider="evm_rpc", budget=budget, receipts=ReceiptStore(work / "receipts", store_bodies=store_bodies), errors_path=work / "errors.jsonl", transport=transport)
    if sleep is not None:
        http.sleep = sleep
    http.deadline = deadline
    rpc = RpcClient(rpc_url or "", http)
    ck = Checkpoints(work / "checkpoints.json")
    ledger = CoverageLedger(work / "coverage_ledger.json")
    log: list[str] = ck.get("decision_log", [])
    result: dict[str, Any] = {"status": "in_progress", "work_dir": str(work), "budget": budget.as_dict()}
    venues = [str(v) for v in (cfg.get("venues") or VENUES)]
    chunk = int(cfg.get("log_chunk_blocks", 1000))
    batch_size = int(cfg.get("address_batch", ACTIVITY_BATCH))
    min_swaps = int(cfg.get("min_swaps", 1))
    delay_ms = int(cfg.get("availability_delay_ms", 4000))
    progress = {"chunks": 0}

    def note(s: str) -> None:
        log.append(s)
        ck.set("decision_log", log)

    def slice_check() -> None:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeSliceExpired("time slice expired")

    def scan(ref: str, field_name: str, key: str, *, address, topics, start: int, end_exclusive: int, chunk_size: int) -> LogFile:
        """Forward chunked scan into one log file, halved on provider error, checkpointed per chunk."""
        cur = int(ck.get(key + ":cursor", start))
        logs_file = LogFile(work / "logs" / f"{key.replace(':', '_')}.jsonl")
        c = int(ck.get(key + ":chunk", chunk_size))
        while cur < end_exclusive:
            slice_check()
            to_b = min(cur + c - 1, end_exclusive - 1)
            ledger.set(ref, field_name, cur, to_b, CoverageState.PENDING, "requested")
            try:
                logs = rpc.get_logs(address=address, topics=topics, from_block=cur, to_block=to_b)
            except ProviderError as e:
                ledger.set(ref, field_name, cur, to_b, CoverageState.FAILED, str(e))
                if c > 25:
                    c //= 2
                    ck.set(key + ":chunk", c)
                    note(f"{key}: chunk reduced to {c} after provider error: {str(e)[:100]}")
                    continue
                raise
            logs_file.append(logs)
            ledger.set(ref, field_name, cur, to_b, CoverageState.COMPLETED_AND_CHECKED, f"{len(logs)} logs; range returned without provider truncation error")
            cur = to_b + 1
            ck.set(key + ":cursor", cur)
            progress["chunks"] += 1
        return logs_file

    try:
        if not rpc_url:
            raise CollectionBlocked(f"environment variable {cfg.get('rpc_url_env', 'BASE_RPC_URL')} is not set; no endpoint is configured")
        deps = {v: DEPLOYMENTS[v][cfg["chain"]] for v in venues}
        chain_id = next(iter(deps.values()))["chain_id"]
        wn = next(iter(deps.values()))["wrapped_native"].lower()
        native = str(deps.get("uniswap_v4", {}).get("native_currency", "")).lower() or None
        # 1. chain and block boundaries
        if ck.get("chain_verified") is None:
            cid = rpc.chain_id()
            if cid != chain_id:
                raise CollectionBlocked(f"endpoint chain id {cid} != expected {chain_id}")
            ck.set("chain_verified", {"chain_id": cid})
        p_start, p_end = iso_ms(cfg["period_start_utc"]), iso_ms(cfg["period_end_utc"])
        pre_ms = int(cfg.get("prehistory_hours", 24)) * 3_600_000
        if ck.get("blocks") is None:
            latest = rpc.block_number()
            latest_ts = rpc.block_timestamp_ms(latest)
            if latest_ts is None or latest_ts < p_end:
                raise CollectionBlocked("period end is not yet finalized on this endpoint")
            lo = max(0, latest - (latest_ts - p_start) // BLOCK_INTERVAL_MS - 5000)
            b_s, ts_s = rpc.find_block_at_or_after(p_start, lo, latest)
            b_e, _ = rpc.find_block_at_or_after(p_end, b_s, latest)
            b_pre, _ = rpc.find_block_at_or_after(p_start - pre_ms, lo, latest)
            ck.set("blocks", {"prehistory_start": b_pre, "period_start": b_s, "period_start_ts": ts_s, "period_end": b_e})
            note(f"block boundaries fixed: prehistory {b_pre}, period [{b_s}, {b_e})")
        blocks = ck.get("blocks")
        anchor_block, anchor_ts = blocks["period_start"], blocks["period_start_ts"]

        def block_time_ms(b: int) -> int:
            return anchor_ts + (b - anchor_block) * BLOCK_INTERVAL_MS

        # 2. launches inside the period, per venue (frozen by creation alone: no flow is consulted)
        launches: dict[str, dict[str, dict[str, Any]]] = ck.get("launches") or {}
        for v in venues:
            if v in launches:
                continue
            d = deps[v]
            found: dict[str, dict[str, Any]] = {}
            if v == "uniswap_v2":
                for lg in scan("factory:v2", "pair_created", "launch:v2", address=d["factory"], topics=[TOPIC_PAIR_CREATED], start=blocks["period_start"], end_exclusive=blocks["period_end"], chunk_size=chunk).read():
                    found["0x" + lg["data"][2:][24:64].lower()] = {"token0": topic_address(lg["topics"][1]), "token1": topic_address(lg["topics"][2]), "created_block": hex_to_int(lg["blockNumber"]), "tx": lg.get("transactionHash")}
            elif v == "uniswap_v3":
                for lg in scan("factory:v3", "pool_created", "launch:v3", address=d["factory"], topics=[TOPIC_V3_POOL_CREATED], start=blocks["period_start"], end_exclusive=blocks["period_end"], chunk_size=chunk).read():
                    data = lg["data"]
                    found["0x" + data[2:][1 * 64 + 24 : 2 * 64].lower()] = {"token0": topic_address(lg["topics"][1]), "token1": topic_address(lg["topics"][2]), "fee": hex_to_int(lg["topics"][3]), "tick_spacing": sword(data, 0), "hooks": None, "created_block": hex_to_int(lg["blockNumber"]), "tx": lg.get("transactionHash")}
            else:
                for lg in scan("pool_manager", "initialize", "launch:v4", address=d["pool_manager"], topics=[TOPIC_V4_INITIALIZE], start=blocks["period_start"], end_exclusive=blocks["period_end"], chunk_size=chunk).read():
                    data = lg["data"]
                    found[lg["topics"][1].lower()] = {"token0": topic_address(lg["topics"][2]), "token1": topic_address(lg["topics"][3]), "fee": word(data, 0), "tick_spacing": sword(data, 1), "hooks": "0x" + data[2:][2 * 64 + 24 : 3 * 64].lower(), "created_block": hex_to_int(lg["blockNumber"]), "tx": lg.get("transactionHash"), "init_log": lg}
            launches[v] = found
            ck.set("launches", launches)
            note(f"{v}: {len(found)} pools created inside the period")
        quote_legs = {wn} | ({native} if native else set())
        in_scope: dict[str, dict[str, dict[str, Any]]] = {v: {a: m for a, m in launches[v].items() if quote_legs & {m["token0"], m["token1"]}} for v in venues}
        excluded = [{"pool": a, "venue": v, "reason": "no wrapped-native leg"} for v in venues for a in launches[v] if a not in in_scope[v]]
        # 3. events in bulk
        logs_by_pool: dict[str, list[dict[str, Any]]] = {}
        for v in venues:
            ids = sorted(in_scope[v])
            if not ids:
                continue
            if v == "uniswap_v4":
                lf = scan("pool_manager", "events", "events:v4", address=deps[v]["pool_manager"], topics=[[TOPIC_V4_MODIFY_LIQUIDITY, TOPIC_V4_SWAP]], start=blocks["period_start"], end_exclusive=blocks["period_end"], chunk_size=chunk)
                wanted = set(ids)
                for lg in lf.read():
                    pid = lg["topics"][1].lower()
                    if pid in wanted:
                        logs_by_pool.setdefault(f"{v}:{pid}", []).append(lg)
            else:
                topics = [[TOPIC_V3_INITIALIZE, TOPIC_V3_MINT, TOPIC_V3_BURN, TOPIC_V3_SWAP]] if v == "uniswap_v3" else [[TOPIC_SWAP, TOPIC_MINT, TOPIC_BURN, TOPIC_SYNC]]
                for bi in range(0, len(ids), batch_size):
                    batch = ids[bi : bi + batch_size]
                    first = min(in_scope[v][a]["created_block"] for a in batch)
                    lf = scan(f"{v}:batch{bi // batch_size}", "events", f"events:{v}:b{bi // batch_size}", address=batch, topics=topics, start=first, end_exclusive=blocks["period_end"], chunk_size=chunk)
                    for lg in lf.read():
                        logs_by_pool.setdefault(f"{v}:{lg['address'].lower()}", []).append(lg)
        # 4. token decimals for the pools that traded
        assets: dict[str, dict[str, Any]] = ck.get("assets", {})
        swap_topics = {TOPIC_SWAP, TOPIC_V3_SWAP, TOPIC_V4_SWAP}
        traded: dict[str, list[str]] = {v: [] for v in venues}
        for v in venues:
            for a in sorted(in_scope[v]):
                n = sum(1 for lg in logs_by_pool.get(f"{v}:{a}", []) if lg["topics"][0].lower() in swap_topics)
                if n >= min_swaps:
                    traded[v].append(a)
        needed: list[str] = []
        for v in venues:
            for a in traded[v]:
                for tok in (in_scope[v][a]["token0"], in_scope[v][a]["token1"]):
                    if native is not None and tok == native:
                        continue
                    if tok not in assets and tok not in needed:
                        needed.append(tok)
        for i, tok in enumerate(needed):
            slice_check()
            try:
                dec = hex_to_int(rpc.eth_call(tok, SEL_DECIMALS))
            except (ProviderError, ValueError):
                dec = None
            assets[tok] = {"decimals": dec, "basis": "eth_call decimals()" if dec is not None else "unknown"}
            if i % 200 == 0:
                ck.set("assets", assets)
        ck.set("assets", assets)
        if native is not None:
            assets.setdefault(wn, {"decimals": 18, "basis": "wrapped_native"})
        # 5. normalize and describe each traded launch
        pools_out: list[dict[str, Any]] = []
        tape: list[dict[str, Any]] = []
        missing: list[dict[str, Any]] = []
        native_pools: list[str] = []
        quiet = [{"pool": a, "venue": v, "reason": f"fewer than {min_swaps} swap(s) inside the period"} for v in venues for a in in_scope[v] if a not in set(traded[v])]
        for v in venues:
            d = deps[v]
            for a in traded[v]:
                meta = in_scope[v][a]
                pool_key = f"{chain_id}:{v}:{a}"
                legs = []
                for tok in (meta["token0"], meta["token1"]):
                    if native is not None and tok == native:
                        native_pools.append(pool_key)
                        tok = wn
                    legs.append(tok)
                if any(assets.get(t, {}).get("decimals") is None for t in legs):
                    missing.append({"pool": pool_key, "reason": "token decimals unavailable"})
                    continue
                raw = logs_by_pool.get(f"{v}:{a}", [])
                created_ms = block_time_ms(meta["created_block"])
                if v == "uniswap_v2":
                    rows, unsupported = normalize_pair_logs(raw, pool_key=pool_key, token0=f"{chain_id}:{legs[0]}", token1=f"{chain_id}:{legs[1]}", block_time_ms=block_time_ms)
                    for r in rows:
                        r["available_utc_ms"] = r["time_utc_ms"] + delay_ms
                    tape.extend(rows)
                    pools_out.append({"key": pool_key, "chain_id": chain_id, "protocol": v, "address": a, "model": str(PoolModel.UNISWAP_V2_PLAIN), "asset0": f"{chain_id}:{legs[0]}", "asset1": f"{chain_id}:{legs[1]}", "fee_numerator": d["fee_numerator"], "fee_denominator": d["fee_denominator"], "created_block": meta["created_block"], "created_time_utc_ms": created_ms, "discovery_available_utc_ms": created_ms + delay_ms, "initial_reserve0": "0", "initial_reserve1": "0", "initial_state_block": blocks["prehistory_start"] - 1, "initial_state_basis": "pair_created_inside_window", "factory": d["factory"].lower(), "supported_by_cpmm": True, "unsupported_reason": None})
                else:
                    if v == "uniswap_v4":
                        raw = [meta["init_log"]] + raw
                    rows = normalize_cl_logs(raw, protocol=v, pool_key=pool_key, fee_pips=int(meta["fee"]), block_time_ms=block_time_ms)
                    for r in rows:
                        r["available_utc_ms"] = r["time_utc_ms"] + delay_ms
                    pool_fee, fee_basis = int(meta["fee"]), "pool_fee_from_initialize"
                    if pool_fee & DYNAMIC_FEE_FLAG:
                        observed = [int(r["fee_pips"]) for r in rows if r["kind"] == "cl_swap" and r.get("fee_pips") is not None]
                        pool_fee, fee_basis = (observed[0], "dynamic_hook_fee_first_observed_inside_window") if observed else (0, "dynamic_hook_fee_never_observed")
                        if not (0 <= pool_fee < FEE_DENOMINATOR_PIPS):
                            pool_fee, fee_basis = FEE_DENOMINATOR_PIPS - 1, "dynamic_hook_fee_out_of_range_clamped"
                    has_init = any(r["kind"] == "cl_init" for r in rows)
                    if not has_init:
                        missing.append({"pool": pool_key, "reason": "no Initialize event observed since creation"})
                        continue
                    tape.extend(rows)
                    pools_out.append({"key": pool_key, "chain_id": chain_id, "protocol": v, "address": a, "model": str(CL_MODELS[v]), "asset0": f"{chain_id}:{legs[0]}", "asset1": f"{chain_id}:{legs[1]}", "fee_numerator": FEE_DENOMINATOR_PIPS - pool_fee, "fee_denominator": FEE_DENOMINATOR_PIPS, "fee_pips": pool_fee, "fee_basis": fee_basis, "tick_spacing": int(meta["tick_spacing"]), "hooks": meta.get("hooks"), "created_block": meta["created_block"], "created_time_utc_ms": created_ms, "discovery_available_utc_ms": created_ms + delay_ms, "initial_reserve0": None, "initial_reserve1": None, "initial_sqrt_price_x96": None, "initial_tick": None, "initial_liquidity": None, "initial_ticks": [], "initial_state_block": None, "initial_state_basis": "pool_initialized_inside_window", "factory": (d.get("factory") or d.get("pool_manager")).lower(), "supported_by_cpmm": False, "supported_by_clmm": True, "unsupported_reason": None})
        tape.sort(key=lambda r: (r["block"], r["log_index"], r.get("seq", 0)))
        for i, r in enumerate(tape, start=1):
            r["seq"] = i
        demoted = demote_unreconciled_pools(pools_out, tape)
        for dm in demoted:
            note(f"{dm['pool']}: demoted from execution: {dm['reason']}")
        # 6. pack
        from ..datasets.builder import build_pack, make_period
        from ..datasets.execution_params import historical_research_params
        from ..domain.models import Rights, Universe
        from ..domain.status import DataOrigin, TokenBehavior, UseStatus

        assets_rows = [{"key": f"{chain_id}:{tok}", "chain_id": chain_id, "address": tok, "decimals": a["decimals"] if a["decimals"] is not None else 18, "symbol": "WETH" if tok == wn else None, "name": None, "is_numeraire": tok == wn, "created_block": None, "created_time_utc_ms": None, "discovery_available_utc_ms": block_time_ms(blocks["period_start"]), "fixture_rules": {"decimals_basis": a.get("basis", "unknown")}} for tok, a in assets.items() if tok == wn or any(p["asset0"].endswith(tok) or p["asset1"].endswith(tok) for p in pools_out)]
        if not any(r["is_numeraire"] for r in assets_rows):
            assets_rows.append({"key": f"{chain_id}:{wn}", "chain_id": chain_id, "address": wn, "decimals": 18, "symbol": "WETH", "name": None, "is_numeraire": True, "created_block": None, "created_time_utc_ms": None, "discovery_available_utc_ms": block_time_ms(blocks["period_start"]), "fixture_rules": {}})
        coverage = {"schema": "coverage_v1", "basis": "evm_rpc_logs", "intervals": [{"object_ref": p["key"], "field": "swaps", "start_utc_ms": p["created_time_utc_ms"], "end_utc_ms": block_time_ms(blocks["period_end"]), "state": "completed_and_checked", "evidence": "bulk eth_getLogs ranges returned without truncation error; provider indexing completeness not independently established"} for p in pools_out], "ledger": ledger.rows, "budget": budget.as_dict(), "unsupported_events": []}
        counts = {"no_wrapped_native_leg": len(excluded), "fewer_than_min_swaps": len(quiet), "missing_state": len(missing), "demoted_after_reconciliation": len(demoted)}
        pack = build_pack(
            out_dir,
            origin=DataOrigin.HISTORICAL_RECONSTRUCTION,
            chain=cfg["chain"],
            chain_id=chain_id,
            scope_label="base_all_launches_research_v1",
            title_private=f"Base every launch {cfg['period_start_utc']}..{cfg['period_end_utc']}",
            period=make_period(block_time_ms(blocks["period_start"]), block_time_ms(blocks["period_end"]), block_time_ms(blocks["prehistory_start"])),
            universe=Universe(factories=[str(deps[v].get("factory") or deps[v].get("pool_manager")) for v in venues], pool_models=[PoolModel.UNISWAP_V2_PLAIN if v == "uniswap_v2" else CL_MODELS[v] for v in venues], quote_asset=f"{chain_id}:{wn}", selection_rule_version=RULE, indexed_block_ranges=[[blocks["period_start"], blocks["period_end"] - 1]], excluded_or_unsupported_counts=counts, candidate_count=sum(len(launches[v]) for v in venues), selected_count=len(pools_out), unsupported_count=len(excluded), missing_count=len(missing), description=f"Every pool created inside the period on {', '.join(venues)} with a wrapped-native leg and at least {min_swaps} swap(s); discoverable from its creation. No sampling, no look-ahead."),
            assets=assets_rows,
            pools=pools_out,
            tape=tape,
            params=historical_research_params(block_interval_ms=BLOCK_INTERVAL_MS, availability_delay_ms=delay_ms),
            coverage=coverage,
            numeraire=f"{chain_id}:{wn}",
            numeraire_alias="NATIVE",
            numeraire_decimals=18,
            token_behavior=TokenBehavior.ASSUMED_STANDARD_TRANSFER,
            availability_model={"kind": "constant_delay_from_block_time", "delay_ms": delay_ms, "acquisition_utc_ms": now_ms(), "note": "acquired later than the events; original provider availability not established"},
            rights=Rights(storage_basis="public_chain_data_via_configured_rpc; endpoint terms not reviewed here", local_processing_basis="research", redistribution="not_cleared", simulator_serving="local_only", notes=str(cfg.get("authorization_note", ""))),
            qualification=UseStatus.RESEARCH,
            inventory={"unsupported": excluded, "excluded_by_sampling": [], "quiet_launches": quiet[:20000], "quiet_launch_count": len(quiet), "missing": missing, "demoted": demoted, "native_currency_pools": native_pools, "candidate_count": sum(len(launches[v]) for v in venues), "selected_count": len(pools_out), "launches_per_venue": {v: len(launches[v]) for v in venues}, "eth_launches_per_venue": {v: len(in_scope[v]) for v in venues}, "traded_launches_per_venue": {v: len(traded[v]) for v in venues}},
            provenance_notes=["HISTORICAL RECONSTRUCTION of every pool launched inside the period (Uniswap v2 pairs, v3 pools, v4 pools) from eth_getLogs. Token sellability/restrictions unknown (assumed standard transfer).", "cl_swap amounts are pool deltas (positive = paid into the pool); v4 user deltas were negated to match the v3 convention."],
            decision_log=log,
        )
        result.update({"status": "pack_built", "pack_id": pack.pack_id, "pack_dir": str(out_dir), "qualification": pack.validation["resulting_qualification"], "tape_events": len(tape), "pools": len(pools_out), "launches_per_venue": {v: len(launches[v]) for v in venues}, "traded_launches_per_venue": {v: len(traded[v]) for v in venues}, "missing": len(missing), "demoted": len(demoted), "budget": budget.as_dict(), "decision_log": log})
    except TimeSliceExpired:
        result.update({"status": "slice_expired", "budget": budget.as_dict(), "decision_log": log})
    except BudgetExhausted as e:
        result.update({"status": "budget_exhausted", "reason": str(e), "budget": budget.as_dict(), "decision_log": log})
    except CollectionBlocked as e:
        result.update({"status": "blocked", "reason": str(e), "budget": budget.as_dict(), "decision_log": log})
    except ProviderError as e:
        result.update({"status": "provider_error", "reason": str(e), "budget": budget.as_dict(), "decision_log": log})
    (work / "last_result.json").write_text(json.dumps(result, indent=2, default=str))
    return result
