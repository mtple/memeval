"""Agent session: the single command handler behind HTTP, SDK and MCP.

The session owns one Simulation, one AliasMap, the request/decision budgets and the
virtual-time rate limiter. It returns only alias-mapped, relative-time payloads and
scans every envelope for leakage before returning it.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from ..datasets.pack import Pack
from ..domain.envelope import Envelope, Quality
from ..domain.identity import AliasMap, looks_like_canonical
from ..domain.quantities import QuantityError, fraction_to_decimal_str, parse_raw
from ..domain.status import (
    SUPPORTED_CPMM_MODELS,
    AvailabilityBasis,
    Completeness,
    ErrorCode,
    OrderState,
    PoolModel,
)
from ..observations.masking import LeakScanner
from ..observations.store import aggregate_candles, basis_for
from .simulation import INF, Simulation, SubmitRejected

TOOLS: dict[str, str] = {
    "session.describe": "Capabilities, limits, relative horizon, numeraire, assumptions and virtual clock.",
    "markets.list": "Paginated currently discoverable pools with point-in-time filters.",
    "markets.get": "Time-qualified metadata and available current observations for one pool.",
    "market.trades": "Bounded visible trade history ending at or before virtual now.",
    "market.candles": "Generic OHLCV over visible trades with explicit completeness handling.",
    "market.liquidity": "Current modeled reserve facts for a supported pool (model-labelled).",
    "market.restrictions": "Available individual restriction observations or explicit unknown/unsupported.",
    "broker.quote": "Exact-input simulated quote against the current model state.",
    "broker.submit": "Submit an exact-input simulated swap with min-output, deadline and idempotency key.",
    "broker.order": "Own order lifecycle and confirmed simulated fill records.",
    "portfolio.get": "Available/reserved/pending balances, holdings and valuation status.",
    "portfolio.history": "Own immutable ledger history, paginated.",
    "clock.advance": "Advance virtual time to a relative time or the next observable event (bounded).",
    "session.finish": "Stop agent decisions and apply the fixed terminal reporting procedure.",
}

UNSUPPORTED_CAPABILITIES = [
    "holder_graph",
    "wallet_history",
    "social_data",
    "web_search",
    "exact_output_swap",
    "limit_order",
    "stop_order",
    "bracket_order",
    "partial_fill",
    "cross_chain",
    "short",
    "leverage",
    "lp_provision",
    "prediction_markets",
    "best_opportunities_ranking",
]

DATA_TOOLS = {"markets.list", "markets.get", "market.trades", "market.candles", "market.liquidity", "market.restrictions"}
FREE_TOOLS = {"session.describe", "broker.order", "portfolio.get", "portfolio.history", "session.finish"}


class SessionError(Exception):
    def __init__(self, code: ErrorCode, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass(slots=True)
class BudgetState:
    max_requests: int
    max_decisions: int
    requests: int = 0
    decisions: int = 0
    invalid_calls: int = 0
    rate_limited: int = 0
    exhausted: bool = False


@dataclass(slots=True)
class TraceRecord:
    index: int
    request_id: str
    tool: str
    arguments: dict[str, Any]
    clock_before_ms: int
    clock_after_ms: int
    status: str
    error_code: str | None


@dataclass
class Session:
    session_id: str
    pack: Pack
    sim: Simulation
    alias: AliasMap
    scanner: LeakScanner
    mode: str = "practice"  # practice | sealed
    budget: BudgetState = field(default_factory=lambda: BudgetState(0, 0))
    request_times: deque[int] = field(default_factory=deque)
    trace: list[TraceRecord] = field(default_factory=list)
    paused: bool = False
    finished: bool = False
    terminal: dict[str, Any] | None = None

    # ------------------------------------------------------------------ construction
    @classmethod
    def create(
        cls,
        *,
        session_id: str,
        pack: Pack,
        bankroll_raw: int,
        mask_seed: str,
        engine_seed: str,
        mode: str = "practice",
    ) -> Session:
        sim = Simulation(pack, bankroll_raw=bankroll_raw, engine_seed=engine_seed)
        alias = AliasMap(mask_seed, numeraire_key=pack.numeraire, numeraire_alias=pack.manifest.numeraire_alias)
        # Assign aliases for everything up front so assignment never depends on later returns.
        for k in sorted(pack.assets):
            alias.asset(k)
        for k in sorted(pack.pools):
            alias.pool(k)
        b = pack.params.budgets
        s = cls(
            session_id=session_id,
            pack=pack,
            sim=sim,
            alias=alias,
            scanner=LeakScanner.for_pack(pack),
            mode=mode,
            budget=BudgetState(max_requests=b.max_requests, max_decisions=b.max_decisions),
        )
        return s

    # ------------------------------------------------------------------ helpers
    @property
    def now(self) -> int:
        return self.sim.now_ms

    @property
    def params(self):
        return self.pack.params

    @property
    def basis(self) -> AvailabilityBasis:
        return basis_for(str(self.pack.manifest.origin))

    def _decimals(self, asset_key: str) -> int:
        return self.pack.assets[asset_key].decimals

    def _resolve(self, kind: str, public_id: Any) -> str:
        if not isinstance(public_id, str) or not public_id:
            raise SessionError(ErrorCode.INVALID_REQUEST, f"{kind}_id must be a non-empty string")
        if looks_like_canonical(public_id):
            # Uniform response: never confirm or deny that a canonical identifier exists.
            raise SessionError(ErrorCode.NOT_YET_DISCOVERED, "identifier is not currently discoverable in this session")
        r = self.alias.resolve(public_id)
        if r is None or r[0] != kind:
            raise SessionError(ErrorCode.NOT_YET_DISCOVERED, "identifier is not currently discoverable in this session")
        return r[1]

    def _resolve_pool_discovered(self, public_id: Any) -> str:
        key = self._resolve("pool", public_id)
        if self.sim.pool_discovery_ms.get(key, INF) > self.now:
            raise SessionError(ErrorCode.NOT_YET_DISCOVERED, "identifier is not currently discoverable in this session")
        return key

    def _pool_public(self, key: str) -> dict[str, Any]:
        meta = self.pack.pools[key]
        st = self.sim.pools.get(key)
        supported = meta.supported_by_cpmm and PoolModel(meta.model) in SUPPORTED_CPMM_MODELS and st is not None
        reason = None
        if not supported:
            reason = meta.unsupported_reason or ("no executable initial state in this pack" if st is None else "unsupported")
        if key in self.sim.fidelity_failed:
            supported = False
            reason = "environment fidelity limit reached for this market"
        base = meta.asset1 if meta.asset0 == self.pack.numeraire else meta.asset0
        quote = self.pack.numeraire if base != meta.asset0 or meta.asset0 != self.pack.numeraire else meta.asset1
        if self.pack.numeraire not in (meta.asset0, meta.asset1):
            quote = meta.asset0
            base = meta.asset1
        return {
            "pool_id": self.alias.pool(key),
            "venue_model": str(meta.model),
            "execution_supported": supported,
            "unsupported_reason": reason,
            "base_asset": self.alias.asset(base),
            "quote_asset": self.alias.asset(quote),
            "assets": [
                {"asset_id": self.alias.asset(meta.asset0), "decimals": self._decimals(meta.asset0)},
                {"asset_id": self.alias.asset(meta.asset1), "decimals": self._decimals(meta.asset1)},
            ],
            "fee": {"numerator": meta.fee_numerator, "denominator": meta.fee_denominator},
            "listed_ms": self.sim.pool_discovery_ms.get(key),
            "age_ms": self.now - self.sim.pool_discovery_ms.get(key, self.now),
            "halted_observed": bool(st.halted) if st is not None else None,
        }

    def _trade_public(self, t, base: str, quote: str) -> dict[str, Any]:
        if t.asset_in == quote:
            side = "buy"
            price = Fraction(t.amount_in, t.amount_out) if t.amount_out else None
        else:
            side = "sell"
            price = Fraction(t.amount_out, t.amount_in) if t.amount_in else None
        # price quoted as quote-raw per base-raw scaled to whole units
        dq = self._decimals(quote)
        db = self._decimals(base)
        price_units = None if price is None else price * Fraction(10**db, 10**dq)
        wallet = "self" if t.origin == "own" else (self.alias.wallet(t.wallet) if t.wallet else None)
        return {
            "trade_id": f"t_{t.seq}",
            "time_ms": t.event_ms,
            "available_ms": t.available_ms,
            "block_seq": t.block - self.sim.schedule.first_block,
            "tx": "self" if t.origin == "own" else (self.alias.tx(t.tx) if t.tx else None),
            "sender": wallet,
            "sender_identity_basis": "fixture_declared_participant" if self.pack.manifest.origin == "generated_fixture" else "not_established_may_be_router",
            "side": side,
            "asset_in": self.alias.asset(t.asset_in),
            "asset_out": self.alias.asset(t.asset_out),
            "amount_in_raw": str(t.amount_in),
            "amount_out_raw": str(t.amount_out),
            "price_quote_per_base": None if price_units is None else fraction_to_decimal_str(price_units, 18),
            "own": t.origin == "own",
        }

    def _base_quote(self, key: str) -> tuple[str, str]:
        meta = self.pack.pools[key]
        if meta.asset0 == self.pack.numeraire:
            return meta.asset1, meta.asset0
        if meta.asset1 == self.pack.numeraire:
            return meta.asset0, meta.asset1
        return meta.asset1, meta.asset0

    def _int_arg(self, args: dict, name: str, *, required: bool = False, default: int | None = None, minimum: int | None = None) -> int | None:
        v = args.get(name, default)
        if v is None:
            if required:
                raise SessionError(ErrorCode.INVALID_REQUEST, f"{name} is required")
            return None
        if isinstance(v, bool) or not isinstance(v, int | str):
            raise SessionError(ErrorCode.INVALID_REQUEST, f"{name} must be an integer")
        try:
            iv = int(v)
        except ValueError as e:
            raise SessionError(ErrorCode.INVALID_REQUEST, f"{name} must be an integer") from e
        if minimum is not None and iv < minimum:
            raise SessionError(ErrorCode.INVALID_REQUEST, f"{name} must be >= {minimum}")
        return iv

    def _raw_arg(self, args: dict, name: str) -> int:
        v = args.get(name)
        if v is None:
            raise SessionError(ErrorCode.INVALID_REQUEST, f"{name} is required")
        try:
            return parse_raw(v)
        except QuantityError as e:
            raise SessionError(ErrorCode.INVALID_REQUEST, f"{name}: {e}") from e

    def _quality(self, completeness: Completeness, warnings: list[str] | None = None, observed_through: int | None = None) -> Quality:
        w = list(warnings or [])
        if self.pack.manifest.origin == "generated_fixture":
            w.append("GENERATED_FIXTURE")
        if self.pack.manifest.origin == "historical_reconstruction":
            w.append("PROVIDER_COMPLETENESS_NOT_ESTABLISHED")
        return Quality(
            completeness=completeness,
            availability_basis=self.basis,
            observed_through_ms=self.now if observed_through is None else observed_through,
            stale=False,
            warnings=w,
        )

    # ------------------------------------------------------------------ dispatcher
    def handle(self, request_id: str, tool: str, arguments: dict[str, Any] | None) -> Envelope:
        args = arguments or {}
        before = self.now
        try:
            if tool not in TOOLS:
                if tool in UNSUPPORTED_CAPABILITIES or tool.split(".")[-1] in UNSUPPORTED_CAPABILITIES:
                    raise SessionError(ErrorCode.UNSUPPORTED_CAPABILITY, f"{tool} is not implemented in this environment")
                raise SessionError(ErrorCode.INVALID_REQUEST, f"unknown tool {tool}", {"known_tools": sorted(TOOLS)})
            if not isinstance(args, dict):
                raise SessionError(ErrorCode.INVALID_REQUEST, "arguments must be an object")
            if self.paused and tool not in ("session.describe",):
                raise SessionError(ErrorCode.RUN_PAUSED, "run is paused by the operator; retry later")
            if self.finished and tool not in FREE_TOOLS:
                raise SessionError(ErrorCode.SESSION_FINISHED, "session.finish was already called")
            self._account_request(tool)
            data, quality = self._dispatch(tool, args)
            env = Envelope.ok(request_id=request_id, session_id=self.session_id, clock_ms=self.now, data=data, quality=quality)
        except SessionError as e:
            if e.code in (ErrorCode.INVALID_REQUEST, ErrorCode.INVALID_ORDER):
                self.budget.invalid_calls += 1
            env = Envelope.fail(request_id=request_id, session_id=self.session_id, clock_ms=self.now, code=e.code, message=e.message, details=e.details, quality=self._quality(Completeness.UNKNOWN))
        except SubmitRejected as e:
            code = ErrorCode(e.code) if e.code in ErrorCode.__members__ else ErrorCode.INVALID_ORDER
            if code in (ErrorCode.INVALID_ORDER,):
                self.budget.invalid_calls += 1
            env = Envelope.fail(request_id=request_id, session_id=self.session_id, clock_ms=self.now, code=code, message=e.message, details=e.details, quality=self._quality(Completeness.UNKNOWN))
        payload = env.model_dump(mode="json")
        findings = self.scanner.scan(payload)
        if findings:
            # Never return a leaking payload. Replace it with a safe error.
            env = Envelope.fail(
                request_id=request_id,
                session_id=self.session_id,
                clock_ms=self.now,
                code=ErrorCode.INVALID_REQUEST,
                message="response withheld by leakage scanner",
                details={"finding_count": len(findings), "kinds": sorted({f.kind for f in findings})},
            )
        self.trace.append(
            TraceRecord(
                index=len(self.trace),
                request_id=request_id,
                tool=tool,
                arguments=args if isinstance(args, dict) else {},
                clock_before_ms=before,
                clock_after_ms=self.now,
                status=env.status,
                error_code=str(env.error.code) if env.error else None,
            )
        )
        return env

    def _account_request(self, tool: str) -> None:
        self.budget.requests += 1
        if tool in FREE_TOOLS:
            return
        if self.budget.requests > self.budget.max_requests:
            self.budget.exhausted = True
            raise SessionError(ErrorCode.BUDGET_EXHAUSTED, "request budget exhausted; only portfolio/order/finish tools remain", {"max_requests": self.budget.max_requests})
        if tool == "broker.submit":
            if self.budget.decisions >= self.budget.max_decisions:
                self.budget.exhausted = True
                raise SessionError(ErrorCode.BUDGET_EXHAUSTED, "decision budget exhausted", {"max_decisions": self.budget.max_decisions})
        # Virtual-time rate limit for data/broker tools.
        if tool in DATA_TOOLS or tool.startswith("broker."):
            window_start = self.now - 60_000
            while self.request_times and self.request_times[0] < window_start:
                self.request_times.popleft()
            limit = self.params.rate_limit.simulated_requests_per_minute
            if len(self.request_times) >= limit:
                self.budget.rate_limited += 1
                retry = self.request_times[0] + 60_000 - self.now
                raise SessionError(ErrorCode.RATE_LIMITED, "simulated request rate limit reached", {"retry_after_ms": max(retry, 1)})
            self.request_times.append(self.now)

    def _dispatch(self, tool: str, args: dict[str, Any]) -> tuple[dict[str, Any], Quality]:
        if tool == "session.describe":
            return self.describe(), self._quality(Completeness.COMPLETE)
        if tool == "session.finish":
            return self.finish(), self._quality(Completeness.COMPLETE)
        if tool == "clock.advance":
            return self.advance(args), self._quality(Completeness.COMPLETE)
        if tool == "portfolio.get":
            return self.portfolio(), self._quality(Completeness.COMPLETE)
        if tool == "portfolio.history":
            return self.history(args), self._quality(Completeness.COMPLETE)
        if tool == "broker.order":
            return self.order(args), self._quality(Completeness.COMPLETE)
        if self.sim.finished or self.now >= self.sim.end_ms:
            raise SessionError(ErrorCode.EPISODE_ENDED, "the episode has ended; call session.finish", {"end_ms": self.sim.end_ms})
        # Data and broker tools consume declared simulated service latency first.
        latency = self.params.quote_latency_ms if tool.startswith("broker.") else self.params.data_latency_ms
        self.sim.process_until(min(self.now + latency, self.sim.end_ms))
        if tool == "markets.list":
            return self.markets_list(args)
        if tool == "markets.get":
            return self.markets_get(args)
        if tool == "market.trades":
            return self.market_trades(args)
        if tool == "market.candles":
            return self.market_candles(args)
        if tool == "market.liquidity":
            return self.market_liquidity(args)
        if tool == "market.restrictions":
            return self.market_restrictions(args)
        if tool == "broker.quote":
            return self.broker_quote(args)
        if tool == "broker.submit":
            return self.broker_submit(args)
        raise SessionError(ErrorCode.INVALID_REQUEST, f"unknown tool {tool}")

    # ------------------------------------------------------------------ tools
    def describe(self) -> dict[str, Any]:
        m = self.pack.manifest
        p = self.params
        return {
            "session_id": self.session_id,
            "mode": self.mode,
            "clock_ms": self.now,
            "episode": {
                "duration_ms": m.period.duration_ms,
                "is_full_week": m.period.is_full_week,
                "prehistory_ms": (m.period.start_utc_ms - m.period.prehistory_start_utc_ms) if m.period.prehistory_start_utc_ms else 0,
                "remaining_ms": max(0, self.sim.end_ms - self.now),
                "origin": str(m.origin),
                "chain": m.chain if self.mode == "practice" else "hidden",
                "use_status": str(m.validation.qualification),
                "predictive_validity": "not_established",
            },
            "numeraire": {"asset_id": self.alias.asset(self.pack.numeraire), "decimals": m.numeraire_decimals},
            "bankroll_raw": str(self.sim.bankroll_raw),
            "execution": {
                "model": str(m.execution.model),
                "swap_type": "exact_input_atomic_full_fill_or_revert",
                "capacity_profile": p.capacity.model_dump(),
                "latency_assumptions": p.public_latency_assumptions(),
                "gas_cost_raw": p.gas_cost_raw,
                "gas_basis": p.gas_basis,
                "valuation_policy": p.valuation_policy,
                "external_flow": "fixed recorded intent; outputs recomputed on the private market copy (historical-flow-based simulation)",
            },
            "information": {
                "availability_basis": str(self.basis),
                "availability_delay_ms": p.availability_delay_ms,
                "time_basis": "integer milliseconds relative to episode start; prehistory is negative",
                "liquidity_and_quotes": "reflect the current private model state at response time (declared mechanics)",
                "candles": {"intervals_ms": p.candle_intervals_ms, "default_include_partial": False},
            },
            "budgets": {
                "max_requests": self.budget.max_requests,
                "requests_used": self.budget.requests,
                "max_decisions": self.budget.max_decisions,
                "decisions_used": self.budget.decisions,
                "simulated_requests_per_minute": p.rate_limit.simulated_requests_per_minute,
                "max_page_size": p.budgets.max_page_size,
            },
            "tools": TOOLS,
            "unsupported_capabilities": UNSUPPORTED_CAPABILITIES,
            "token_behavior_basis": str(m.data.token_behavior_basis),
            "limitations": [
                "Blinded interface, not contamination-proof.",
                "Fixed external flow cannot model how other participants would have reacted to you.",
                "Quotes are model outputs, not guaranteed fills; min-output failures revert at inclusion.",
                "Missing data is reported as missing, never as zero activity.",
            ],
        }

    def markets_list(self, args: dict[str, Any]) -> tuple[dict[str, Any], Quality]:
        limit = self._int_arg(args, "limit", default=50, minimum=1) or 50
        limit = min(limit, self.params.budgets.max_page_size)
        cursor = self._int_arg(args, "cursor", default=0, minimum=0) or 0
        filters = args.get("filters") or {}
        if not isinstance(filters, dict):
            raise SessionError(ErrorCode.INVALID_REQUEST, "filters must be an object")
        min_age = self._int_arg(filters, "min_age_ms")
        max_age = self._int_arg(filters, "max_age_ms")
        active_since = self._int_arg(filters, "active_since_ms")
        venue = filters.get("venue_model")
        supported_only = bool(filters.get("execution_supported_only", False))
        discovered = self.sim.discovered_pools(self.now)
        rows = []
        for key in discovered:
            pub = self._pool_public(key)
            if min_age is not None and pub["age_ms"] < min_age:
                continue
            if max_age is not None and pub["age_ms"] > max_age:
                continue
            if venue is not None and pub["venue_model"] != venue:
                continue
            if supported_only and not pub["execution_supported"]:
                continue
            if active_since is not None:
                last = self.sim.obs[key].last_visible(self.now)
                if last is None or last.event_ms < active_since:
                    continue
            last = self.sim.obs[key].last_visible(self.now)
            pub["last_trade_ms"] = last.event_ms if last else None
            pub["visible_trade_count"] = len(self.sim.obs[key].visible(self.now))
            rows.append(pub)
        rows.sort(key=lambda r: r["pool_id"])
        page = rows[cursor : cursor + limit]
        next_cursor = cursor + limit if cursor + limit < len(rows) else None
        data = {
            "items": page,
            "next_cursor": next_cursor,
            "total_currently_discoverable": len(rows),
            "as_of_ms": self.now,
            "note": "Totals count only pools discoverable at as_of_ms; future listings are not disclosed.",
        }
        return data, self._quality(Completeness.COMPLETE)

    def markets_get(self, args: dict[str, Any]) -> tuple[dict[str, Any], Quality]:
        key = self._resolve_pool_discovered(args.get("pool_id"))
        pub = self._pool_public(key)
        base, quote = self._base_quote(key)
        obs = self.sim.obs[key]
        last = obs.last_visible(self.now)
        pub["last_trade"] = self._trade_public(last, base, quote) if last else None
        pub["last_trade_age_ms"] = (self.now - last.event_ms) if last else None
        pub["visible_trade_count"] = len(obs.visible(self.now))
        pub["restrictions"] = self._restrictions_for(key)
        cov = obs.coverage_state(min(self.sim.pool_discovery_ms.get(key, 0), self.now), self.now) if obs.coverage else None
        pub["coverage_to_now"] = str(cov) if cov else "unknown"
        warnings = []
        if cov is not None and str(cov) != "completed_and_checked":
            warnings.append("COVERAGE_INCOMPLETE")
        return pub, self._quality(Completeness.COMPLETE if cov and str(cov) == "completed_and_checked" else Completeness.PARTIAL, warnings)

    def _clamp_range(self, args: dict[str, Any]) -> tuple[int, int, list[str]]:
        warnings: list[str] = []
        start = self._int_arg(args, "start_ms")
        end = self._int_arg(args, "end_ms")
        if start is None:
            start = -(10**15)
        if end is None:
            end = self.now
        if end > self.now:
            warnings.append("RANGE_CLAMPED_TO_PRESENT")
            end = self.now
        if start > end:
            raise SessionError(ErrorCode.INVALID_REQUEST, "start_ms must not exceed end_ms (after clamping to the present)")
        return start, end, warnings

    def market_trades(self, args: dict[str, Any]) -> tuple[dict[str, Any], Quality]:
        key = self._resolve_pool_discovered(args.get("pool_id"))
        start, end, warnings = self._clamp_range(args)
        limit = min(self._int_arg(args, "limit", default=100, minimum=1) or 100, self.params.budgets.max_page_size)
        cursor = self._int_arg(args, "cursor", default=0, minimum=0) or 0
        base, quote = self._base_quote(key)
        obs = self.sim.obs[key]
        rows = obs.visible_between(start, end, self.now)
        page = rows[cursor : cursor + limit]
        next_cursor = cursor + limit if cursor + limit < len(rows) else None
        cov_start = max(start, self.sim.pool_discovery_ms.get(key, start))
        cov = obs.coverage_state(cov_start, end) if obs.coverage and end > cov_start else None
        if cov is None:
            comp = Completeness.UNKNOWN
        elif str(cov) == "completed_and_checked":
            comp = Completeness.COMPLETE if rows else Completeness.EMPTY_VERIFIED
        else:
            comp = Completeness.PARTIAL
            warnings.append("COVERAGE_INCOMPLETE")
        data = {
            "pool_id": self.alias.pool(key),
            "base_asset": self.alias.asset(base),
            "quote_asset": self.alias.asset(quote),
            "range": {"start_ms": start if start > -(10**15) else None, "end_ms": end},
            "items": [self._trade_public(t, base, quote) for t in page],
            "next_cursor": next_cursor,
            "count_in_range": len(rows),
            "coverage": str(cov) if cov else "unknown",
        }
        return data, self._quality(comp, warnings, observed_through=end)

    def market_candles(self, args: dict[str, Any]) -> tuple[dict[str, Any], Quality]:
        key = self._resolve_pool_discovered(args.get("pool_id"))
        interval = self._int_arg(args, "interval_ms", required=True, minimum=1000)
        assert interval is not None
        if interval not in self.params.candle_intervals_ms:
            raise SessionError(ErrorCode.INVALID_REQUEST, "unsupported interval_ms", {"supported": self.params.candle_intervals_ms})
        start, end, warnings = self._clamp_range(args)
        if start < -(10**14):
            start = end - interval * 200
        include_partial = bool(args.get("include_partial", False))
        max_bars = self.params.budgets.max_response_items
        if (end - start) // interval > max_bars:
            start = end - interval * max_bars
            warnings.append("RANGE_TRUNCATED_TO_MAX_ITEMS")
        base, quote = self._base_quote(key)
        candles, gaps = aggregate_candles(
            self.sim.obs[key],
            base_asset=base,
            interval_ms=interval,
            start_ms=start,
            end_ms=end,
            as_of=self.now,
            availability_delay_ms=self.params.availability_delay_ms,
            include_partial=include_partial,
        )
        db, dq = self._decimals(base), self._decimals(quote)
        scale = Fraction(10**db, 10**dq)
        out = []
        for c in candles:
            d = c.to_public()
            for f in ("open", "high", "low", "close"):
                v = getattr(c, f)
                d[f] = None if v is None else fraction_to_decimal_str(v * scale, 18)
            out.append(d)
        comp = Completeness.COMPLETE if not gaps and all(c.closed for c in candles) else Completeness.PARTIAL
        if gaps:
            warnings.append("MISSING_INTERVALS_PRESENT")
        data = {
            "pool_id": self.alias.pool(key),
            "base_asset": self.alias.asset(base),
            "quote_asset": self.alias.asset(quote),
            "interval_ms": interval,
            "price_unit": "quote per whole base unit",
            "range": {"start_ms": start, "end_ms": end},
            "items": out,
            "gaps": gaps,
            "include_partial": include_partial,
            "close_rule": f"bar closes when end + availability_delay ({self.params.availability_delay_ms} ms) <= now",
        }
        return data, self._quality(comp, warnings, observed_through=end)

    def market_liquidity(self, args: dict[str, Any]) -> tuple[dict[str, Any], Quality]:
        key = self._resolve_pool_discovered(args.get("pool_id"))
        meta = self.pack.pools[key]
        st = self.sim.pools.get(key)
        if st is None or not meta.supported_by_cpmm:
            raise SessionError(ErrorCode.UNSUPPORTED_CAPABILITY, f"no modeled liquidity for this venue: {meta.unsupported_reason or 'missing state'}")
        if key in self.sim.fidelity_failed:
            raise SessionError(ErrorCode.ENVIRONMENT_FIDELITY_LIMIT, "market left the model's validated domain")
        data = {
            "pool_id": self.alias.pool(key),
            "basis": "modeled_private_market_state",
            "model": str(meta.model),
            "reserves": [
                {"asset_id": self.alias.asset(st.asset0), "reserve_raw": str(st.reserve0)},
                {"asset_id": self.alias.asset(st.asset1), "reserve_raw": str(st.reserve1)},
            ],
            "fee": {"numerator": st.fee_num, "denominator": st.fee_den},
            "halted": st.halted,
            "as_of_ms": self.now,
            "note": "Reserves are the private model copy after your own fills; depth is CPMM formula depth, not router depth.",
        }
        return data, self._quality(Completeness.COMPLETE)

    def _restrictions_for(self, key: str) -> dict[str, Any]:
        meta = self.pack.pools[key]
        items = []
        for ev in self.sim.restriction_events:
            if ev.pool == key and ev.available_ms is not None and ev.available_ms <= self.now:
                items.append({"asset_id": self.alias.asset(ev.asset), "observed_ms": ev.event_ms, "available_ms": ev.available_ms, "source": "fixture_rules", "flags": {k: v for k, v in ev.payload.items() if k != "asset"}})
        # Pack-level recorded restriction observations (historical) with availability
        start = self.pack.manifest.period.start_utc_ms
        for r in self.pack.restrictions:
            if r.asset in (meta.asset0, meta.asset1) and r.available_utc_ms is not None and (r.available_utc_ms - start) <= self.now:
                items.append({"asset_id": self.alias.asset(r.asset), "observed_ms": r.observed_utc_ms - start, "available_ms": r.available_utc_ms - start, "source": r.source, "flags": {"is_honeypot": r.is_honeypot, "buy_tax_bps": r.buy_tax_bps, "sell_tax_bps": r.sell_tax_bps, "is_in_dex": r.is_in_dex, "sell_blocked": r.sell_blocked}, "conflicts": r.conflicts})
        basis = str(self.pack.manifest.data.token_behavior_basis)
        return {"basis": basis, "items": items, "status": "observations_available" if items else ("unknown" if basis in ("unknown", "assumed_standard_transfer") else "no_restrictions_observed_under_fixture_rules")}

    def market_restrictions(self, args: dict[str, Any]) -> tuple[dict[str, Any], Quality]:
        key = self._resolve_pool_discovered(args.get("pool_id"))
        data = {"pool_id": self.alias.pool(key), **self._restrictions_for(key)}
        comp = Completeness.COMPLETE if data["basis"] == "known_fixture_rules" else Completeness.UNKNOWN
        return data, self._quality(comp, [] if comp == Completeness.COMPLETE else ["HISTORICAL_RESTRICTIONS_UNKNOWN"])

    def broker_quote(self, args: dict[str, Any]) -> tuple[dict[str, Any], Quality]:
        key = self._resolve_pool_discovered(args.get("pool_id"))
        asset_in = self._resolve("asset", args.get("asset_in"))
        amount_in = self._raw_arg(args, "amount_in_raw")
        q = self.sim.quote(key, asset_in, amount_in)
        data = {
            "quote_id": q.quote_id,
            "pool_id": self.alias.pool(key),
            "asset_in": self.alias.asset(q.asset_in),
            "asset_out": self.alias.asset(q.asset_out),
            "amount_in_raw": str(q.amount_in),
            "expected_amount_out_raw": str(q.amount_out),
            "fee": {"numerator": q.fee_num, "denominator": q.fee_den, "note": "embedded in output; not charged again"},
            "gas_cost_raw": str(q.gas_cost),
            "created_ms": q.created_ms,
            "expires_ms": q.expires_ms,
            "state_version": q.state_version,
            "capacity_ok": q.capacity_ok,
            "capacity_reason": q.capacity_reason,
            "note": "Model output on the current private state; not a guaranteed fill.",
        }
        return data, self._quality(Completeness.COMPLETE)

    def broker_submit(self, args: dict[str, Any]) -> tuple[dict[str, Any], Quality]:
        key = self._resolve_pool_discovered(args.get("pool_id"))
        asset_in = self._resolve("asset", args.get("asset_in"))
        asset_out = self._resolve("asset", args.get("asset_out"))
        amount_in = self._raw_arg(args, "amount_in_raw")
        min_out = self._raw_arg(args, "min_amount_out_raw")
        deadline = self._int_arg(args, "deadline_ms", required=True)
        idem = args.get("idempotency_key")
        if not isinstance(idem, str) or not idem or len(idem) > 128:
            raise SessionError(ErrorCode.INVALID_ORDER, "idempotency_key must be a non-empty string (<=128 chars)")
        quote_id = args.get("quote_id")
        if quote_id is not None and not isinstance(quote_id, str):
            raise SessionError(ErrorCode.INVALID_ORDER, "quote_id must be a string")
        assert deadline is not None
        order, created = self.sim.submit(
            pool_key=key,
            asset_in=asset_in,
            asset_out=asset_out,
            amount_in=amount_in,
            min_amount_out=min_out,
            deadline_ms=deadline,
            idempotency_key=idem,
            quote_id=quote_id,
        )
        if created:
            self.budget.decisions += 1
        data = {"created": created, "order": order.to_public(self.alias)}
        return data, self._quality(Completeness.COMPLETE)

    def order(self, args: dict[str, Any]) -> dict[str, Any]:
        oid = args.get("order_id")
        if oid is not None:
            o = self.sim.orders.get(str(oid))
            if o is None:
                raise SessionError(ErrorCode.INVALID_REQUEST, "unknown order_id")
            return {"order": o.to_public(self.alias)}
        limit = min(self._int_arg(args, "limit", default=100, minimum=1) or 100, self.params.budgets.max_page_size)
        cursor = self._int_arg(args, "cursor", default=0, minimum=0) or 0
        rows = list(self.sim.orders.values())
        page = rows[cursor : cursor + limit]
        return {"items": [o.to_public(self.alias) for o in page], "next_cursor": cursor + limit if cursor + limit < len(rows) else None, "total": len(rows)}

    def portfolio(self) -> dict[str, Any]:
        v = self.sim.value_portfolio()
        L = self.sim.ledger
        assets = set()
        for acct in ("agent.available", "agent.reserved", "agent.pending"):
            assets.update(L.balances_of(acct))
        balances = []
        for a in sorted(assets):
            balances.append(
                {
                    "asset_id": self.alias.asset(a),
                    "decimals": self._decimals(a),
                    "available_raw": str(L.balance("agent.available", a)),
                    "reserved_raw": str(L.balance("agent.reserved", a)),
                    "pending_raw": str(L.balance("agent.pending", a)),
                }
            )
        pending_orders = [o.order_id for o in self.sim.unresolved_orders()]
        return {
            "clock_ms": self.now,
            "numeraire": self.alias.asset(self.pack.numeraire),
            "balances": balances,
            "pending_orders": pending_orders,
            "valuation": {
                "policy": v.policy,
                "complete": v.complete,
                "cash_available_raw": str(v.cash_available),
                "cash_reserved_raw": str(v.cash_reserved),
                "cash_pending_raw": str(v.cash_pending),
                "priced_inventory_raw": str(v.priced_value),
                "liquidation_gas_raw": str(v.liquidation_gas),
                "model_equity_raw": None if v.equity is None else str(v.equity),
                "holdings": [
                    {
                        "asset_id": self.alias.asset(h["asset"]),
                        "quantity_raw": str(h["quantity"]),
                        "class": h["class"],
                        "model_value_raw": str(h["value"]),
                        "pool_id": self.alias.pool(h["pool"]) if h["pool"] else None,
                        "reason": h["reason"],
                    }
                    for h in v.holdings
                ],
                "warnings": v.warnings,
                "note": "Model liquidation value on a temporary branch; not a prediction of realizable value.",
            },
        }

    def history(self, args: dict[str, Any]) -> dict[str, Any]:
        limit = min(self._int_arg(args, "limit", default=100, minimum=1) or 100, self.params.budgets.max_page_size)
        cursor = self._int_arg(args, "cursor", default=0, minimum=0) or 0
        entries = self.sim.ledger.entries
        page = entries[cursor : cursor + limit]
        items = []
        for e in page:
            d = e.to_public()
            for leg in d["legs"]:
                leg["asset"] = self.alias.asset(leg["asset"])
                if leg["account"].startswith("pool."):
                    leg["account"] = "pool." + self.alias.pool(leg["account"][5:])
            items.append(d)
        return {"items": items, "next_cursor": cursor + limit if cursor + limit < len(entries) else None, "total": len(entries)}

    def advance(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.sim.finished:
            raise SessionError(ErrorCode.SESSION_FINISHED, "session already finished")
        before = self.now
        states_before = {o.order_id: o.state for o in self.sim.orders.values()}
        if args.get("next_event"):
            max_ms = self._int_arg(args, "max_ms", default=3_600_000, minimum=1) or 3_600_000
            nxt = self.sim.next_observable_event_ms(self.now, max_ms)
            target = nxt if nxt is not None else min(self.now + max_ms, self.sim.end_ms)
        else:
            to_ms = self._int_arg(args, "to_ms", required=True)
            assert to_ms is not None
            if to_ms < self.now:
                raise SessionError(ErrorCode.INVALID_REQUEST, "to_ms is in the past", {"clock_ms": self.now})
            target = to_ms
        target = min(target, self.sim.end_ms)
        self.sim.process_until(target)
        changed = [o.to_public(self.alias) for o in self.sim.orders.values() if states_before.get(o.order_id) != o.state]
        return {
            "advanced_from_ms": before,
            "clock_ms": self.now,
            "episode_ended": self.now >= self.sim.end_ms,
            "orders_changed": changed,
        }

    def finish(self) -> dict[str, Any]:
        if not self.finished:
            self.sim.finish()
            self.finished = True
        v = self.sim.value_portfolio()
        unresolved = [o.to_public(self.alias) for o in self.sim.unresolved_orders()]
        summary = {
            "clock_ms": self.now,
            "finished": True,
            "terminal_portfolio": self.portfolio(),
            "unresolved_orders": unresolved,
            "valuation_complete": v.complete,
            "note": "Terminal reporting applies the fixed valuation policy; no forced last-price sale.",
        }
        self.terminal = summary
        return summary

    # ------------------------------------------------------------------ replay support
    def trace_hash(self) -> str:
        h = hashlib.sha256()
        for r in self.trace:
            h.update(json.dumps({"tool": r.tool, "args": r.arguments, "status": r.status, "code": r.error_code, "clock": r.clock_after_ms}, sort_keys=True).encode())
        return h.hexdigest()

    def result_hash(self) -> str:
        return hashlib.sha256((self.sim.state_hash() + self.trace_hash()).encode()).hexdigest()


def replay_trace(pack: Pack, trace: list[dict[str, Any]], *, bankroll_raw: int, mask_seed: str, engine_seed: str, session_id: str = "replay", mode: str = "practice") -> Session:
    """Re-execute recorded agent commands against a fresh session; used for action-replay reproducibility."""
    s = Session.create(session_id=session_id, pack=pack, bankroll_raw=bankroll_raw, mask_seed=mask_seed, engine_seed=engine_seed, mode=mode)
    for r in trace:
        s.handle(r["request_id"], r["tool"], r.get("arguments") or {})
    return s
