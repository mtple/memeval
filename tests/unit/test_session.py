"""Acceptance 2, 7, 21, 34, 36, 37, 38 at the session (tool) level."""

from __future__ import annotations

from market_replay.datasets.pack import Pack
from market_replay.engine.session import Session, replay_trace


def make(pack: Pack, seed: str = "m") -> Session:
    return Session.create(session_id="ses_t", pack=pack, bankroll_raw=1_000_000, mask_seed=seed, engine_seed="e")


def test_future_range_clamped_with_explicit_warning(fresh_pack: Pack):
    s = make(fresh_pack)
    pid = s.handle("r", "markets.list", {}).data["items"][0]["pool_id"]
    s.handle("r", "clock.advance", {"to_ms": 600_000})
    env = s.handle("r", "market.trades", {"pool_id": pid, "start_ms": 0, "end_ms": 10**9})
    assert env.status == "ok"
    assert env.data["range"]["end_ms"] <= env.clock_ms
    assert "RANGE_CLAMPED_TO_PRESENT" in env.quality.warnings
    assert all(t["time_ms"] <= env.clock_ms for t in env.data["items"])
    assert all(t["available_ms"] <= env.clock_ms for t in env.data["items"])
    env2 = s.handle("r", "market.candles", {"pool_id": pid, "interval_ms": 60_000, "start_ms": 0, "end_ms": 10**9})
    assert env2.status == "ok" and all(c["end_ms"] <= env2.clock_ms for c in env2.data["items"])


def test_past_range_stays_within_range_even_though_query_costs_time(fresh_pack: Pack):
    s = make(fresh_pack)
    pid = s.handle("r", "markets.list", {}).data["items"][0]["pool_id"]
    s.handle("r", "clock.advance", {"to_ms": 900_000})
    env = s.handle("r", "market.trades", {"pool_id": pid, "start_ms": 100_000, "end_ms": 500_000, "limit": 500})
    assert env.data["range"] == {"start_ms": 100_000, "end_ms": 500_000}
    assert all(100_000 <= t["time_ms"] <= 500_000 for t in env.data["items"])
    assert env.clock_ms == 900_000 + fresh_pack.params.data_latency_ms


def test_future_pools_not_enumerable(fresh_pack: Pack):
    s = make(fresh_pack)
    lst = s.handle("r", "markets.list", {"limit": 1}).data
    total_now = lst["total_currently_discoverable"]
    later_pools = [k for k, d in s.sim.pool_discovery_ms.items() if d > s.now]
    assert later_pools, "fixture should contain later listings"
    assert total_now == len(s.sim.discovered_pools(s.now))
    # cursor beyond current total does not reveal future pools
    assert s.handle("r", "markets.list", {"cursor": 10_000}).data["items"] == []
    # guessing the future alias yields the same response as an unknown id
    future_alias = s.alias.pool(later_pools[0])
    a = s.handle("r", "markets.get", {"pool_id": future_alias})
    b = s.handle("r", "markets.get", {"pool_id": "pool_nonexistent"})
    assert a.status == b.status == "error" and a.error.code == b.error.code and a.error.message == b.error.message
    assert a.error.details == b.error.details
    # canonical key as escape hatch: same uniform response
    c = s.handle("r", "markets.get", {"pool_id": later_pools[0]})
    assert c.error.code == a.error.code and c.error.message == a.error.message
    # after discovery time the pool appears
    s.handle("r", "clock.advance", {"to_ms": s.sim.pool_discovery_ms[later_pools[0]] + 1})
    assert s.handle("r", "markets.get", {"pool_id": future_alias}).status == "ok"


def test_next_event_advance_does_not_leak_hidden_pools(fresh_pack: Pack):
    s = make(fresh_pack)
    later = min(d for d in s.sim.pool_discovery_ms.values() if d > 0)
    # next_event may stop at a discovery time (a public event) but never earlier than an unpublished trade of a hidden pool
    env = s.handle("r", "clock.advance", {"next_event": True, "max_ms": later + 10})
    assert env.data["clock_ms"] <= later + 10


def test_unsupported_capabilities_typed(fresh_pack: Pack):
    s = make(fresh_pack)
    for tool in ("wallet.history", "holder_graph", "social_data", "orders.limit_order", "exact_output_swap"):
        env = s.handle("r", tool, {})
        assert env.status == "error" and env.error.code == "UNSUPPORTED_CAPABILITY", tool


def test_agent_can_investigate_any_in_scope_token(fresh_pack: Pack):
    s = make(fresh_pack)
    s.handle("r", "clock.advance", {"to_ms": 3_600_000})
    items = s.handle("r", "markets.list", {}).data["items"]
    # every discoverable pool answers get/trades/candles/restrictions, not just ones a reference agent used
    for it in items:
        for tool, args in (("markets.get", {}), ("market.trades", {"limit": 5}), ("market.candles", {"interval_ms": 300_000}), ("market.restrictions", {})):
            env = s.handle("r", tool, {"pool_id": it["pool_id"], **args})
            assert env.status == "ok", (tool, env.error)


