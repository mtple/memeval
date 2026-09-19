"""Terminal behavior: information timing, delayed watches, accounting and replay."""

import json
from dataclasses import replace

import pytest

from market_replay.engine.session import Session, replay_trace
from market_replay.observations.store import PoolObservations, TradeObs


def make(pack):
    return Session.create(
        session_id="terminal", pack=pack, bankroll_raw=1_000_000, mask_seed="m", engine_seed="e"
    )


def quiet(pack):
    s = make(pack)
    s.sim.tape = []  # isolate observation delivery without mutating the shared pack tape
    s.sim.cursor = 0
    for key in s.sim.obs:
        s.sim.obs[key] = PoolObservations(key)
        s.sim.pool_discovery_ms[key] = -1
    key = next(k for k in s.sim.pools if s._modeled_depth(k) is not None)
    return s, key, s.alias.pool(key)


def trade(s, key, *, seq, event, available, price):
    base, quote = s._base_quote(key)
    s.sim.obs[key].append(
        TradeObs(
            seq=seq,
            pool=key,
            event_ms=event,
            available_ms=available,
            block=s.sim.schedule.first_block,
            log_index=seq,
            tx=None,
            wallet=None,
            asset_in=quote,
            asset_out=base,
            amount_in=price * 10 ** s._decimals(quote),
            amount_out=10 ** s._decimals(base),
            reserve0_after=1,
            reserve1_after=1,
            origin="external",
        )
    )


def test_snapshot_is_budgeted_visible_and_explicit_about_missing_data(fresh_pack):
    s, key, pid = quiet(fresh_pack)
    trade(s, key, seq=1, event=0, available=0, price=1)
    trade(s, key, seq=2, event=50, available=5_000, price=9)
    env = s.handle("r", "session.snapshot", {"pool_ids": [pid], "window_ms": 1000, "stale_after_ms": 10})
    assert env.status == "ok", env.error
    assert s.now == fresh_pack.params.data_latency_ms
    assert s.budget.requests == 1 and len(s.request_times) == 1
    row = env.data["markets"]["items"][0]
    assert row["activity"]["observed_trade_count"] == 1
    assert row["activity"]["price_change_fraction"] is None
    assert row["last_trade"]["available_ms"] <= s.now
    assert row["freshness"]["stale"] is True and env.quality.stale
    assert row["coverage"] == "missing" and "COVERAGE_INCOMPLETE" in env.quality.warnings
    assert row["modeled_liquidity"]["basis"] == "modeled_private_market_state"
    assert isinstance(row["modeled_liquidity"]["numeraire_depth_raw"], str)
    assert env.data["portfolio"]["valuation"]["cash_available_raw"] == "1000000"
    assert not s.scanner.scan(env.model_dump(mode="json"))
    s.handle("a", "clock.advance", {"to_ms": 5000})
    row = s.handle(
        "r2", "session.snapshot", {"pool_ids": [pid], "since_ms": env.clock_ms, "window_ms": 6000}
    ).data["markets"]["items"][0]
    assert row["activity"]["newly_available_trade_count"] == 1
    assert row["activity"]["price_change_fraction"] == "8.000000000000000000"


def test_snapshot_watchlist_does_not_hide_discoveries_or_reveal_future_pools(fresh_pack):
    s = make(fresh_pack)
    future = next(k for k, d in s.sim.pool_discovery_ms.items() if d > s.now)
    first = s.handle("r", "session.snapshot", {"pool_ids": [], "limit": 1})
    assert first.data["markets"]["items"] == []
    assert first.data["discoveries"]["items"] and first.data["discoveries"]["next_cursor"] == 1
    assert first.data["discoveries"]["total"] == len(s.sim.discovered_pools(s.now))
    for pid in (s.alias.pool(future), "pool_unknown", future):
        denied = s.handle("r", "session.snapshot", {"pool_ids": [pid]})
        assert denied.error.code == "NOT_YET_DISCOVERED"
    s.handle("a", "clock.advance", {"to_ms": s.sim.pool_discovery_ms[future]})
    delta = s.handle("d", "session.snapshot", {"since_ms": first.clock_ms, "pool_ids": []})
    assert s.alias.pool(future) in [p["pool_id"] for p in delta.data["discoveries"]["items"]]


