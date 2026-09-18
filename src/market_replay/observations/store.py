"""Agent-visible information state, separate from the internal market state.

Trades are appended by the engine with an ``available_ms``. Every query filters by
``available_ms <= as_of`` *before* aggregating. Candles are computed from visible
trades only; a bar closes when its boundary plus the availability delay has passed.
Missing intervals are only marked ``synthetic_empty_bar`` when the coverage ledger
confirms the interval is complete; otherwise they remain missing.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from fractions import Fraction

from ..domain.status import AvailabilityBasis, Completeness, CoverageState


@dataclass(slots=True)
class TradeObs:
    seq: int
    pool: str
    event_ms: int
    available_ms: int
    block: int
    log_index: int
    tx: str | None
    wallet: str | None
    asset_in: str
    asset_out: str
    amount_in: int
    amount_out: int
    reserve0_after: int  # CPMM: reserve; CL: virtual depth of the active range (L * 2^96 / sqrtP)
    reserve1_after: int  # CPMM: reserve; CL: virtual depth of the active range (L * sqrtP / 2^96)
    origin: str  # "external" | "own"
    order_id: str | None = None
    # concentrated-liquidity fills only
    sqrt_price_x96_after: int | None = None
    tick_after: int | None = None
    liquidity_after: int | None = None


@dataclass(slots=True)
class CoverageSpan:
    start_ms: int
    end_ms: int
    state: CoverageState
    evidence: str


class PoolObservations:
    __slots__ = ("pool", "trades", "_avail_keys", "coverage")

    def __init__(self, pool: str) -> None:
        self.pool = pool
        self.trades: list[TradeObs] = []
        self._avail_keys: list[int] = []  # available_ms, must be non-decreasing for bisect
        self.coverage: list[CoverageSpan] = []

    def append(self, t: TradeObs) -> None:
        # Appends arrive in event order; availability delay is constant per pack so available order == event order.
        if self._avail_keys and t.available_ms < self._avail_keys[-1]:
            # Insert while keeping ordering by available_ms (late observations).
            i = bisect.bisect_right(self._avail_keys, t.available_ms)
            self.trades.insert(i, t)
            self._avail_keys.insert(i, t.available_ms)
        else:
            self.trades.append(t)
            self._avail_keys.append(t.available_ms)

    def visible(self, as_of: int) -> list[TradeObs]:
        i = bisect.bisect_right(self._avail_keys, as_of)
        return self.trades[:i]

    def visible_count(self, as_of: int) -> int:
        return bisect.bisect_right(self._avail_keys, as_of)

    def visible_between(self, start_ms: int, end_ms: int, as_of: int) -> list[TradeObs]:
        """Trades with event_ms in [start, end] whose availability <= as_of. end is clamped by caller."""
        vis = self.visible(as_of)
        return [t for t in vis if start_ms <= t.event_ms <= end_ms]

    def last_visible(self, as_of: int) -> TradeObs | None:
        i = bisect.bisect_right(self._avail_keys, as_of)
        return self.trades[i - 1] if i else None

    def coverage_state(self, start_ms: int, end_ms: int) -> CoverageState:
        """Coverage of [start, end) for swaps: complete only if fully covered by completed spans."""
        if not self.coverage:
            return CoverageState.MISSING
        covered_to = start_ms
        worst = CoverageState.COMPLETED_AND_CHECKED
        for span in sorted(self.coverage, key=lambda s: s.start_ms):
            if span.end_ms <= covered_to:
                continue
            if span.start_ms > covered_to:
                return CoverageState.MISSING
            if span.state != CoverageState.COMPLETED_AND_CHECKED:
                worst = CoverageState.PARTIAL
            covered_to = max(covered_to, span.end_ms)
            if covered_to >= end_ms:
                break
        if covered_to < end_ms:
            return CoverageState.MISSING
        return worst


def _price(t: TradeObs, base_asset: str) -> Fraction | None:
    """Price of base asset in the other asset for a trade."""
    if t.asset_in == base_asset:
        if t.amount_in == 0:
            return None
        return Fraction(t.amount_out, t.amount_in)
    if t.amount_out == 0:
        return None
    return Fraction(t.amount_in, t.amount_out)


@dataclass(slots=True)
class Candle:
    start_ms: int
    end_ms: int
    open: Fraction | None
    high: Fraction | None
    low: Fraction | None
    close: Fraction | None
    volume_base: int
    volume_quote: int
    trade_count: int
    closed: bool
    synthetic_empty_bar: bool
    completeness: Completeness

    def to_public(self, places: int = 18) -> dict:
        from ..domain.quantities import fraction_to_decimal_str as f2s

        def fmt(x: Fraction | None) -> str | None:
            return None if x is None else f2s(x, places)

        return {
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "open": fmt(self.open),
            "high": fmt(self.high),
            "low": fmt(self.low),
            "close": fmt(self.close),
            "volume_base_raw": str(self.volume_base),
            "volume_quote_raw": str(self.volume_quote),
            "trade_count": self.trade_count,
            "closed": self.closed,
            "synthetic_empty_bar": self.synthetic_empty_bar,
            "completeness": str(self.completeness),
        }


def aggregate_candles(
    obs: PoolObservations,
    *,
    base_asset: str,
    interval_ms: int,
    start_ms: int,
    end_ms: int,
    as_of: int,
    availability_delay_ms: int,
    include_partial: bool = False,
) -> tuple[list[Candle], list[dict]]:
    """Generic OHLCV over visible trades. ``end_ms`` must already be clamped to the allowed present.

    Returns (candles, gaps). A bar is closed when ``bar_end + availability_delay <= as_of``.
    Empty closed bars in intervals with complete coverage are synthetic-empty (previous close, zero
    volume). Empty bars whose coverage is not complete are omitted and listed as gaps.
    """
    if interval_ms <= 0:
        raise ValueError("interval_ms must be positive")
    first_bucket = (start_ms // interval_ms) * interval_ms
    trades = obs.visible_between(first_bucket, end_ms, as_of)
    by_bucket: dict[int, list[TradeObs]] = {}
    for t in trades:
        b = (t.event_ms // interval_ms) * interval_ms
        by_bucket.setdefault(b, []).append(t)
    candles: list[Candle] = []
    gaps: list[dict] = []
    prev_close: Fraction | None = None
    # Seed prev_close from the last visible trade before the first bucket.
    before = [t for t in obs.visible(as_of) if t.event_ms < first_bucket]
    if before:
        prev_close = _price(before[-1], base_asset)
    b = first_bucket
    while b <= end_ms:
        bar_end = b + interval_ms
        closed = bar_end + availability_delay_ms <= as_of
        rows = by_bucket.get(b, [])
        if not closed and not include_partial:
            b = bar_end
            continue
        if rows:
            prices = [p for p in (_price(t, base_asset) for t in rows) if p is not None]
            vol_base = sum(t.amount_in if t.asset_in == base_asset else t.amount_out for t in rows)
            vol_quote = sum(t.amount_out if t.asset_in == base_asset else t.amount_in for t in rows)
            cov = obs.coverage_state(b, bar_end)
            comp = Completeness.COMPLETE if cov == CoverageState.COMPLETED_AND_CHECKED and closed else Completeness.PARTIAL
            if not closed:
                comp = Completeness.PARTIAL
            c = Candle(
                start_ms=b,
                end_ms=bar_end,
                open=prices[0] if prices else None,
                high=max(prices) if prices else None,
                low=min(prices) if prices else None,
                close=prices[-1] if prices else None,
                volume_base=vol_base,
                volume_quote=vol_quote,
                trade_count=len(rows),
                closed=closed,
                synthetic_empty_bar=False,
                completeness=comp,
            )
            candles.append(c)
            if prices:
                prev_close = prices[-1]
        else:
            cov = obs.coverage_state(b, bar_end) if closed else CoverageState.PENDING
            if closed and cov == CoverageState.COMPLETED_AND_CHECKED:
                candles.append(
                    Candle(
                        start_ms=b,
                        end_ms=bar_end,
                        open=prev_close,
                        high=prev_close,
                        low=prev_close,
                        close=prev_close,
                        volume_base=0,
                        volume_quote=0,
                        trade_count=0,
                        closed=True,
                        synthetic_empty_bar=True,
                        completeness=Completeness.EMPTY_VERIFIED,
                    )
                )
            elif closed:
                gaps.append({"start_ms": b, "end_ms": bar_end, "reason": f"coverage_{cov}"})
            elif include_partial:
                # open bar with no trades yet: report as open, not as an empty verified bar
                candles.append(
                    Candle(
                        start_ms=b,
                        end_ms=bar_end,
                        open=None,
                        high=None,
                        low=None,
                        close=None,
                        volume_base=0,
                        volume_quote=0,
                        trade_count=0,
                        closed=False,
                        synthetic_empty_bar=False,
                        completeness=Completeness.PARTIAL,
                    )
                )
        b = bar_end
    return candles, gaps


def basis_for(origin: str) -> AvailabilityBasis:
    if origin == "generated_fixture":
        return AvailabilityBasis.FIXTURE_DELAY_MODEL
    if origin == "historical_reconstruction":
        return AvailabilityBasis.RECONSTRUCTED_WITH_DELAY_MODEL
    return AvailabilityBasis.UNKNOWN
