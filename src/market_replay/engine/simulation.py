"""The deterministic simulation for one participant on one pack.

Internal market state (private pool copies: CPMM ``CpmmPoolState`` or concentrated
liquidity ``ClPoolState``), the no-agent reference state, the agent-visible observation
store, the pending-order schedule and the ledger all live here. Time is integer
milliseconds relative to the episode start. Nothing here reads the wall clock or the
network.

Both pool kinds expose the same surface (``depth_for``, ``max_out``, ``apply_swap``,
``copy``, ``other``, ``transfer_blocked``, ``halted``, ``restrictions``, ``fee_num`` /
``fee_den``, ``spot_price_fraction``). For a CPMM the depth is the reserve pair; for a
concentrated-liquidity pool it is the virtual reserves of the active tick range at the
current price, so every depth-based check (capacity, liquidation ranking) reads the
active range only.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from ..broker.ledger import (
    AGENT_AVAILABLE,
    AGENT_PENDING,
    AGENT_RESERVED,
    SINK_GAS,
    SOURCE_BANKROLL,
    Ledger,
    LedgerError,
    Leg,
)
from ..broker.orders import Order, Quote, payload_hash
from ..datasets.pack import Pack
from ..domain.status import SUPPORTED_CPMM_MODELS, CoverageState, OrderState, PoolModel, ValuationClass
from ..observations.store import CoverageSpan, PoolObservations, TradeObs
from ..venues.clmm.math import ClMathError, get_tick_at_sqrt_ratio
from ..venues.clmm.pool import SUPPORTED_CLMM_MODELS, ClPoolState
from ..venues.cpmm.math import CpmmMathError
from ..venues.cpmm.pool import CpmmPoolState, FidelityLimit, classify_checkpoint_delta
from .blocks import BlockSchedule, BlockScheduleError, FixedIntervalSchedule, TableSchedule
from .tape import RelEvent, to_relative

INF = 1 << 62
PoolState = CpmmPoolState | ClPoolState
# Every "the model cannot fill this" error from either venue adapter.
MathError = (CpmmMathError, ClMathError)


def cl_supported(meta) -> bool:
    """True when a pool record is served by the concentrated-liquidity adapter."""
    return bool(meta.supported_by_clmm) and str(meta.model) in SUPPORTED_CLMM_MODELS


def cpmm_supported(meta) -> bool:
    """True when a pool record is served by the CPMM adapter."""
    return bool(meta.supported_by_cpmm) and PoolModel(meta.model) in SUPPORTED_CPMM_MODELS


def cl_state_from_pool(meta) -> ClPoolState | None:
    """Build the initial ``ClPoolState`` of a CL pool record, or None when it has no initial price.

    The tick map comes from ``initial_ticks`` (``[tick, liquidity_net, liquidity_gross]`` strings),
    the active liquidity from ``initial_liquidity`` and the tick from ``initial_tick`` when the
    collector recorded it (a swap can leave the on-chain tick one below ``getTickAtSqrtRatio`` when it
    stops exactly on a crossed boundary, so the recorded tick is authoritative).
    """
    if not cl_supported(meta) or meta.initial_sqrt_price_x96 is None:
        return None
    sqrt_price = int(meta.initial_sqrt_price_x96)
    tick = meta.initial_tick if meta.initial_tick is not None else get_tick_at_sqrt_ratio(sqrt_price)
    ticks = {int(t): (int(net), int(gross)) for t, net, gross in meta.initial_ticks}
    return ClPoolState(
        key=meta.key,
        asset0=meta.asset0,
        asset1=meta.asset1,
        fee_pips=int(meta.fee_pips or 0),
        tick_spacing=int(meta.tick_spacing or 1),
        sqrt_price_x96=sqrt_price,
        tick=int(tick),
        liquidity=int(meta.initial_liquidity or 0),
        ticks=ticks,
        model=str(meta.model),
    )


HOOKED_LAUNCH_WINDOW_MS = 120_000  # no fills on a hooked v4 pool in its first two minutes (launch MEV modules)


class SimulationError(RuntimeError):
    pass


class SubmitRejected(ValueError):
    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass(slots=True)
class RestrictionEvent:
    pool: str
    asset: str
    event_ms: int
    available_ms: int | None
    payload: dict


@dataclass(slots=True)
class EquityPoint:
    time_ms: int
    complete: bool
    equity: int | None
    cash: int
    priced: int
    no_route_assets: list[str]
    unpriced_assets: list[str]
    source: str  # "grid" | "ledger"


@dataclass(slots=True)
class ValuationResult:
    time_ms: int
    complete: bool
    cash_available: int
    cash_reserved: int
    cash_pending: int
    priced_value: int
    liquidation_gas: int
    holdings: list[dict[str, Any]]
    equity: int | None
    policy: str
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class FidelityFlag:
    time_ms: int
    pool: str
    code: str
    message: str


class Simulation:
    def __init__(
        self,
        pack: Pack,
        *,
        bankroll_raw: int,
        engine_seed: str,
        prehistory_events: bool = True,
    ) -> None:
        self.pack = pack
        self.params = pack.params
        m = pack.manifest
        self.start_utc_ms = m.period.start_utc_ms
        self.duration_ms = m.period.duration_ms
        self.end_ms = self.duration_ms
        self.numeraire = m.numeraire
        self.engine_seed = engine_seed
        self.now_ms = 0
        self.finished = False
        self.tail_processed = False
        self.gas_cost = self.params.gas_cost

        # Block schedule (relative time)
        if pack.blocks:
            self.schedule: BlockSchedule = TableSchedule([(b, t - self.start_utc_ms) for b, t in pack.blocks])
        else:
            if self.params.block_interval_ms is None:
                raise SimulationError("pack has no block table and no fixed block interval")
            first_block = min((p.created_block or 0) for p in pack.pools.values()) if pack.pools else 0
            pre_start = m.period.prehistory_start_utc_ms if m.period.prehistory_start_utc_ms is not None else self.start_utc_ms
            origin = pre_start - self.start_utc_ms
            tail_ms = self.params.settlement_tail_blocks * self.params.block_interval_ms
            last_block = first_block + (self.duration_ms - origin + tail_ms) // self.params.block_interval_ms + 2
            self.schedule = FixedIntervalSchedule(first=first_block, origin_ms=origin, interval_ms=self.params.block_interval_ms, last=last_block)

        # Market state
        self.pools: dict[str, PoolState] = {}
        self.ref_pools: dict[str, PoolState] = {}
        self.pool_meta = pack.pools
        self.assets = pack.assets
        self.fidelity_failed: dict[str, str] = {}
        for key, p in pack.pools.items():
            st: PoolState | None = None
            if cpmm_supported(p):
                if p.initial_reserve0 is None or p.initial_reserve1 is None:
                    continue
                st = CpmmPoolState(
                    key=key,
                    asset0=p.asset0,
                    asset1=p.asset1,
                    reserve0=int(p.initial_reserve0),
                    reserve1=int(p.initial_reserve1),
                    fee_num=p.fee_numerator,
                    fee_den=p.fee_denominator,
                    model=PoolModel(p.model),
                )
            elif cl_supported(p):
                # A CL pool initialized inside the window has no initial price; its state is created
                # by the cl_init tape row and it is not tradable before that.
                try:
                    st = cl_state_from_pool(p)
                except ClMathError as e:
                    raise SimulationError(f"pool {key} has an invalid concentrated-liquidity initial state: {e}") from e
            if st is None:
                continue
            self.pools[key] = st
            self.ref_pools[key] = st.copy()
        self.state_version = 0

        # Observations
        self.obs: dict[str, PoolObservations] = {k: PoolObservations(k) for k in pack.pools}
        self._load_coverage()
        self.restriction_events: list[RestrictionEvent] = []
        self.pool_discovery_ms: dict[str, int] = {}
        for key, p in pack.pools.items():
            disc = p.discovery_available_utc_ms if p.discovery_available_utc_ms is not None else (p.created_time_utc_ms or self.start_utc_ms)
            self.pool_discovery_ms[key] = disc - self.start_utc_ms

        # Tape
        self.tape: list[RelEvent] = to_relative(pack.tape, self.start_utc_ms)
        self.cursor = 0
        # Pools whose state is created by a cl_init row on the tape: key -> time of that row.
        self.cl_init_ms: dict[str, int] = {}
        for ev in self.tape:
            if ev.kind == "cl_init" and ev.pool not in self.cl_init_ms:
                self.cl_init_ms[ev.pool] = ev.time_ms
        self.reconciliation_mismatches: list[dict[str, Any]] = []
        self.reserve_adjustments: dict[str, int] = defaultdict(int)  # explained / material checkpoint corrections
        self.fidelity_flags: list[FidelityFlag] = []

        # Broker
        self.ledger = Ledger()
        self.orders: dict[str, Order] = {}
        self.orders_by_key: dict[str, Order] = {}
        self.order_seq = 0
        self.quotes: dict[str, Quote] = {}
        self.quote_seq = 0
        self.inclusions: dict[int, list[Order]] = defaultdict(list)
        self.confirmations: dict[int, list[Order]] = defaultdict(list)
        self.trade_seq = 0
        self.events_processed = 0
        self.equity_points: list[EquityPoint] = []
        self.next_grid_ms = 0
        self.bankroll_raw = bankroll_raw
        self.ledger.post(
            0, "bankroll", "init", [Leg(SOURCE_BANKROLL, self.numeraire, -bankroll_raw), Leg(AGENT_AVAILABLE, self.numeraire, bankroll_raw)]
        )

        # Process prehistory tape so the private state equals the reference at t=0.
        if prehistory_events:
            self._process_tape_until(-1, include_equal=True)
            self.now_ms = 0
        self._grid_snapshot(0)
        self.next_grid_ms = self.params.reporting_grid_ms

    # ------------------------------------------------------------------ setup helpers
    def _load_coverage(self) -> None:
        cov = self.pack.coverage or {}
        for row in cov.get("intervals", []):
            if row.get("field") != "swaps":
                continue
            pool = row.get("object_ref")
            if pool not in self.obs:
                continue
            self.obs[pool].coverage.append(
                CoverageSpan(
                    start_ms=int(row["start_utc_ms"]) - self.start_utc_ms,
                    end_ms=int(row["end_utc_ms"]) - self.start_utc_ms,
                    state=CoverageState(row["state"]),
                    evidence=str(row.get("evidence", "")),
                )
            )

    # ------------------------------------------------------------------ time
    def process_until(self, target_ms: int) -> int:
        """Advance virtual time to ``target_ms`` processing every event in deterministic order."""
        if target_ms < self.now_ms:
            raise SimulationError("cannot move time backwards")
        limit = self.end_ms if not self.finished else self._tail_end_ms()
        target = min(target_ms, limit) if not self.finished else min(target_ms, limit)
        while True:
            nxt = self._next_event_time()
            if nxt > target:
                break
            self._process_time(nxt)
        self.now_ms = max(self.now_ms, target)
        return self.now_ms

    def _tail_end_ms(self) -> int:
        try:
            last_ep_block = self.schedule.block_containing(self.end_ms)
            return self.schedule.time_of(min(last_ep_block + self.params.settlement_tail_blocks, self.schedule.last_block))
        except BlockScheduleError:
            return self.end_ms

    def _next_event_time(self) -> int:
        nxt = INF
        if self.cursor < len(self.tape):
            nxt = min(nxt, self.tape[self.cursor].time_ms)
        if self.inclusions:
            nxt = min(nxt, min(self.schedule.time_of(b) for b in self.inclusions))
        if self.confirmations:
            nxt = min(nxt, min(self.schedule.time_of(b) for b in self.confirmations))
        if self.next_grid_ms <= self.end_ms:
            nxt = min(nxt, self.next_grid_ms)
        return nxt

    def _process_time(self, t: int) -> None:
        # 1. tape events at time t (whole blocks are processed in order)
        self._process_tape_until(t, include_equal=True)
        # 2. inclusions / confirmations for blocks at time t
        blocks_here = sorted(b for b in list(self.inclusions) + list(self.confirmations) if self.schedule.time_of(b) == t)
        for b in sorted(set(blocks_here)):
            for order in self.inclusions.pop(b, []):
                self._include(order, b, t)
            for order in self.confirmations.pop(b, []):
                self._confirm(order, b, t)
        self.now_ms = t
        # 3. reporting grid
        while self.next_grid_ms <= t and self.next_grid_ms <= self.end_ms:
            self._grid_snapshot(self.next_grid_ms)
            self.next_grid_ms += self.params.reporting_grid_ms

    def _process_tape_until(self, t: int, include_equal: bool) -> None:
        n = len(self.tape)
        while self.cursor < n:
            ev = self.tape[self.cursor]
            if ev.time_ms > t or (ev.time_ms == t and not include_equal):
                break
            self._apply_external(ev)
            self.cursor += 1
            self.events_processed += 1

    # ------------------------------------------------------------------ external flow
    def _apply_external(self, ev: RelEvent) -> None:
        pool = self.pools.get(ev.pool)
        ref = self.ref_pools.get(ev.pool)
        if ev.kind == "swap":
            if not isinstance(pool, CpmmPoolState) or not isinstance(ref, CpmmPoolState) or ev.asset_in is None or ev.amount_in is None:
                return
            # Reference state: recorded output exactly.
            rec_out = ev.amount_out_recorded
            try:
                ref_max = ref.max_out(ev.asset_in, ev.amount_in)
            except CpmmMathError as e:
                self._flag(ev, "REFERENCE_SWAP_INVALID", str(e))
                return
            if rec_out is None:
                rec_out = ref_max
            if rec_out > ref_max:
                self._flag(ev, "RECORDED_OUTPUT_EXCEEDS_MODEL", f"recorded {rec_out} > model max {ref_max}")
                rec_out = ref_max
            ratio = Fraction(rec_out, ref_max) if ref_max and rec_out != ref_max else None
            ev.output_ratio = ratio
            self._apply_recorded_to_ref(ref, ev.asset_in, ev.amount_in, rec_out)
            # Private state: fixed intent, recomputed output.
            if ev.pool in self.fidelity_failed:
                return
            try:
                out = pool.apply_swap(ev.asset_in, ev.amount_in, ratio)
            except CpmmMathError as e:
                self._flag(ev, "EXTERNAL_SWAP_FAILED_ON_PRIVATE_STATE", str(e))
                return
            self.state_version += 1
            if ev.available_ms is not None:
                self._record_trade(ev, pool, ev.asset_in, ev.amount_in, out, origin="external")
        elif ev.kind == "mint":
            if not isinstance(pool, CpmmPoolState) or not isinstance(ref, CpmmPoolState):
                return
            ref.apply_mint(ev.amount0 or 0, ev.amount1 or 0)
            if ev.pool not in self.fidelity_failed:
                pool.apply_mint(ev.amount0 or 0, ev.amount1 or 0)
                self.state_version += 1
        elif ev.kind == "burn":
            if not isinstance(pool, CpmmPoolState) or not isinstance(ref, CpmmPoolState):
                return
            try:
                ref.apply_burn(ev.amount0 or 0, ev.amount1 or 0)
            except FidelityLimit as e:
                self._flag(ev, "REFERENCE_BURN_OVERDRAW", str(e))
            if ev.pool not in self.fidelity_failed:
                try:
                    pool.apply_burn(ev.amount0 or 0, ev.amount1 or 0)
                    self.state_version += 1
                except FidelityLimit as e:
                    self.fidelity_failed[ev.pool] = "ENVIRONMENT_FIDELITY_LIMIT"
                    self._flag(ev, "ENVIRONMENT_FIDELITY_LIMIT", str(e))
        elif ev.kind == "adjust":
            if not isinstance(pool, CpmmPoolState) or not isinstance(ref, CpmmPoolState):
                return
            d0, d1 = ev.amount0 or 0, ev.amount1 or 0
            try:
                ref.apply_adjust(d0, d1)
            except FidelityLimit as e:
                self._flag(ev, "REFERENCE_ADJUST_OVERDRAW", str(e))
            self._adjust_private(ev, pool, d0, d1)
        elif ev.kind == "sync":
            if not isinstance(pool, CpmmPoolState) or not isinstance(ref, CpmmPoolState) or ev.reserve0 is None or ev.reserve1 is None:
                return
            # The chain is the truth: the reference re-anchors to every Sync and the private state
            # takes the same correction, so one unrecorded transfer never compounds into drift.
            d0, d1 = ref.anchor_to_sync(ev.reserve0, ev.reserve1)
            cls = classify_checkpoint_delta(d0, d1, ev.reserve0, ev.reserve1, explained=bool(ev.payload.get("orphan")))
            if cls == "match":
                return
            self.reserve_adjustments[cls] += 1
            if cls == "material":
                self.reconciliation_mismatches.append({"seq": ev.seq, "pool": ev.pool, "block": ev.block, "delta0": d0, "delta1": d1})
            self._adjust_private(ev, pool, -d0, -d1)
        elif ev.kind == "cl_init":
            self._apply_cl_init(ev)
        elif ev.kind == "cl_modify":
            if not isinstance(ref, ClPoolState) or ev.tick_lower is None or ev.tick_upper is None or ev.liquidity_delta is None:
                return
            assert isinstance(pool, ClPoolState)
            try:
                ref.apply_modify_liquidity(ev.tick_lower, ev.tick_upper, ev.liquidity_delta)
            except ClMathError as e:
                self._flag(ev, "REFERENCE_MODIFY_INVALID", str(e))
            if ev.pool not in self.fidelity_failed:
                try:
                    pool.apply_modify_liquidity(ev.tick_lower, ev.tick_upper, ev.liquidity_delta)
                    self.state_version += 1
                except ClMathError as e:
                    # e.g. a burn the private tick map cannot honour after agent flow (``LS``): the
                    # counterfactual left the model's validated domain, like a CPMM burn overdraw.
                    self.fidelity_failed[ev.pool] = "ENVIRONMENT_FIDELITY_LIMIT"
                    self._flag(ev, "ENVIRONMENT_FIDELITY_LIMIT", f"counterfactual liquidity change cannot be applied to the private state of {ev.pool}: {e}")
        elif ev.kind == "cl_swap":
            self._apply_cl_swap(ev)
        elif ev.kind == "halt":
            if pool is not None:
                pool.halted = True
                ref.halted = True  # type: ignore[union-attr]
                self.state_version += 1
        elif ev.kind == "unhalt":
            if pool is not None:
                pool.halted = False
                ref.halted = False  # type: ignore[union-attr]
                self.state_version += 1
        elif ev.kind == "restriction":
            asset = ev.payload.get("asset")
            if pool is not None and asset:
                pool.restrictions.setdefault(asset, {}).update({k: v for k, v in ev.payload.items() if k != "asset"})
                ref.restrictions.setdefault(asset, {}).update({k: v for k, v in ev.payload.items() if k != "asset"})  # type: ignore[union-attr]
                self.restriction_events.append(
                    RestrictionEvent(pool=ev.pool, asset=asset, event_ms=ev.time_ms, available_ms=ev.available_ms, payload=dict(ev.payload))
                )
                self.state_version += 1
        elif ev.kind == "discovery":
            pass

    def _apply_cl_init(self, ev: RelEvent) -> None:
        """``Initialize``: create the reference and private states of a pool that starts inside the window."""
        meta = self.pool_meta.get(ev.pool)
        if meta is None or not cl_supported(meta) or ev.sqrt_price_x96 is None:
            return
        if ev.pool in self.pools:
            self._flag(ev, "CL_ALREADY_INITIALIZED", "Initialize observed for a pool that already has a state; ignored")
            return
        tick = ev.tick if ev.tick is not None else get_tick_at_sqrt_ratio(ev.sqrt_price_x96)
        try:
            st = ClPoolState(
                key=ev.pool,
                asset0=meta.asset0,
                asset1=meta.asset1,
                fee_pips=int(meta.fee_pips or 0),
                tick_spacing=int(meta.tick_spacing or 1),
                sqrt_price_x96=ev.sqrt_price_x96,
                tick=int(tick),
                model=str(meta.model),
            )
        except ClMathError as e:
            self._flag(ev, "REFERENCE_INIT_INVALID", str(e))
            return
        self.ref_pools[ev.pool] = st
        self.pools[ev.pool] = st.copy()
        self.state_version += 1

    def _apply_cl_swap(self, ev: RelEvent) -> None:
        """Recorded ``Swap`` on a CL pool: the reference replays and reconciles the row (every swap is a
        checkpoint, the CL counterpart of a v2 Sync); the private copy re-executes the same intent."""
        pool = self.pools.get(ev.pool)
        ref = self.ref_pools.get(ev.pool)
        if not isinstance(ref, ClPoolState) or not isinstance(pool, ClPoolState):
            return
        if ev.amount0 is None or ev.amount1 is None or ev.sqrt_price_x96_after is None or ev.liquidity_after is None or ev.tick_after is None:
            return
        try:
            rec = ref.apply_recorded_swap(ev.amount0, ev.amount1, ev.sqrt_price_x96_after, ev.liquidity_after, ev.tick_after, ev.fee_pips)
        except ClMathError as e:
            self._flag(ev, "REFERENCE_SWAP_INVALID", str(e))
            return
        if rec["mode"] == "anchored_degenerate":
            # A swap the loop cannot replay (a hook absorbed a leg): the reference was anchored to the
            # chain; the private copy takes the same correction. Nothing to fill, nothing to observe.
            self.reserve_adjustments["explained"] += 1
            if ev.pool not in self.fidelity_failed and (rec["sqrt_price_x96"] or rec["liquidity"]):
                try:
                    pool.apply_state_delta(-int(rec["sqrt_price_x96"]), -int(rec["liquidity"]))
                    self.state_version += 1
                except ClMathError as e:
                    self.fidelity_failed[ev.pool] = "ENVIRONMENT_FIDELITY_LIMIT"
                    self._flag(ev, "ENVIRONMENT_FIDELITY_LIMIT", str(e))
            return
        if rec["sqrt_price_x96"] != 0 or rec["liquidity"] != 0 or rec["tick"] != 0:
            self.reconciliation_mismatches.append(
                {
                    "seq": ev.seq,
                    "pool": ev.pool,
                    "block": ev.block,
                    "delta_sqrt_price_x96": rec["sqrt_price_x96"],
                    "delta_liquidity": rec["liquidity"],
                    "delta_tick": rec["tick"],
                    "delta_amount0": rec["amount0"],
                    "delta_amount1": rec["amount1"],
                    "mode": rec["mode"],
                }
            )
            self.reserve_adjustments["material"] += 1
            ref.anchor_to_recorded(ev.sqrt_price_x96_after, ev.liquidity_after, ev.tick_after)  # the chain is the truth
        # Private state: the recorded intent (exact input of the recorded input amount, or exact output
        # when that is what reproduced the event) with the fee the event reported.
        if ev.pool in self.fidelity_failed:
            return
        zero_for_one = ev.amount0 > 0
        amount_in = ev.amount0 if zero_for_one else ev.amount1
        amount_out = -(ev.amount1 if zero_for_one else ev.amount0)
        specified = -amount_out if rec["mode"] == "exact_out" else amount_in
        try:
            actual_in, actual_out = pool.apply_intent(zero_for_one, specified, ev.fee_pips)
        except ClMathError as e:
            self._flag(ev, "EXTERNAL_SWAP_FAILED_ON_PRIVATE_STATE", str(e))
            return
        self.state_version += 1
        if ev.available_ms is not None:
            self._record_trade(ev, pool, pool.asset0 if zero_for_one else pool.asset1, actual_in, actual_out, origin="external")

    def _adjust_private(self, ev: RelEvent, pool: CpmmPoolState, d0: int, d1: int) -> None:
        if ev.pool in self.fidelity_failed or (d0 == 0 and d1 == 0):
            return
        try:
            pool.apply_adjust(d0, d1)
            self.state_version += 1
        except FidelityLimit as e:
            self.fidelity_failed[ev.pool] = "ENVIRONMENT_FIDELITY_LIMIT"
            self._flag(ev, "ENVIRONMENT_FIDELITY_LIMIT", str(e))

    @staticmethod
    def _apply_recorded_to_ref(ref: CpmmPoolState, asset_in: str, amount_in: int, out: int) -> None:
        if asset_in == ref.asset0:
            ref.reserve0 += amount_in
            ref.reserve1 -= out
        else:
            ref.reserve1 += amount_in
            ref.reserve0 -= out

    def _flag(self, ev: RelEvent, code: str, message: str) -> None:
        self.fidelity_flags.append(FidelityFlag(time_ms=ev.time_ms, pool=ev.pool, code=code, message=message))

    @staticmethod
    def _state_after(pool: PoolState) -> dict[str, Any]:
        """TradeObs fields describing the pool after a trade. For a CL pool ``reserve0/1_after`` carry the
        virtual depths of the active range so reserve-based consumers keep working."""
        if isinstance(pool, ClPoolState):
            x, y = pool.virtual_reserves()
            return {"reserve0_after": x, "reserve1_after": y, "sqrt_price_x96_after": pool.sqrt_price_x96, "tick_after": pool.tick, "liquidity_after": pool.liquidity}
        return {"reserve0_after": pool.reserve0, "reserve1_after": pool.reserve1}

    def _record_trade(self, ev: RelEvent, pool: PoolState, asset_in: str, amount_in: int, out: int, origin: str, order_id: str | None = None) -> None:
        self.trade_seq += 1
        available = ev.available_ms if ev.available_ms is not None else ev.time_ms + self.params.availability_delay_ms
        self.obs[ev.pool].append(
            TradeObs(
                seq=self.trade_seq,
                pool=ev.pool,
                event_ms=ev.time_ms,
                available_ms=available,
                block=ev.block,
                log_index=ev.log_index,
                tx=ev.tx,
                wallet=ev.wallet,
                asset_in=asset_in,
                asset_out=pool.other(asset_in),
                amount_in=amount_in,
                amount_out=out,
                origin=origin,
                order_id=order_id,
                **self._state_after(pool),
            )
        )

    # ------------------------------------------------------------------ broker: quote
    def quote(self, pool_key: str, asset_in: str, amount_in: int) -> Quote:
        pool = self._tradable_pool(pool_key)
        if asset_in not in (pool.asset0, pool.asset1):
            raise SubmitRejected("INVALID_ORDER", "asset_in is not in the pool")
        if amount_in <= 0:
            raise SubmitRejected("INVALID_ORDER", "amount_in must be positive")
        asset_out = pool.other(asset_in)
        rin, rout = pool.depth_for(asset_in)
        try:
            out = pool.max_out(asset_in, amount_in)
        except MathError as e:
            raise SubmitRejected("NO_ROUTE", f"pool cannot fill: {e}") from e
        cap_ok, cap_reason = self._capacity_check(pool, asset_in, amount_in, out)
        self.quote_seq += 1
        q = Quote(
            quote_id=f"q_{self.quote_seq:06d}",
            created_ms=self.now_ms,
            expires_ms=self.now_ms + self.params.quote_ttl_ms,
            pool=pool_key,
            asset_in=asset_in,
            asset_out=asset_out,
            amount_in=amount_in,
            amount_out=out,
            reserve_in=rin,
            reserve_out=rout,
            fee_num=pool.fee_num,
            fee_den=pool.fee_den,
            gas_cost=self.gas_cost,
            state_version=self.state_version,
            capacity_ok=cap_ok,
            capacity_reason=cap_reason,
        )
        self.quotes[q.quote_id] = q
        return q

    def _tradable_pool(self, pool_key: str) -> PoolState:
        meta = self.pool_meta.get(pool_key)
        if meta is None:
            raise SubmitRejected("NOT_YET_DISCOVERED", "identifier is not currently discoverable in this session")
        if self.pool_discovery_ms.get(pool_key, INF) > self.now_ms:
            raise SubmitRejected("NOT_YET_DISCOVERED", "identifier is not currently discoverable in this session")
        if pool_key in self.fidelity_failed:
            raise SubmitRejected(
                "ENVIRONMENT_FIDELITY_LIMIT", "this market left the model's validated domain; no execution is simulated"
            )
        pool = self.pools.get(pool_key)
        if pool is None:
            if not cpmm_supported(meta) and not cl_supported(meta):
                raise SubmitRejected("UNSUPPORTED_CAPABILITY", f"pool mechanics not supported: {meta.unsupported_reason or meta.model}")
            if self.cl_init_ms.get(pool_key, -INF) > self.now_ms:
                # Initialized later in the window: the pool exists but has no state yet.
                raise SubmitRejected("NOT_YET_DISCOVERED", "identifier is not currently discoverable in this session")
            raise SubmitRejected("MISSING_DATA", "pool has no executable initial state in this pack")
        if pool.halted:
            raise SubmitRejected("NO_ROUTE", "no modeled route: pool trading is halted")
        launch = self._launch_window_reason(pool_key, self.now_ms)
        if launch:
            raise SubmitRejected("NO_ROUTE", f"no modeled route: {launch}")
        return pool

    def _launch_window_reason(self, pool_key: str, t: int) -> str | None:
        """Hooked v4 pools (Clanker and similar launchers) run MEV modules right after initialization:
        a block delay or a sniper auction with fees decaying from about 80%. Neither is modelled, so
        the engine offers no route in that window instead of pretending a plain fill was possible."""
        meta = self.pool_meta.get(pool_key)
        init = self.cl_init_ms.get(pool_key)
        if meta is None or not meta.hooks or init is None or t >= init + HOOKED_LAUNCH_WINDOW_MS:
            return None
        return f"hooked pool inside its launch window ({HOOKED_LAUNCH_WINDOW_MS // 1000} s after initialization; MEV module semantics not modelled)"

    def _capacity_check(self, pool: PoolState, asset_in: str, amount_in: int, out: int) -> tuple[bool, str | None]:
        """Capacity profile on the pool depth (``depth_for``).

        CPMM: the depth is the reserve pair and the post-trade depth is ``(rin + in, rout - out)``.
        CL: the depth is the virtual reserves of the active range, the post-trade depth is the virtual
        reserves after the swap on a copy, and the reference-displacement check compares those virtual
        depths with the no-agent state's; liquidity outside the active range is not counted.
        """
        cap = self.params.capacity
        rin, rout = pool.depth_for(asset_in)
        if amount_in * 10_000 > rin * cap.max_input_bps_of_reserve:
            return False, f"input exceeds {cap.max_input_bps_of_reserve} bps of current input reserve ({cap.version})"
        ref = self.ref_pools.get(pool.key)
        if ref is not None:
            if isinstance(pool, ClPoolState):
                trial = pool.copy()
                trial.apply_swap(asset_in, amount_in)
                new_in, new_out = trial.depth_for(asset_in)
            else:
                new_in = rin + amount_in
                new_out = rout - out
            ref_in, ref_out = ref.depth_for(asset_in)
            for new, refv in ((new_in, ref_in), (new_out, ref_out)):
                if refv <= 0:
                    continue
                if abs(new - refv) * 10_000 > refv * cap.max_cumulative_displacement_bps:
                    return False, (
                        f"cumulative reserve displacement would exceed {cap.max_cumulative_displacement_bps} bps "
                        f"from the no-agent state ({cap.version})"
                    )
        return True, None

    # ------------------------------------------------------------------ broker: submit
    def submit(
        self,
        *,
        pool_key: str,
        asset_in: str,
        asset_out: str,
        amount_in: int,
        min_amount_out: int,
        deadline_ms: int,
        idempotency_key: str,
        quote_id: str | None = None,
    ) -> tuple[Order, bool]:
        """Returns (order, created). Repeated idempotency key with same payload returns original."""
        payload = {
            "pool": pool_key,
            "asset_in": asset_in,
            "asset_out": asset_out,
            "amount_in": amount_in,
            "min_amount_out": min_amount_out,
            "deadline_ms": deadline_ms,
            "quote_id": quote_id,
        }
        ph = payload_hash(payload)
        existing = self.orders_by_key.get(idempotency_key)
        if existing is not None:
            if existing.payload_hash != ph:
                raise SubmitRejected("IDEMPOTENCY_CONFLICT", "idempotency key reused with a different payload")
            return existing, False
        if self.finished or self.now_ms >= self.end_ms:
            raise SubmitRejected("EPISODE_ENDED", "the episode has ended; no new orders are accepted")
        if amount_in <= 0 or min_amount_out < 0:
            raise SubmitRejected("INVALID_ORDER", "amount_in must be positive and min_amount_out non-negative")
        if deadline_ms <= self.now_ms:
            raise SubmitRejected("INVALID_ORDER", "deadline_ms must be after the current virtual time")
        pool = self._tradable_pool(pool_key)
        if asset_in not in (pool.asset0, pool.asset1) or pool.other(asset_in) != asset_out:
            raise SubmitRejected("INVALID_ORDER", "asset pair does not match the pool")
        if quote_id is not None:
            q = self.quotes.get(quote_id)
            if q is None:
                raise SubmitRejected("INVALID_ORDER", "unknown quote_id")
            if q.expires_ms < self.now_ms:
                raise SubmitRejected("QUOTE_EXPIRED", "referenced quote has expired", {"expired_ms": q.expires_ms})
        blocked = pool.transfer_blocked(asset_in, "sell") or pool.transfer_blocked(asset_out, "buy")
        if blocked:
            raise SubmitRejected("NO_ROUTE", f"no modeled route: {blocked}")
        try:
            out_now = pool.max_out(asset_in, amount_in)
        except MathError as e:
            raise SubmitRejected("NO_ROUTE", f"pool cannot fill: {e}") from e
        cap_ok, cap_reason = self._capacity_check(pool, asset_in, amount_in, out_now)
        if not cap_ok:
            raise SubmitRejected("MODEL_CAPACITY_LIMIT", cap_reason or "capacity limit", {"profile": self.params.capacity.version})
        # Reserve principal and gas atomically.
        gas = self.gas_cost
        legs: list[Leg] = []
        need = defaultdict(int)
        need[asset_in] += amount_in
        need[self.numeraire] += gas
        for asset, amt in need.items():
            if amt <= 0:
                continue
            if self.ledger.balance(AGENT_AVAILABLE, asset) < amt:
                raise SubmitRejected(
                    "INSUFFICIENT_FUNDS",
                    "available balance cannot cover principal plus gas allowance",
                    {"asset": asset, "required_raw": str(amt), "available_raw": str(self.ledger.balance(AGENT_AVAILABLE, asset))},
                )
            legs.append(Leg(AGENT_AVAILABLE, asset, -amt))
            legs.append(Leg(AGENT_RESERVED, asset, amt))
        self.order_seq += 1
        order_id = f"ord_{self.order_seq:06d}"
        ready = self.now_ms + self.params.submit_latency_ms
        try:
            blk = self.schedule.block_at_or_after(ready)
            blk_time = self.schedule.time_of(blk)
        except BlockScheduleError as e:
            raise SubmitRejected("EPISODE_ENDED", f"no block available for inclusion: {e}") from e
        order = Order(
            order_id=order_id,
            idempotency_key=idempotency_key,
            payload_hash=ph,
            pool=pool_key,
            asset_in=asset_in,
            asset_out=asset_out,
            amount_in=amount_in,
            min_amount_out=min_amount_out,
            deadline_ms=deadline_ms,
            submitted_ms=self.now_ms,
            ready_ms=ready,
            inclusion_block=blk,
            inclusion_time_ms=blk_time,
            reserved_gas=gas,
            quote_id=quote_id,
        )
        order.transition(self.now_ms, OrderState.RECEIVED)
        try:
            self.ledger.post(self.now_ms, "reserve", order_id, legs, note="principal + gas allowance reserved")
        except LedgerError as e:
            raise SubmitRejected("INSUFFICIENT_FUNDS", str(e)) from e
        order.transition(self.now_ms, OrderState.ACCEPTED_AND_RESERVED)
        order.transition(self.now_ms, OrderState.PENDING_INCLUSION)
        self.orders[order_id] = order
        self.orders_by_key[idempotency_key] = order
        self.inclusions[blk].append(order)
        return order, True

    def _release(self, order: Order, t: int, cause: str, note: str) -> None:
        legs = [
            Leg(AGENT_RESERVED, order.asset_in, -order.amount_in),
            Leg(AGENT_AVAILABLE, order.asset_in, order.amount_in),
        ]
        if order.reserved_gas:
            legs += [Leg(AGENT_RESERVED, self.numeraire, -order.reserved_gas), Leg(AGENT_AVAILABLE, self.numeraire, order.reserved_gas)]
        self.ledger.post(t, cause, order.order_id, legs, note=note)

    def _revert(self, order: Order, t: int, block: int, reason: str) -> None:
        """Included but reverted: principal returned, gas charged."""
        legs = [
            Leg(AGENT_RESERVED, order.asset_in, -order.amount_in),
            Leg(AGENT_AVAILABLE, order.asset_in, order.amount_in),
        ]
        if order.reserved_gas:
            legs += [Leg(AGENT_RESERVED, self.numeraire, -order.reserved_gas), Leg(SINK_GAS, self.numeraire, order.reserved_gas)]
            order.gas_charged = order.reserved_gas
        self.ledger.post(t, "revert_gas", order.order_id, legs, note=f"reverted: {reason}")
        order.fill_block = block
        order.fill_time_ms = t
        order.transition(t, OrderState.REVERTED, reason)

    def _include(self, order: Order, block: int, t: int) -> None:
        if order.state != OrderState.PENDING_INCLUSION:
            return
        if t > order.deadline_ms:
            self._release(order, t, "expire", "deadline passed before inclusion")
            order.transition(t, OrderState.EXPIRED, "deadline passed before inclusion")
            return
        pool = self.pools.get(order.pool)
        if pool is None or order.pool in self.fidelity_failed:
            self._release(order, t, "release", "environment fidelity limit at inclusion")
            order.transition(t, OrderState.MODEL_CAPACITY_REJECTED, "ENVIRONMENT_FIDELITY_LIMIT")
            return
        if pool.halted:
            self._revert(order, t, block, "NO_ROUTE: pool halted at inclusion")
            return
        launch = self._launch_window_reason(order.pool, t)
        if launch:
            self._revert(order, t, block, f"NO_ROUTE: {launch}")
            return
        blocked = pool.transfer_blocked(order.asset_in, "sell") or pool.transfer_blocked(order.asset_out, "buy")
        if blocked:
            self._revert(order, t, block, f"NO_ROUTE: {blocked}")
            return
        try:
            out = pool.max_out(order.asset_in, order.amount_in)
        except MathError as e:
            self._revert(order, t, block, f"NO_ROUTE: {e}")
            return
        cap_ok, cap_reason = self._capacity_check(pool, order.asset_in, order.amount_in, out)
        if not cap_ok:
            self._release(order, t, "release", f"model capacity limit at inclusion: {cap_reason}")
            order.transition(t, OrderState.MODEL_CAPACITY_REJECTED, cap_reason)
            return
        if out < order.min_amount_out:
            self._revert(order, t, block, f"SLIPPAGE_LIMIT: output {out} below min {order.min_amount_out}")
            return
        # Fill on the private state.
        try:
            actual = pool.apply_swap(order.asset_in, order.amount_in)
        except MathError as e:
            self._revert(order, t, block, f"NO_ROUTE: {e}")
            return
        assert actual == out
        self.state_version += 1
        legs = [
            Leg(AGENT_RESERVED, order.asset_in, -order.amount_in),
            Leg(f"pool.{order.pool}", order.asset_in, order.amount_in),
            Leg(f"pool.{order.pool}", order.asset_out, -out),
            Leg(AGENT_PENDING, order.asset_out, out),
        ]
        if order.reserved_gas:
            legs += [Leg(AGENT_RESERVED, self.numeraire, -order.reserved_gas), Leg(SINK_GAS, self.numeraire, order.reserved_gas)]
            order.gas_charged = order.reserved_gas
        self.ledger.post(t, "fill", order.order_id, legs, note="included and filled; awaiting confirmation")
        order.amount_out = out
        order.fill_block = block
        order.fill_time_ms = t
        order.transition(t, OrderState.FILLED_PENDING_CONFIRMATION)
        cb = block + self.params.confirm_blocks
        try:
            self.schedule.time_of(cb)
        except BlockScheduleError:
            cb = self.schedule.last_block
        order.confirm_block = cb
        self.confirmations[cb].append(order)
        # Own trade becomes an observation like any other, with the same availability delay.
        self.trade_seq += 1
        self.obs[order.pool].append(
            TradeObs(
                seq=self.trade_seq,
                pool=order.pool,
                event_ms=t,
                available_ms=t + self.params.availability_delay_ms,
                block=block,
                log_index=1_000_000 + self.order_seq,
                tx=f"own:{order.order_id}",
                wallet="own",
                asset_in=order.asset_in,
                asset_out=order.asset_out,
                amount_in=order.amount_in,
                amount_out=out,
                origin="own",
                order_id=order.order_id,
                **self._state_after(pool),
            )
        )
        self._ledger_equity_point(t)

    def _confirm(self, order: Order, block: int, t: int) -> None:
        if order.state != OrderState.FILLED_PENDING_CONFIRMATION or order.amount_out is None:
            return
        self.ledger.post(
            t,
            "confirm",
            order.order_id,
            [Leg(AGENT_PENDING, order.asset_out, -order.amount_out), Leg(AGENT_AVAILABLE, order.asset_out, order.amount_out)],
            note="fill confirmed",
        )
        order.confirm_time_ms = t
        order.transition(t, OrderState.CONFIRMED)

    # ------------------------------------------------------------------ valuation (non-mutating branch)
    def _holdings(self) -> dict[str, int]:
        h: dict[str, int] = defaultdict(int)
        for acct in (AGENT_AVAILABLE, AGENT_RESERVED, AGENT_PENDING):
            for asset, v in self.ledger.balances_of(acct).items():
                if asset != self.numeraire and v > 0:
                    h[asset] += v
        return dict(h)

    def _liquidation_pool_for(self, asset: str) -> str | None:
        best: tuple[int, str] | None = None
        for key, pool in self.pools.items():
            if key in self.fidelity_failed:
                continue
            if self.pool_discovery_ms.get(key, INF) > self.now_ms:
                continue
            if asset in (pool.asset0, pool.asset1) and pool.other(asset) == self.numeraire:
                rn, _ = pool.depth_for(self.numeraire)
                cand = (rn, key)
                if best is None or cand > best:
                    best = cand
        return best[1] if best else None

    def value_portfolio(self, time_ms: int | None = None) -> ValuationResult:
        t = self.now_ms if time_ms is None else time_ms
        cash_av = self.ledger.balance(AGENT_AVAILABLE, self.numeraire)
        cash_rs = self.ledger.balance(AGENT_RESERVED, self.numeraire)
        cash_pd = self.ledger.balance(AGENT_PENDING, self.numeraire)
        branch: dict[str, PoolState] = {}
        holdings_out: list[dict[str, Any]] = []
        priced = 0
        gas_total = 0
        complete = True
        warnings: list[str] = []
        holdings = self._holdings()
        for asset in sorted(holdings):
            qty = holdings[asset]
            pk = self._liquidation_pool_for(asset)
            meta_pool = None
            for _key, m in self.pool_meta.items():
                if asset in (m.asset0, m.asset1):
                    meta_pool = m
                    break
            if pk is None:
                if meta_pool is not None and (not (cpmm_supported(meta_pool) or cl_supported(meta_pool)) or meta_pool.key in self.fidelity_failed or meta_pool.key not in self.pools):
                    cls = ValuationClass.UNPRICED_MISSING_DATA
                    complete = False
                else:
                    cls = ValuationClass.NO_ROUTE
                holdings_out.append({"asset": asset, "quantity": qty, "class": str(cls), "value": 0, "pool": None, "reason": "no supported pool to the numeraire" if cls == ValuationClass.NO_ROUTE else "unsupported mechanics or missing state"})
                continue
            pool = branch.get(pk)
            if pool is None:
                pool = self.pools[pk].copy()
                branch[pk] = pool
            if pool.halted or pool.transfer_blocked(asset, "sell"):
                holdings_out.append({"asset": asset, "quantity": qty, "class": str(ValuationClass.NO_ROUTE), "value": 0, "pool": pk, "reason": "halted or sell-blocked under fixture rules"})
                continue
            try:
                out = pool.apply_swap(asset, qty)
            except MathError as e:
                holdings_out.append({"asset": asset, "quantity": qty, "class": str(ValuationClass.NO_ROUTE), "value": 0, "pool": pk, "reason": f"model cannot fill: {e}"})
                continue
            rin, _ = self.pools[pk].depth_for(asset)
            if qty * 10_000 > rin * self.params.capacity.max_input_bps_of_reserve:
                warnings.append(f"liquidation of {asset} exceeds the capacity profile; value is model output, not validated")
            net = out - self.gas_cost
            gas_total += self.gas_cost
            priced += out
            holdings_out.append({"asset": asset, "quantity": qty, "class": str(ValuationClass.PRICED_LIQUIDATABLE), "value": out, "net_value": net, "pool": pk, "reason": None})
        equity = cash_av + cash_rs + cash_pd + priced - gas_total if complete else None
        return ValuationResult(
            time_ms=t,
            complete=complete,
            cash_available=cash_av,
            cash_reserved=cash_rs,
            cash_pending=cash_pd,
            priced_value=priced,
            liquidation_gas=gas_total,
            holdings=holdings_out,
            equity=equity,
            policy=self.params.valuation_policy,
            warnings=warnings,
        )

    def _grid_snapshot(self, t: int) -> None:
        v = self.value_portfolio(t)
        self.equity_points.append(
            EquityPoint(
                time_ms=t,
                complete=v.complete,
                equity=v.equity,
                cash=v.cash_available + v.cash_reserved + v.cash_pending,
                priced=v.priced_value - v.liquidation_gas,
                no_route_assets=[h["asset"] for h in v.holdings if h["class"] == str(ValuationClass.NO_ROUTE)],
                unpriced_assets=[h["asset"] for h in v.holdings if h["class"] == str(ValuationClass.UNPRICED_MISSING_DATA)],
                source="grid",
            )
        )

    def _ledger_equity_point(self, t: int) -> None:
        v = self.value_portfolio(t)
        self.equity_points.append(
            EquityPoint(
                time_ms=t,
                complete=v.complete,
                equity=v.equity,
                cash=v.cash_available + v.cash_reserved + v.cash_pending,
                priced=v.priced_value - v.liquidation_gas,
                no_route_assets=[h["asset"] for h in v.holdings if h["class"] == str(ValuationClass.NO_ROUTE)],
                unpriced_assets=[h["asset"] for h in v.holdings if h["class"] == str(ValuationClass.UNPRICED_MISSING_DATA)],
                source="ledger",
            )
        )

    # ------------------------------------------------------------------ finish
    def finish(self) -> None:
        """Stop agent decisions, advance to the episode end, then apply the settlement tail to already submitted orders."""
        if self.finished:
            return
        self.process_until(self.end_ms)
        self.finished = True
        tail_end = self._tail_end_ms()
        # Only already-submitted orders resolve during the tail; no new orders are possible.
        while True:
            nxt = self._next_event_time()
            if nxt > tail_end or (not self.inclusions and not self.confirmations):
                break
            self._process_time(nxt)
        self.now_ms = max(self.now_ms, self.end_ms)
        self.tail_processed = True

    def unresolved_orders(self) -> list[Order]:
        return [o for o in self.orders.values() if o.state in (OrderState.PENDING_INCLUSION, OrderState.FILLED_PENDING_CONFIRMATION)]

    def state_hash(self) -> str:
        h = hashlib.sha256()
        h.update(self.ledger.content_hash().encode())
        for k in sorted(self.pools):
            p = self.pools[k]
            if isinstance(p, ClPoolState):
                h.update(f"{k}:cl:{p.sqrt_price_x96}:{p.tick}:{p.liquidity}:{p.ticks_digest()}:{p.halted}\n".encode())
            else:
                h.update(f"{k}:{p.reserve0}:{p.reserve1}:{p.halted}\n".encode())
        return h.hexdigest()

    def discovered_pools(self, as_of: int) -> list[str]:
        return sorted(k for k, d in self.pool_discovery_ms.items() if d <= as_of)

    def next_observable_event_ms(self, after_ms: int, horizon_ms: int) -> int | None:
        """Earliest agent-observable event after ``after_ms`` within horizon, without leaking hidden pools.

        Considers: availability of trades in already-discovered pools (from the tape and own fills),
        own order inclusion/confirmation times, and pool discoveries. Bounded by horizon.
        """
        limit = min(after_ms + horizon_ms, self.end_ms)
        best: int | None = None
        discovered = set(self.discovered_pools(after_ms))
        for d in self.pool_discovery_ms.values():
            if after_ms < d <= limit:
                best = d if best is None else min(best, d)
        for o in self.orders.values():
            for tm in (o.inclusion_time_ms, ):
                if tm is not None and after_ms < tm <= limit and o.state == OrderState.PENDING_INCLUSION:
                    best = tm if best is None else min(best, tm)
            if o.state == OrderState.FILLED_PENDING_CONFIRMATION and o.confirm_block is not None:
                ct = self.schedule.time_of(o.confirm_block)
                if after_ms < ct <= limit:
                    best = ct if best is None else min(best, ct)
        # Already-recorded but not yet available trades in discovered pools
        for k in discovered:
            for t in self.obs[k].trades[::-1]:
                if t.available_ms <= after_ms:
                    break
                if t.available_ms <= limit:
                    best = t.available_ms if best is None else min(best, t.available_ms)
        # Upcoming tape swaps in discovered pools
        i = self.cursor
        n = len(self.tape)
        while i < n:
            ev = self.tape[i]
            if ev.time_ms > limit:
                break
            if ev.kind in ("swap", "cl_swap") and ev.pool in discovered and ev.available_ms is not None and ev.available_ms > after_ms:
                cand = min(ev.available_ms, limit)
                best = cand if best is None else min(best, cand)
                break
            i += 1
        return best