def test_new_pool_alert_waits_for_discovery_and_delivery(fresh_pack):
    s, key, pid = quiet(fresh_pack)
    s.sim.pool_discovery_ms[key] = 2000
    args = {"until_ms": 1900, "conditions": [{"kind": "new_pool"}]}
    early = s.handle("r", "clock.wait", args)
    assert early.status == "ok" and early.data["alerts"] == [] and s.now == 1900
    wake = s.handle("r2", "clock.wait", {**args, "until_ms": 5000})
    assert wake.data["reason"] == "alert" and s.now == 2000 + s.params.data_latency_ms
    assert wake.data["alerts"][0]["pool_id"] == pid
    assert wake.data["alerts"][0]["matched_ms"] == 2000
    assert not s.scanner.scan(wake.model_dump(mode="json"))
    assert not s.sim.orders and s.budget.decisions == 0


def test_new_pool_thresholds_wait_for_delivered_activity(fresh_pack):
    s, key, pid = quiet(fresh_pack)
    s.sim.pool_discovery_ms[key] = 2000
    trade(s, key, seq=1, event=1000, available=4000, price=1)
    wake = s.handle(
        "r",
        "clock.wait",
        {
            "until_ms": 6000,
            "conditions": [
                {"kind": "new_pool", "min_visible_trades": 1, "min_numeraire_depth_raw": "1"},
            ],
        },
    )
    assert wake.status == "ok", wake.error
    assert wake.data["alerts"][0]["pool_id"] == pid and wake.clock_ms == 4100


def test_price_crossing_waits_for_availability_and_latches_during_delivery(fresh_pack):
    s, key, pid = quiet(fresh_pack)
    trade(s, key, seq=1, event=0, available=0, price=1)
    trade(s, key, seq=2, event=1000, available=4000, price=3)
    trade(s, key, seq=3, event=2000, available=4050, price=1)
    conditions = [{"kind": "price_cross", "pool_id": pid, "direction": "above", "price": "2"}]
    wake = s.handle("r", "clock.wait", {"until_ms": 6000, "conditions": conditions})
    assert wake.status == "ok", wake.error
    assert wake.clock_ms == 4100
    alert = wake.data["alerts"][0]
    assert alert["event_ms"] == 1000 and alert["available_ms"] == alert["matched_ms"] == 4000
    assert alert["price"] == "3.000000000000000000"
    assert s._visible_price(key) == 1  # the market changes while the notification is in flight
    assert conditions == [{"kind": "price_cross", "pool_id": pid, "direction": "above", "price": "2"}]


def test_price_watch_does_not_wake_for_future_data_or_an_existing_level(fresh_pack):
    s, key, pid = quiet(fresh_pack)
    trade(s, key, seq=1, event=0, available=0, price=3)
    trade(s, key, seq=2, event=1000, available=4000, price=1)
    wake = s.handle(
        "r",
        "clock.wait",
        {
            "until_ms": 3999,
            "conditions": [
                {"kind": "price_cross", "pool_id": pid, "direction": "below", "price": "2"},
                {"kind": "price_cross", "pool_id": pid, "direction": "above", "price": "2"},
            ],
        },
    )
    assert wake.data["alerts"] == [] and wake.clock_ms == 3999
    wake = s.handle(
        "r2",
        "clock.wait",
        {
            "until_ms": 6000,
            "conditions": [
                {"kind": "price_cross", "pool_id": pid, "direction": "below", "price": "2"},
            ],
        },
    )
    assert wake.clock_ms == 4100 and wake.data["alerts"]


