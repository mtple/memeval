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
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any

from ..datasets.pack import Pack
from ..domain.envelope import Envelope, Quality
from ..domain.identity import AliasMap, looks_like_canonical
from ..domain.profiles import ResourceProfile, effective_pack
from ..domain.quantities import QuantityError, fraction_to_decimal_str, parse_raw
from ..domain.status import (
    TERMINAL_ORDER_STATES,
    AvailabilityBasis,
    Completeness,
    ErrorCode,
)
from ..observations.masking import LeakScanner
from ..observations.store import aggregate_candles, basis_for
from ..venues.clmm.pool import ClPoolState
from .simulation import INF, Simulation, SubmitRejected, cl_supported, cpmm_supported

TOOLS: dict[str, str] = {
    "session.describe": "Capabilities, limits, relative horizon, numeraire, assumptions and virtual clock.",
    "session.snapshot": "Compact visible market activity, freshness, coverage, modeled depth, portfolio and orders. Optional pool_ids select a client-owned watchlist; discoveries remain separate. Accepts since_ms, window_ms, stale_after_ms, limit and market/discovery/order cursors.",
    "markets.list": "Paginated currently discoverable pools with point-in-time filters; sort by newest, most_traded or recently_traded to find launches worth a look.",
    "markets.get": "Time-qualified metadata and available current observations for one pool.",
    "market.trades": "Bounded visible trade history ending at or before virtual now.",
    "market.candles": "Generic OHLCV over visible trades with explicit completeness handling.",
    "market.liquidity": "Current modeled liquidity facts for a supported pool (CPMM reserves, or CL sqrt price / tick / active liquidity / virtual depth; model-labelled).",
    "market.restrictions": "Available individual restriction observations or explicit unknown/unsupported.",
    "broker.quote": "Exact-input simulated quote against the current model state.",
    "broker.submit": "Optional reason and exit_condition metadata are bounded to 512 characters each and included in idempotency. Submit an exact-input simulated swap with min-output, deadline and idempotency key.",
    "broker.order": "Own order lifecycle and confirmed simulated fill records.",
    "portfolio.get": "Available/reserved/pending balances, holdings and valuation status.",
    "portfolio.history": "Own immutable ledger history, paginated.",
    "clock.advance": "Advance virtual time to a relative time or the next observable event (bounded).",
    "clock.wait": "Wait until until_ms or a delayed one-shot notification. Up to 32 conditions: new_pool (since_ms, min_visible_trades, min_numeraire_depth_raw), price_cross (pool_id, direction above/below, price), liquidity_below (pool_id, depth_raw), order_terminal (order_id). Never submits orders.",
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

KNOWN_NAMESPACES = {"session", "markets", "market", "broker", "portfolio", "clock"}
MARKETS_SORTS = frozenset({"pool_id", "newest", "most_traded", "recently_traded"})
DATA_TOOLS = {"session.snapshot", "clock.wait", "markets.list", "markets.get", "market.trades", "market.candles", "market.liquidity", "market.restrictions"}
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
    decision_elapsed_ms: int = 0
    delivered: dict[str, Any] = field(default_factory=dict)


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
    resource_profile: ResourceProfile = field(default_factory=ResourceProfile)

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
        resource_profile: dict | None = None,
    ) -> Session:
        profile = ResourceProfile.model_validate(resource_profile or {})
        pack = effective_pack(pack, profile)
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
            budget=BudgetState(max_requests=min(b.max_requests, profile.max_requests), max_decisions=min(b.max_decisions, profile.max_decisions)),
            resource_profile=profile,
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

    def _public_details(self, details: Any) -> Any:
        """Error details built inside the engine name assets and pools by their canonical keys (a real
        pack's are chain addresses). Replace every such key with the session alias so a rejection
        reaches the agent as a rejection and not as a payload the leakage scanner has to withhold."""
        if isinstance(details, dict):
            return {k: self._public_details(v) for k, v in details.items()}
        if isinstance(details, list):
            return [self._public_details(v) for v in details]
        if isinstance(details, str):
            if details in self.pack.assets:
                return self.alias.asset(details)
            if details in self.pack.pools:
                return self.alias.pool(details)
        return details

    def _pool_public(self, key: str) -> dict[str, Any]:
        meta = self.pack.pools[key]
        st = self.sim.pools.get(key)
        supported = (cpmm_supported(meta) or cl_supported(meta)) and st is not None
        reason = None
        if not supported:
            if st is None and cl_supported(meta) and self.sim.cl_init_ms.get(key, -INF) > self.now:
                reason = "pool not yet initialized at the current virtual time"
            else:
                reason = meta.unsupported_reason or ("no executable initial state in this pack" if st is None else "unsupported")
        if key in self.sim.fidelity_failed:
            supported = False
            reason = "environment fidelity limit reached for this market"
        base = meta.asset1 if meta.asset0 == self.pack.numeraire else meta.asset0
        quote = self.pack.numeraire if base != meta.asset0 or meta.asset0 != self.pack.numeraire else meta.asset1
        if self.pack.numeraire not in (meta.asset0, meta.asset1):
            quote = meta.asset0
            base = meta.asset1
        pub = {
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
        # Concentrated-liquidity descriptors (present only when the record carries them).
        if meta.fee_pips is not None:
            pub["fee_pips"] = meta.fee_pips
        if meta.tick_spacing is not None:
            pub["tick_spacing"] = meta.tick_spacing
        if meta.hooks is not None:
            pub["hooks"] = self.alias.hook(meta.hooks)
        return pub

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
    def handle(self, request_id: str, tool: str, arguments: dict[str, Any] | None, *, decision_elapsed_ms: int = 0) -> Envelope:
        args = arguments or {}
        before = self.now
        try:
            if tool not in TOOLS:
                # A malformed name inside a known namespace is an invalid request; anything else the
                # environment does not provide is a typed capability error, never a fabricated answer.
                namespace = tool.split(".")[0] if "." in tool else ""
                if namespace in KNOWN_NAMESPACES and not any(c in tool for c in UNSUPPORTED_CAPABILITIES):
                    raise SessionError(ErrorCode.INVALID_REQUEST, f"unknown tool {tool}", {"known_tools": sorted(TOOLS)})
                raise SessionError(ErrorCode.UNSUPPORTED_CAPABILITY, f"{tool} is not implemented in this environment", {"unsupported_capabilities": UNSUPPORTED_CAPABILITIES})
            if not isinstance(args, dict):
                raise SessionError(ErrorCode.INVALID_REQUEST, "arguments must be an object")
            if self.paused and tool not in ("session.describe",):
                raise SessionError(ErrorCode.RUN_PAUSED, "run is paused by the operator; retry later")
            if self.finished and tool not in FREE_TOOLS:
                raise SessionError(ErrorCode.SESSION_FINISHED, "session.finish was already called")
            self._account_request(tool)
            if not self.finished:
                delay = decision_elapsed_ms if self.resource_profile.timing == "runner_measured" else (self.resource_profile.decision_latency_ms if tool not in FREE_TOOLS else 0)
                self.sim.process_until(min(self.now + max(0, delay), self.sim.end_ms))
            dispatch_args = args
            # A deadline that was future at receipt can pass while computation is charged.
            # Service it immediately; genuinely stale deadlines still fail validation.
            deadline_key = {"clock.advance": "to_ms", "clock.wait": "until_ms"}.get(tool)
            if deadline_key and type(args.get(deadline_key)) is int and before <= args[deadline_key] < self.now:
                dispatch_args = {**args, deadline_key: self.now}
            data, quality = self._dispatch(tool, dispatch_args)
            env = Envelope.ok(request_id=request_id, session_id=self.session_id, clock_ms=self.now, data=data, quality=quality)
        except SessionError as e:
            if e.code in (ErrorCode.INVALID_REQUEST, ErrorCode.INVALID_ORDER):
                self.budget.invalid_calls += 1
            env = Envelope.fail(request_id=request_id, session_id=self.session_id, clock_ms=self.now, code=e.code, message=e.message, details=self._public_details(e.details), quality=self._quality(Completeness.UNKNOWN))
        except SubmitRejected as e:
            code = ErrorCode(e.code) if e.code in ErrorCode.__members__ else ErrorCode.INVALID_ORDER
            if code in (ErrorCode.INVALID_ORDER,):
                self.budget.invalid_calls += 1
            env = Envelope.fail(request_id=request_id, session_id=self.session_id, clock_ms=self.now, code=code, message=e.message, details=self._public_details(e.details), quality=self._quality(Completeness.UNKNOWN))
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
                decision_elapsed_ms=decision_elapsed_ms,
                delivered=self._delivery_record(env),
            )
        )
        return env

    @staticmethod
    def _delivery_record(env: Envelope) -> dict[str, Any]:
        """Bounded record of what this command actually delivered, after leakage scanning."""
        payload = {"data": env.data, "quality": env.quality.model_dump(mode="json"),
                   "error": env.error.model_dump(mode="json") if env.error else None}
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return {"evidence_basis": "recorded_delivery", "sha256": hashlib.sha256(encoded.encode()).hexdigest(),
                "quality": payload["quality"],
                "payload": payload if len(encoded.encode()) <= 16_384 else None,
                "payload_omitted": len(encoded.encode()) > 16_384}

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
            limit = min(self.params.rate_limit.simulated_requests_per_minute, self.resource_profile.requests_per_minute)
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
        if tool == "clock.wait":
            return self.wait(args), self._quality(Completeness.COMPLETE)
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
        if tool == "session.snapshot":
            return self.snapshot(args)
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
            "objective": "Maximize final settled ETH (NATIVE), or CASH in practice episodes. Submit your own sells before the episode ends and allow time for confirmation. Unsold tokens and unconfirmed sale proceeds do not count toward final cash return; liquidatable portfolio value is secondary. session.finish does not sell holdings for you.",
            "mode": self.mode,
            "resource_profile": self.resource_profile.public(),
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
                "latency_assumptions": p.public_latency_assumptions() | {"computation_time_basis": self.resource_profile.public()["decision_latency_basis"]},
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
                "notifications": {"delivery_delay_ms": p.data_latency_ms, "max_conditions": 32,
                                  "lifetime": "one clock.wait call; client resubmits conditions on each wait",
                                  "computation_time": self.resource_profile.public()["decision_latency_basis"]},
            },
            "budgets": {
                "max_requests": self.budget.max_requests,
                "requests_used": self.budget.requests,
                "max_decisions": self.budget.max_decisions,
                "decisions_used": self.budget.decisions,
                "simulated_requests_per_minute": min(p.rate_limit.simulated_requests_per_minute, self.resource_profile.requests_per_minute),
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

    def _modeled_depth(self, key: str) -> int | None:
        meta = self.pack.pools[key]
        state = self.sim.pools.get(key)
        if (state is None or self.pack.numeraire not in (meta.asset0, meta.asset1)
                or key in self.sim.fidelity_failed or not (cpmm_supported(meta) or cl_supported(meta))):
            return None
        return state.depth_for(self.pack.numeraire)[0]

    def _visible_price(self, key: str) -> Fraction | None:
        trade = self.sim.obs[key].last_visible(self.now)
        if trade is None:
            return None
        base, quote = self._base_quote(key)
        n, d = (trade.amount_in, trade.amount_out) if trade.asset_in == quote else (trade.amount_out, trade.amount_in)
        return Fraction(n * 10**self._decimals(base), d * 10**self._decimals(quote)) if d else None

    def snapshot(self, args: dict[str, Any]) -> tuple[dict[str, Any], Quality]:
        since = self._int_arg(args, "since_ms")
        if since is not None and since > self.now:
            raise SessionError(ErrorCode.INVALID_REQUEST, "since_ms must not be in the future")
        window = self._int_arg(args, "window_ms", default=300_000, minimum=1, required=True)
        stale_after = self._int_arg(args, "stale_after_ms", default=window, minimum=1, required=True)
        limit = min(self._int_arg(args, "limit", default=25, minimum=1, required=True), self.params.budgets.max_page_size)
        cursors = {k: self._int_arg(args, k, default=0, minimum=0, required=True) for k in ("market_cursor", "discovery_cursor", "order_cursor")}
        # Discovery order is stable across pages as later pools become available.
        discovered = sorted(self.sim.discovered_pools(self.now), key=lambda k: (self.sim.pool_discovery_ms[k], self.alias.pool(k)))
        selected = discovered
        if "pool_ids" in args:
            ids = args["pool_ids"]
            if not isinstance(ids, list) or len(ids) > self.params.budgets.max_page_size:
                raise SessionError(ErrorCode.INVALID_REQUEST, "pool_ids must be a list within max_page_size")
            selected_keys = {self._resolve_pool_discovered(pid) for pid in ids}
            selected = [k for k in discovered if k in selected_keys]

        def page(rows, cursor_name):
            cursor = cursors[cursor_name]
            return rows[cursor:cursor + limit], cursor + limit if cursor + limit < len(rows) else None

        keys, next_market = page(selected, "market_cursor")
        rows = []
        for key in keys:
            pub = self._pool_public(key)
            obs = self.sim.obs[key]
            base, quote = self._base_quote(key)
            start = max(self.now - window, self.sim.pool_discovery_ms[key])
            trades = sorted(obs.visible_between(start, self.now, self.now), key=lambda t: (t.event_ms, t.seq))
            last = obs.last_visible(self.now)
            cov = obs.coverage_state(start, self.now) if start < self.now else None
            first_price = self._trade_public(trades[0], base, quote)["price_quote_per_base"] if trades else None
            last_price = self._trade_public(trades[-1], base, quote)["price_quote_per_base"] if trades else None
            # Compute the change from exact raw ratios, not the rounded wire prices.
            change = None
            if len(trades) >= 2:
                def price(t, quote=quote):
                    n, d = (t.amount_in, t.amount_out) if t.asset_in == quote else (t.amount_out, t.amount_in)
                    return Fraction(n, d) if d else None
                first, final = price(trades[0]), price(trades[-1])
                if first and final is not None:
                    change = fraction_to_decimal_str(final / first - 1, 18)
            state = self.sim.pools.get(key)
            depth = self._modeled_depth(key)
            restrictions = self._restrictions_for(key)
            pub.update({
                "newly_discovered": since is None or self.sim.pool_discovery_ms[key] > since,
                "last_trade": self._trade_public(last, base, quote) if last else None,
                "freshness": {"last_event_ms": last.event_ms if last else None,
                              "last_available_ms": last.available_ms if last else None,
                              "age_ms": self.now - last.event_ms if last else None,
                              "stale_after_ms": stale_after,
                              "stale": self.now - last.event_ms > stale_after if last else None},
                "activity": {"start_ms": start, "end_ms": self.now,
                             "observed_trade_count": len(trades),
                             "observed_volume_base_raw": str(sum(t.amount_in if t.asset_in == base else t.amount_out for t in trades)),
                             "observed_volume_quote_raw": str(sum(t.amount_in if t.asset_in == quote else t.amount_out for t in trades)),
                             "first_price_quote_per_base": first_price,
                             "last_price_quote_per_base": last_price,
                             "price_change_fraction": change,
                             "newly_available_trade_count": sum(t.available_ms > since for t in obs.visible(self.now)) if since is not None else obs.visible_count(self.now)},
                "coverage": str(cov) if cov else "unknown",
                "modeled_liquidity": {"basis": "modeled_private_market_state", "as_of_ms": self.now,
                                      "numeraire_depth_raw": None if depth is None else str(depth),
                                      "fee": {"numerator": state.fee_num, "denominator": state.fee_den} if depth is not None else None,
                                      "depth_kind": "active_range_virtual_depth" if isinstance(state, ClPoolState) else "cpmm_reserve" if state is not None else "unavailable"},
                "restrictions": {"basis": restrictions["basis"], "status": restrictions["status"]},
            })
            rows.append(pub)
        discoveries = [k for k in discovered if since is None or self.sim.pool_discovery_ms[k] > since]
        new_keys, next_discovery = page(discoveries, "discovery_cursor")
        changed_orders = [o for o in self.sim.orders.values() if since is None or any(t >= since for t, _ in o.history)]
        orders, next_order = page(changed_orders, "order_cursor")
        data = {
            "as_of_ms": self.now, "since_ms": since,
            "markets": {"items": rows, "next_cursor": next_market, "total": len(selected)},
            "discoveries": {"items": [self._pool_public(k) for k in new_keys], "next_cursor": next_discovery, "total": len(discoveries)},
            "orders": {"items": [o.to_public(self.alias) for o in orders], "next_cursor": next_order, "total": len(changed_orders)},
            "portfolio": self.portfolio(),
            "costs": {"gas_per_included_transaction_raw": self.params.gas_cost_raw, "gas_basis": self.params.gas_basis,
                      "quote_tool": "broker.quote", "note": "Pool fees are embedded in swap outputs. Request a quote for your chosen amount."},
            "note": "Activity counts only delivered observations, not all market activity. Missing coverage and undelivered data are not zero activity. Depth is current modeled state, not delayed trade data. Orders include changes at since_ms; deduplicate by order_id.",
        }
        warnings = ["OBSERVATION_DELAY_APPLIES"] if self.params.availability_delay_ms else []
        if any(r["coverage"] != "completed_and_checked" for r in rows):
            warnings.append("COVERAGE_INCOMPLETE")
        if any(r["last_trade"] is None for r in rows):
            warnings.append("NO_VISIBLE_TRADES")
        quality = self._quality(Completeness.PARTIAL if warnings else Completeness.COMPLETE, warnings)
        quality.stale = any(r["freshness"]["stale"] is True for r in rows)
        return data, quality

    def _watch_conditions(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        raw = args.get("conditions", [])
        if not isinstance(raw, list) or len(raw) > 32:
            raise SessionError(ErrorCode.INVALID_REQUEST, "conditions must be a list of at most 32 objects")
        conditions = []
        fields = {"new_pool": {"since_ms", "min_visible_trades", "min_numeraire_depth_raw"},
                  "price_cross": {"pool_id", "direction", "price"},
                  "liquidity_below": {"pool_id", "depth_raw"}, "order_terminal": {"order_id"}}
        for index, raw_condition in enumerate(raw):
            if not isinstance(raw_condition, dict):
                raise SessionError(ErrorCode.INVALID_REQUEST, "each condition must be an object")
            c = dict(raw_condition)
            kind = c.get("kind")
            if not isinstance(kind, str) or kind not in fields or c.keys() - fields[kind] - {"kind"}:
                raise SessionError(ErrorCode.INVALID_REQUEST, "unsupported condition kind or arguments")
            c["index"] = index
            if kind == "new_pool":
                c["since_ms"] = self._int_arg(c, "since_ms", default=self.now, required=True)
                if c["since_ms"] > self.now:
                    raise SessionError(ErrorCode.INVALID_REQUEST, "since_ms must not be in the future")
                c["min_visible_trades"] = self._int_arg(c, "min_visible_trades", default=0, minimum=0, required=True)
                c["depth"] = self._raw_arg(c, "min_numeraire_depth_raw") if "min_numeraire_depth_raw" in c else None
            elif kind in ("price_cross", "liquidity_below"):
                c["key"] = self._resolve_pool_discovered(c.get("pool_id"))
                if kind == "price_cross":
                    if c.get("direction") not in ("above", "below"):
                        raise SessionError(ErrorCode.INVALID_REQUEST, "direction must be above or below")
                    value = c.get("price")
                    try:
                        if not isinstance(value, str) or len(value) > 128:
                            raise ValueError
                        value = Decimal(value)
                        if not value.is_finite() or value <= 0 or abs(value.adjusted()) > 128:
                            raise ValueError
                        c["threshold"] = Fraction(value)
                    except (InvalidOperation, ValueError):
                        raise SessionError(ErrorCode.INVALID_REQUEST, "price must be a positive finite decimal string") from None
                    c["previous"] = self._visible_price(c["key"])
                else:
                    c["depth"] = self._raw_arg(c, "depth_raw")
                    if self._modeled_depth(c["key"]) is None:
                        raise SessionError(ErrorCode.UNSUPPORTED_CAPABILITY, "no modeled numeraire depth for this pool")
            else:
                if not isinstance(c.get("order_id"), str) or c["order_id"] not in self.sim.orders:
                    raise SessionError(ErrorCode.INVALID_REQUEST, "unknown order_id")
            conditions.append(c)
        return conditions

    def _watch_matches(self, conditions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        alerts = []
        for c in conditions:
            kind, match = c["kind"], None
            if kind == "new_pool":
                for key in sorted(self.sim.discovered_pools(self.now), key=lambda k: (self.sim.pool_discovery_ms[k], self.alias.pool(k))):
                    if self.sim.pool_discovery_ms[key] <= c["since_ms"] or self.sim.obs[key].visible_count(self.now) < c["min_visible_trades"]:
                        continue
                    depth = self._modeled_depth(key)
                    if c["depth"] is not None and (depth is None or depth < c["depth"]):
                        continue
                    match = {"pool_id": self.alias.pool(key), "listed_ms": self.sim.pool_discovery_ms[key]}
                    break
            elif kind == "price_cross":
                value, prev = self._visible_price(c["key"]), c["previous"]
                c["previous"] = value
                if value is not None and prev is not None:
                    crossed = prev < c["threshold"] <= value if c["direction"] == "above" else prev > c["threshold"] >= value
                    if crossed:
                        trade = self.sim.obs[c["key"]].last_visible(self.now)
                        match = {"pool_id": c["pool_id"], "price": fraction_to_decimal_str(value, 18),
                                 "event_ms": trade.event_ms, "available_ms": trade.available_ms}
            elif kind == "liquidity_below":
                depth = self._modeled_depth(c["key"])
                if depth is not None and depth < c["depth"]:
                    match = {"pool_id": c["pool_id"], "numeraire_depth_raw": str(depth), "basis": "modeled_private_market_state"}
            else:
                order = self.sim.orders[c["order_id"]]
                if order.state in TERMINAL_ORDER_STATES:
                    match = {"order_id": order.order_id, "state": str(order.state)}
            if match is not None:
                alerts.append({"condition_index": c["index"], "kind": kind, "matched_ms": self.now,
                               "delivery_ms": self.now + self.params.data_latency_ms, **match})
        return alerts

    def _watch_checkpoints(self, conditions: list[dict[str, Any]], until: int) -> list[int]:
        """Internal evaluation times. Only satisfied, delivered conditions can wake the client."""
        times = {self.now, until}
        price_keys = {c["key"] for c in conditions if c["kind"] == "price_cross"}
        depth_keys = {c["key"] for c in conditions if c["kind"] == "liquidity_below"}
        for c in conditions:
            if c["kind"] == "new_pool":
                keys = {k for k, d in self.sim.pool_discovery_ms.items() if c["since_ms"] < d <= until}
                times.update(self.sim.pool_discovery_ms[k] for k in keys)
                if c["min_visible_trades"]:
                    price_keys.update(keys)
                if c["depth"] is not None:
                    depth_keys.update(keys)
        for key in price_keys:
            # Delayed observations of events already processed, including own trades.
            for trade in reversed(self.sim.obs[key].trades):
                if trade.available_ms <= self.now:
                    break
                times.add(max(trade.available_ms, self.sim.pool_discovery_ms[key]))
        if price_keys or depth_keys:
            for i in range(self.sim.cursor, len(self.sim.tape)):
                event = self.sim.tape[i]
                if event.time_ms > until:
                    break
                discovery = self.sim.pool_discovery_ms.get(event.pool, INF)
                if event.pool in price_keys and event.kind in ("swap", "cl_swap"):
                    available = event.available_ms if event.available_ms is not None else event.time_ms + self.params.availability_delay_ms
                    times.add(max(event.time_ms, available, discovery))
                if event.pool in depth_keys:
                    times.add(max(event.time_ms, discovery))
        for order in self.sim.unresolved_orders():
            if order.inclusion_time_ms is not None:
                times.add(order.inclusion_time_ms)
                times.add(order.inclusion_time_ms + self.params.availability_delay_ms)
            if order.inclusion_block is not None:
                block = order.inclusion_block + self.params.confirm_blocks
                if block <= self.sim.schedule.last_block:
                    times.add(self.sim.schedule.time_of(block))
        return sorted(t for t in times if self.now <= t <= until)

    def wait(self, args: dict[str, Any]) -> dict[str, Any]:
        until = self._int_arg(args, "until_ms", required=True)
        if until < self.now:
            raise SessionError(ErrorCode.INVALID_REQUEST, "until_ms is in the past")
        conditions = self._watch_conditions(args)
        before, until = self.now, min(until, self.sim.end_ms)
        alerts = []
        for checkpoint in self._watch_checkpoints(conditions, until):
            self.sim.process_until(checkpoint)
            matched = self._watch_matches(conditions)
            if matched:
                delivery = matched[0]["delivery_ms"]
                self.sim.process_until(min(delivery, until))
                if delivery <= until:
                    alerts = matched
                break
        return {"advanced_from_ms": before, "clock_ms": self.now,
                "episode_ended": self.now >= self.sim.end_ms,
                "reason": "alert" if alerts else "episode_end" if self.now >= self.sim.end_ms else "deadline",
                "alerts": alerts,
                "note": "Conditions expire when this call returns. Notifications do not submit orders. A match whose delivery is after until_ms is not delivered; no notification is retained."}

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
        min_trades = self._int_arg(filters, "min_visible_trades")
        sort = str(args.get("sort") or "pool_id")
        if sort not in MARKETS_SORTS:
            raise SessionError(ErrorCode.INVALID_REQUEST, f"sort must be one of {sorted(MARKETS_SORTS)}")
        discovered = self.sim.discovered_pools(self.now)
        rows = []
        for key in discovered:
            # cheap fields first: a week of every launch lists thousands of pools per call
            listed = self.sim.pool_discovery_ms.get(key, self.now)
            age = self.now - listed
            if min_age is not None and age < min_age:
                continue
            if max_age is not None and age > max_age:
                continue
            obs = self.sim.obs[key]
            n = obs.visible_count(self.now)
            if min_trades is not None and n < min_trades:
                continue
            last = obs.last_visible(self.now)
            if active_since is not None and (last is None or last.event_ms < active_since):
                continue
            pub = self._pool_public(key)
            if venue is not None and pub["venue_model"] != venue:
                continue
            if supported_only and not pub["execution_supported"]:
                continue
            pub["last_trade_ms"] = last.event_ms if last else None
            pub["visible_trade_count"] = n
            st = self.sim.pools.get(key)
            # Depth on the cash side of the current in-range state: "0" means nothing can be bought here
            # now (liquidity pulled, or a launch whose range the price has left), whatever the trade count says.
            pub["numeraire_depth_raw"] = str(st.depth_for(self.pack.numeraire)[0]) if st is not None and pub["execution_supported"] else None
            rows.append(pub)
        if sort == "newest":
            rows.sort(key=lambda r: (-(r["listed_ms"] or 0), r["pool_id"]))
        elif sort == "most_traded":
            rows.sort(key=lambda r: (-r["visible_trade_count"], r["pool_id"]))
        elif sort == "recently_traded":
            rows.sort(key=lambda r: (-(r["last_trade_ms"] if r["last_trade_ms"] is not None else -1), r["pool_id"]))
        else:
            rows.sort(key=lambda r: r["pool_id"])
        page = rows[cursor : cursor + limit]
        next_cursor = cursor + limit if cursor + limit < len(rows) else None
        data = {
            "items": page,
            "next_cursor": next_cursor,
            "sort": sort,
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
        if st is None and cl_supported(meta) and self.sim.cl_init_ms.get(key, -INF) > self.now:
            raise SessionError(ErrorCode.NOT_YET_DISCOVERED, "identifier is not currently discoverable in this session")
        if st is None or not (cpmm_supported(meta) or cl_supported(meta)):
            raise SessionError(ErrorCode.UNSUPPORTED_CAPABILITY, f"no modeled liquidity for this venue: {meta.unsupported_reason or 'missing state'}")
        if key in self.sim.fidelity_failed:
            raise SessionError(ErrorCode.ENVIRONMENT_FIDELITY_LIMIT, "market left the model's validated domain")
        if isinstance(st, ClPoolState):
            x, y = st.virtual_reserves()
            data = {
                "pool_id": self.alias.pool(key),
                "basis": "modeled_private_market_state",
                "model": str(meta.model),
                "sqrt_price_x96": str(st.sqrt_price_x96),
                "tick": st.tick,
                "liquidity": str(st.liquidity),
                "virtual_depth": [
                    {"asset_id": self.alias.asset(st.asset0), "depth_raw": str(x)},
                    {"asset_id": self.alias.asset(st.asset1), "depth_raw": str(y)},
                ],
                "fee_pips": st.fee_pips,
                "fee": {"numerator": st.fee_num, "denominator": st.fee_den},
                "tick_spacing": st.tick_spacing,
                "initialized_ticks_near_price": [{"tick": t, "liquidity_net": str(net)} for t, net, _gross in st.ticks_near(8)],
                "halted": st.halted,
                "as_of_ms": self.now,
                "note": "State is the private model copy after your own fills; virtual_depth is the active tick range only (L*2^96/sqrtP, L*sqrtP/2^96), not the pool's token balances nor router depth.",
            }
            return data, self._quality(Completeness.COMPLETE)
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
        intent = {}
        for field_name in ("reason", "exit_condition"):
            if field_name in args:
                value = args[field_name]
                if not isinstance(value, str) or len(value) > 512 or self.scanner.scan({field_name: value}):
                    raise SessionError(ErrorCode.INVALID_ORDER, "intent metadata must be at most 512 characters and contain no private identifiers")
                intent[field_name] = value
        order, created = self.sim.submit(
            pool_key=key,
            asset_in=asset_in,
            asset_out=asset_out,
            amount_in=amount_in,
            min_amount_out=min_out,
            deadline_ms=deadline,
            idempotency_key=idem,
            quote_id=quote_id,
            intent=intent,
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
            "note": "Primary result is final settled ETH/cash return. Unsold holdings receive no primary credit; their liquidatable value is reported separately. No automatic sale.",
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


def replay_trace(pack: Pack, trace: list[dict[str, Any]], *, bankroll_raw: int, mask_seed: str, engine_seed: str, session_id: str = "replay", mode: str = "practice", resource_profile: dict | None = None) -> Session:
    """Re-execute recorded agent commands against a fresh session; used for action-replay reproducibility."""
    s = Session.create(session_id=session_id, pack=pack, bankroll_raw=bankroll_raw, mask_seed=mask_seed, engine_seed=engine_seed, mode=mode, resource_profile=resource_profile)
    for r in trace:
        s.handle(r["request_id"], r["tool"], r.get("arguments") or {}, decision_elapsed_ms=r.get("decision_elapsed_ms", 0))
        if r.get("delivered"):
            s.trace[-1].delivered = r["delivered"]
        else:
            s.trace[-1].delivered["evidence_basis"] = "reconstructed_under_current_engine"
    return s
