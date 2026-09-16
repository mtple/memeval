"""Acceptance 1, 3, 4, 5, 6: as-of cutoffs, open candles, missing minutes, late observations."""

from __future__ import annotations

from market_replay.domain.status import Completeness, CoverageState
from market_replay.observations.store import CoverageSpan, PoolObservations, TradeObs, aggregate_candles


def obs_with(trades: list[tuple[int, int, int, int]], coverage: list[tuple[int, int, CoverageState]]) -> PoolObservations:
    o = PoolObservations("p")
    for i, (event_ms, avail_ms, ain, aout) in enumerate(trades, start=1):
        o.append(TradeObs(seq=i, pool="p", event_ms=event_ms, available_ms=avail_ms, block=i, log_index=0, tx=None, wallet=None, asset_in="Q", asset_out="B", amount_in=ain, amount_out=aout, reserve0_after=0, reserve1_after=0, origin="external"))
    for s, e, st in coverage:
        o.coverage.append(CoverageSpan(s, e, st, "test"))
    return o


def test_future_events_do_not_change_earlier_view():
    o = obs_with([(10_000, 11_500, 100, 200)], [(0, 10**9, CoverageState.COMPLETED_AND_CHECKED)])
    before = [t.seq for t in o.visible(20_000)]
    o.append(TradeObs(seq=99, pool="p", event_ms=30_000, available_ms=31_500, block=9, log_index=0, tx=None, wallet=None, asset_in="Q", asset_out="B", amount_in=1, amount_out=1, reserve0_after=0, reserve1_after=0, origin="external"))
    assert [t.seq for t in o.visible(20_000)] == before
    # an event that already happened but is not yet available is invisible
    assert [t.seq for t in o.visible(30_500)] == before


def test_open_candle_not_reported_as_closed():
    o = obs_with([(61_000, 62_500, 100, 200), (125_000, 126_500, 100, 210)], [(0, 10**9, CoverageState.COMPLETED_AND_CHECKED)])
    # as_of 126_600: bar [120k,180k) is open; bar [60k,120k) closes at 120k+1.5k delay
    candles, gaps = aggregate_candles(o, base_asset="B", interval_ms=60_000, start_ms=60_000, end_ms=126_600, as_of=126_600, availability_delay_ms=1_500)
    assert [c.start_ms for c in candles] == [60_000]
    assert candles[0].closed and candles[0].trade_count == 1
    with_partial, _ = aggregate_candles(o, base_asset="B", interval_ms=60_000, start_ms=60_000, end_ms=126_600, as_of=126_600, availability_delay_ms=1_500, include_partial=True)
    assert [c.start_ms for c in with_partial] == [60_000, 120_000]
    assert with_partial[1].closed is False and with_partial[1].completeness == Completeness.PARTIAL


def test_missing_minute_is_gap_not_zero_volume():
    # coverage complete for minute 0 and 2, missing for minute 1
    o = obs_with([(5_000, 6_500, 100, 200), (125_000, 126_500, 100, 210)], [(0, 60_000, CoverageState.COMPLETED_AND_CHECKED), (120_000, 180_000, CoverageState.COMPLETED_AND_CHECKED)])
    candles, gaps = aggregate_candles(o, base_asset="B", interval_ms=60_000, start_ms=0, end_ms=180_000, as_of=200_000, availability_delay_ms=1_500)
    starts = [c.start_ms for c in candles]
    assert 60_000 not in starts
    assert gaps and gaps[0]["start_ms"] == 60_000 and "coverage_missing" in gaps[0]["reason"]


def test_verified_empty_interval_marked_synthetic():
    o = obs_with([(5_000, 6_500, 100, 200), (125_000, 126_500, 100, 210)], [(0, 10**9, CoverageState.COMPLETED_AND_CHECKED)])
    candles, gaps = aggregate_candles(o, base_asset="B", interval_ms=60_000, start_ms=0, end_ms=180_000, as_of=200_000, availability_delay_ms=1_500)
    empty = [c for c in candles if c.start_ms == 60_000]
    assert len(empty) == 1 and empty[0].synthetic_empty_bar and empty[0].volume_base == 0
    assert empty[0].close == candles[0].close  # carries previous observation
    assert empty[0].completeness == Completeness.EMPTY_VERIFIED
    assert not gaps


def test_late_observation_does_not_rewrite_earlier_view():
    o = obs_with([(10_000, 11_500, 100, 200)], [(0, 10**9, CoverageState.COMPLETED_AND_CHECKED)])
    early_view = aggregate_candles(o, base_asset="B", interval_ms=60_000, start_ms=0, end_ms=70_000, as_of=70_000, availability_delay_ms=1_500)[0]
    # A late-published trade from minute 0 becomes available only at 80_000
    o.append(TradeObs(seq=50, pool="p", event_ms=20_000, available_ms=80_000, block=2, log_index=0, tx=None, wallet=None, asset_in="Q", asset_out="B", amount_in=100, amount_out=999, reserve0_after=0, reserve1_after=0, origin="external"))
    again = aggregate_candles(o, base_asset="B", interval_ms=60_000, start_ms=0, end_ms=70_000, as_of=70_000, availability_delay_ms=1_500)[0]
    assert [c.to_public() for c in again] == [c.to_public() for c in early_view]
    later = aggregate_candles(o, base_asset="B", interval_ms=60_000, start_ms=0, end_ms=90_000, as_of=90_000, availability_delay_ms=1_500)[0]
    assert later[0].trade_count == 2  # revision visible only from its availability onward


def test_coverage_state_composition():
    o = obs_with([], [(0, 100, CoverageState.COMPLETED_AND_CHECKED), (100, 200, CoverageState.PARTIAL), (300, 400, CoverageState.COMPLETED_AND_CHECKED)])
    assert o.coverage_state(0, 100) == CoverageState.COMPLETED_AND_CHECKED
    assert o.coverage_state(0, 200) == CoverageState.PARTIAL
    assert o.coverage_state(0, 400) == CoverageState.MISSING
    assert o.coverage_state(250, 260) == CoverageState.MISSING