def test_liquidity_burn_wakes_on_modeled_state_then_delivery(fresh_pack):
    s = make(fresh_pack)
    key = next(k for k in s.sim.discovered_pools(0) if s._modeled_depth(k) is not None)
    state = s.sim.pools[key]
    depth = s._modeled_depth(key)
    event = replace(
        s.sim.tape[-1],
        time_ms=2000,
        pool=key,
        kind="burn",
        amount0=state.reserve0 // 2,
        amount1=state.reserve1 // 2,
    )
    s.sim.tape, s.sim.cursor = [event], 0
    wake = s.handle(
        "r",
        "clock.wait",
        {
            "until_ms": 6000,
            "conditions": [
                {"kind": "liquidity_below", "pool_id": s.alias.pool(key), "depth_raw": str(depth * 3 // 4)},
            ],
        },
    )
    assert wake.status == "ok", wake.error
    assert wake.clock_ms == 2100 and wake.data["alerts"][0]["matched_ms"] == 2000
    assert wake.data["alerts"][0]["basis"] == "modeled_private_market_state"


@pytest.mark.parametrize("revert", [False, True])
def test_order_terminal_alert_and_snapshot_explain_execution(fresh_pack, revert):
    s = make(fresh_pack)
    pool = s.handle("m", "markets.list", {"filters": {"execution_supported_only": True}}).data["items"][0]
    order = s.handle(
        "o",
        "broker.submit",
        {
            "pool_id": pool["pool_id"],
            "asset_in": "CASH",
            "asset_out": pool["base_asset"],
            "amount_in_raw": "1000",
            "min_amount_out_raw": str(10**30 if revert else 0),
            "deadline_ms": 60000,
            "idempotency_key": "intent",
        },
    ).data["order"]
    wake = s.handle(
        "w",
        "clock.wait",
        {"until_ms": 60000, "conditions": [{"kind": "order_terminal", "order_id": order["order_id"]}]},
    )
    assert wake.status == "ok", wake.error
    assert wake.data["alerts"][0]["state"] == ("reverted" if revert else "confirmed")
    assert wake.clock_ms == wake.data["alerts"][0]["matched_ms"] + s.params.data_latency_ms
    snap = s.handle("s", "session.snapshot", {"since_ms": 0, "pool_ids": []}).data
    assert snap["orders"]["items"][0]["state"] == ("reverted" if revert else "confirmed")
    assert snap["orders"]["items"][0]["gas_charged_raw"] == s.params.gas_cost_raw
    assert snap["portfolio"]["pending_orders"] == []


def test_wait_deadline_never_delivers_early_and_clamps_to_episode(fresh_pack):
    s, key, pid = quiet(fresh_pack)
    s.sim.pool_discovery_ms[key] = 2000
    wake = s.handle("w", "clock.wait", {"until_ms": 2050, "conditions": [{"kind": "new_pool"}]})
    assert wake.clock_ms == 2050 and wake.data["reason"] == "deadline" and not wake.data["alerts"]
    end = s.handle("e", "clock.wait", {"until_ms": s.sim.end_ms + 100000})
    assert end.clock_ms == s.sim.end_ms and end.data["episode_ended"]
    s.handle("f", "session.finish", {})
    assert s.handle("x", "clock.wait", {"until_ms": s.now + 1}).error.code == "SESSION_FINISHED"


@pytest.mark.parametrize(
    "tool,args",
    [
        ("session.snapshot", {"pool_ids": "all"}),
        ("session.snapshot", {"limit": None}),
        ("session.snapshot", {"window_ms": None}),
        ("session.snapshot", {"market_cursor": -1}),
        ("session.snapshot", {"since_ms": 10**12}),
        ("clock.wait", {"until_ms": None}),
        ("clock.wait", {"until_ms": -1}),
        ("clock.wait", {"until_ms": 1000, "conditions": [None]}),
        ("clock.wait", {"until_ms": 1000, "conditions": [{"kind": "new_pool"}] * 33}),
        ("clock.wait", {"until_ms": 1000, "conditions": [{"kind": []}]}),
        ("clock.wait", {"until_ms": 1000, "conditions": [{"kind": "new_pool", "min_visible_trades": None}]}),
        ("clock.wait", {"until_ms": 1000, "conditions": [{"kind": "new_pool", "best_opportunity": True}]}),
        ("clock.wait", {"until_ms": 1000, "conditions": [{"kind": "order_terminal", "order_id": []}]}),
    ],
)
def test_terminal_invalid_arguments_return_typed_errors(fresh_pack, tool, args):
    env = make(fresh_pack).handle("r", tool, args)
    assert env.status == "error" and env.error.code == "INVALID_REQUEST"


@pytest.mark.parametrize("price", [1.5, None, "NaN", "Infinity", "-1", "0", "1/2", "1e10000"])
def test_invalid_price_thresholds_are_rejected(fresh_pack, price):
    s, key, pid = quiet(fresh_pack)
    env = s.handle(
        "r",
        "clock.wait",
        {
            "until_ms": 1000,
            "conditions": [
                {"kind": "price_cross", "pool_id": pid, "direction": "above", "price": price},
            ],
        },
    )
    assert env.status == "error" and env.error.code == "INVALID_REQUEST"


def test_terminal_budget_and_rate_limits_apply(fresh_pack):
    s = make(fresh_pack)
    s.budget.max_requests = 1
    assert s.handle("s", "session.snapshot", {}).status == "ok"
    assert s.handle("w", "clock.wait", {"until_ms": 6000}).error.code == "BUDGET_EXHAUSTED"
    assert s.handle("f", "session.finish", {}).status == "ok"
    fresh_pack.params.rate_limit.simulated_requests_per_minute = 1
    s = make(fresh_pack)
    assert s.handle("w", "clock.wait", {"until_ms": 10}).status == "ok"
    assert s.handle("s", "session.snapshot", {}).error.code == "RATE_LIMITED"


def test_terminal_replays_and_sessions_do_not_share_watches(fresh_pack):
    s = make(fresh_pack)
    first = s.handle("s", "session.snapshot", {})
    s.handle("w", "clock.wait", {"until_ms": s.sim.end_ms, "conditions": [{"kind": "new_pool"}]})
    s.handle("s2", "session.snapshot", {"since_ms": first.clock_ms})
    other = make(fresh_pack)
    assert other.now == 0 and not other.trace
    again = replay_trace(
        fresh_pack,
        [{"request_id": r.request_id, "tool": r.tool, "arguments": r.arguments} for r in s.trace],
        bankroll_raw=1_000_000,
        mask_seed="m",
        engine_seed="e",
        session_id="terminal",
    )
    assert again.result_hash() == s.result_hash() and again.now == s.now


def test_compact_snapshot_preserves_values_pagination_quality_and_time(fresh_pack):
    full, compact = make(fresh_pack), make(fresh_pack)
    a = full.handle("s", "session.snapshot", {"limit": 2})
    b = compact.handle("s", "session.snapshot", {"limit": 2, "format": "compact"})
    assert a.status == b.status == "ok"
    assert a.clock_ms == b.clock_ms and a.quality == b.quality
    assert full.budget == compact.budget
    for name in ("markets", "discoveries"):
        assert a.data[name]["total"] == b.data[name]["total"]
        assert a.data[name]["next_cursor"] == b.data[name]["next_cursor"]
    rows = [dict(zip(b.data["markets"]["columns"], r, strict=True)) for r in b.data["markets"]["items"]]
    for expanded, row in zip(a.data["markets"]["items"], rows, strict=True):
        assert row["pool_id"] == expanded["pool_id"]
        assert row["observed_volume_quote_raw"] == expanded["activity"]["observed_volume_quote_raw"]
        assert row["last_available_ms"] == expanded["freshness"]["last_available_ms"]
        assert row["stale"] == expanded["freshness"]["stale"]
        assert row["numeraire_depth_raw"] == expanded["modeled_liquidity"]["numeraire_depth_raw"]
        assert row["restriction_basis"] == expanded["restrictions"]["basis"]
    assert a.data["portfolio"] == b.data["portfolio"] and a.data["orders"] == b.data["orders"]
    assert len(json.dumps(b.data)) < len(json.dumps(a.data))
    assert not compact.scanner.scan(b.model_dump(mode="json"))
    assert compact.handle("invalid", "session.snapshot", {"format": "unknown"}).error.code == "INVALID_REQUEST"
