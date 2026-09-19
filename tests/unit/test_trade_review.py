from fastapi.testclient import TestClient

from market_replay.broker.ledger import AGENT_AVAILABLE
from market_replay.engine.session import Session
from market_replay.evaluation.trade_review import build_trade_review
from market_replay.service.app import create_app
from market_replay.service.runs import RunManager


def test_review_matches_ledger_and_does_not_change_session(fresh_pack):
    s = Session.create(session_id="ses_review", pack=fresh_pack, bankroll_raw=1_000_000, mask_seed="m", engine_seed="e")
    sim = s.sim
    sim.process_until(300_000)
    key = sim.discovered_pools(sim.now_ms)[0]
    pool = sim.pools[key]
    cash = fresh_pack.numeraire
    token = pool.asset1 if pool.asset0 == cash else pool.asset0
    buy, _ = sim.submit(pool_key=key, asset_in=cash, asset_out=token, amount_in=1000, min_amount_out=0, deadline_ms=400_000, idempotency_key="buy")
    sim.process_until(320_000)
    quantity = sim.ledger.balance(AGENT_AVAILABLE, token)
    sell, _ = sim.submit(pool_key=key, asset_in=token, asset_out=cash, amount_in=quantity // 2, min_amount_out=0, deadline_ms=400_000, idempotency_key="sell")
    sim.process_until(340_000)
    before = (sim.now_ms, sim.state_hash(), sim.ledger.content_hash(), s.trace_hash(), s.budget.requests)
    review = build_trade_review(s)
    row = review["tokens"][0]
    assert row["eth_spent_raw"] == "1000"
    assert int(row["eth_recovered_raw"]) == sell.amount_out
    assert int(row["net_cash_raw"]) == sim.ledger.balance(AGENT_AVAILABLE, cash) - 1_000_000
    assert int(row["remaining_raw"]) == quantity - quantity // 2
    assert row["average_hold_ms"] == sell.fill_time_ms - buy.fill_time_ms
    assert [e["side"] for e in review["events"]] == ["buy", "sell"]
    assert all(e["price"] is not None for e in review["events"])
    assert token not in str(review) and key not in str(review)
    assert before == (sim.now_ms, sim.state_hash(), sim.ledger.content_hash(), s.trace_hash(), s.budget.requests)


def test_failed_orders_have_no_cash_proceeds(fresh_pack):
    s = Session.create(session_id="ses_review", pack=fresh_pack, bankroll_raw=1_000_000, mask_seed="m", engine_seed="e")
    sim = s.sim
    key = sim.discovered_pools(0)[0]
    pool = sim.pools[key]
    cash = fresh_pack.numeraire
    token = pool.asset1 if pool.asset0 == cash else pool.asset0
    order, _ = sim.submit(pool_key=key, asset_in=cash, asset_out=token, amount_in=1000, min_amount_out=10**30, deadline_ms=100_000, idempotency_key="bad")
    sim.process_until(20_000)
    assert str(order.state) == "reverted"
    review = build_trade_review(s)
    assert review["tokens"][0]["eth_spent_raw"] == "0"
    assert int(review["tokens"][0]["net_cash_raw"]) == -order.gas_charged
    assert review["events"][0]["price"] is None
    assert review["events"][0]["reason"]


def test_endpoint_is_opt_in_terminal_only_and_supports_stored_runs(tmp_path, dev_pack_dir):
    mgr = RunManager(data_dir=tmp_path / "data", hosted=True)
    try:
        mgr.import_pack(dev_pack_dir, "test")
        client = TestClient(create_app(mgr, "adm_test"))
        run = client.post("/api/v1/runs", json={"agent": {"name": "reviewer", "version": "1"}, "pack_id": "test"}).json()
        rid = run["run_id"]
        assert client.get(f"/api/v1/runs/{rid}/trade-review").status_code == 409
        token = run["session_credential"]["token"]
        mgr.handle_command(token, "done", "session.finish", {})
        before = dict(mgr.store.run(rid))
        stored = mgr.store.get_doc(rid, "trade_review")
        assert isinstance(stored, dict) and stored["events"] == []  # kept at the end of the run, while the session was live
        mgr._contexts.clear()
        response = client.get(f"/api/v1/runs/{rid}/trade-review")
        assert response.status_code == 200, response.text
        assert response.json() == stored and rid not in mgr._contexts  # served from the store: no session rebuild
        assert dict(mgr.store.run(rid)) == before
        # a run finished before reviews were stored is rebuilt from its trace once, then kept
        mgr.store.put_doc(rid, "trade_review", None)
        response = client.get(f"/api/v1/runs/{rid}/trade-review")
        assert response.status_code == 200 and response.json()["events"] == [] and isinstance(mgr.store.get_doc(rid, "trade_review"), dict)
        assert client.get(f"/api/v1/runs/{rid}/trade-review", headers={"Authorization": f"Bearer {token}"}).status_code == 403
    finally:
        mgr.close()