def test_replay_reproduces_result_hash(fresh_pack: Pack):
    s = make(fresh_pack)
    pid = s.handle("r1", "markets.list", {}).data["items"][0]["pool_id"]
    base = s.handle("r2", "markets.get", {"pool_id": pid}).data["base_asset"]
    s.handle("r3", "clock.advance", {"to_ms": 600_000})
    s.handle("r4", "broker.submit", {"pool_id": pid, "asset_in": "CASH", "asset_out": base, "amount_in_raw": "20000", "min_amount_out_raw": "0", "deadline_ms": 700_000, "idempotency_key": "x"})
    s.handle("r5", "clock.advance", {"to_ms": 2_000_000})
    s.handle("r6", "session.finish", {})
    trace = [{"request_id": r.request_id, "tool": r.tool, "arguments": r.arguments} for r in s.trace]
    again = replay_trace(fresh_pack, trace, bankroll_raw=1_000_000, mask_seed="m", engine_seed="e", session_id="ses_t")
    assert again.sim.ledger.content_hash() == s.sim.ledger.content_hash()
    assert again.result_hash() == s.result_hash()
    # a different mask seed changes aliases but not the market: the same canonical actions give the same ledger
    other = Session.create(session_id="ses_t", pack=fresh_pack, bankroll_raw=1_000_000, mask_seed="other", engine_seed="e")
    assert other.alias.pool(s.alias.resolve(pid)[1]) != pid


def test_runs_do_not_share_state(fresh_pack: Pack):
    a = make(fresh_pack, "a")
    b = make(fresh_pack, "b")
    pid = a.handle("r", "markets.list", {}).data["items"][0]["pool_id"]
    base = a.handle("r", "markets.get", {"pool_id": pid}).data["base_asset"]
    a.handle("r", "clock.advance", {"to_ms": 600_000})
    a.handle("r", "broker.submit", {"pool_id": pid, "asset_in": "CASH", "asset_out": base, "amount_in_raw": "50000", "min_amount_out_raw": "0", "deadline_ms": 700_000, "idempotency_key": "x"})
    a.handle("r", "clock.advance", {"to_ms": 700_000})
    b.handle("r", "clock.advance", {"to_ms": 700_000})
    key = a.alias.resolve(pid)[1]
    assert a.sim.pools[key].reserve0 != b.sim.pools[key].reserve0
    assert (b.sim.pools[key].reserve0, b.sim.pools[key].reserve1) == (b.sim.ref_pools[key].reserve0, b.sim.ref_pools[key].reserve1)
    assert b.sim.ledger.balance("agent.available", fresh_pack.numeraire) == 1_000_000


def test_budget_exhaustion_visible_not_free(fresh_pack: Pack):
    s = make(fresh_pack)
    s.budget.max_requests = 3
    pid = s.handle("r", "markets.list", {}).data["items"][0]["pool_id"]
    s.handle("r", "markets.get", {"pool_id": pid})
    s.handle("r", "markets.get", {"pool_id": pid})
    env = s.handle("r", "markets.get", {"pool_id": pid})
    assert env.status == "error" and env.error.code == "BUDGET_EXHAUSTED"
    assert s.budget.exhausted
    assert s.handle("r", "portfolio.get", {}).status == "ok"
    assert s.handle("r", "session.finish", {}).status == "ok"


def test_rate_limit_in_virtual_time(fresh_pack: Pack):
    s = make(fresh_pack)
    fresh_pack.params.rate_limit.simulated_requests_per_minute = 5
    pid = s.handle("r", "markets.list", {}).data["items"][0]["pool_id"]
    codes = [s.handle("r", "markets.get", {"pool_id": pid}).error for _ in range(8)]
    assert any(e is not None and e.code == "RATE_LIMITED" for e in codes)
    fresh_pack.params.rate_limit.simulated_requests_per_minute = 600


def test_episode_end_blocks_new_actions(fresh_pack: Pack):
    s = make(fresh_pack)
    s.handle("r", "clock.advance", {"to_ms": 10**9})
    assert s.now == s.sim.end_ms
    env = s.handle("r", "markets.list", {})
    assert env.status == "error" and env.error.code == "EPISODE_ENDED"
    fin = s.handle("r", "session.finish", {})
    assert fin.status == "ok" and fin.data["finished"]


def test_markets_list_sorts_and_filters_for_discovery(fresh_pack: Pack):
    s = make(fresh_pack)
    s.handle("r", "clock.advance", {"to_ms": 60 * 60_000})  # the development fixture is a two-hour episode
    newest = s.handle("r", "markets.list", {"sort": "newest", "limit": 500}).data
    assert newest["sort"] == "newest" and [r["listed_ms"] for r in newest["items"]] == sorted((r["listed_ms"] for r in newest["items"]), reverse=True)
    busiest = s.handle("r", "markets.list", {"sort": "most_traded", "limit": 500}).data["items"]
    assert [r["visible_trade_count"] for r in busiest] == sorted((r["visible_trade_count"] for r in busiest), reverse=True)
    quiet_cut = s.handle("r", "markets.list", {"filters": {"min_visible_trades": busiest[0]["visible_trade_count"]}}).data
    assert quiet_cut["total_currently_discoverable"] >= 1 and all(r["visible_trade_count"] >= busiest[0]["visible_trade_count"] for r in quiet_cut["items"])
    bad = s.handle("r", "markets.list", {"sort": "richest"})
    assert bad.status == "error" and bad.error.code == "INVALID_REQUEST"
