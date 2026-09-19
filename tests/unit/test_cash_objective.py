"""Final cash scoring must require exits and survive incomplete token valuations."""
import json
from decimal import Decimal

from market_replay.broker.ledger import AGENT_AVAILABLE, AGENT_PENDING, AGENT_RESERVED, Leg
from market_replay.engine.session import Session
from market_replay.evaluation.compare import pair_runs
from market_replay.evaluation.report import build_report
from market_replay.service.runs import RunManager


def test_unsold_tokens_do_not_count_and_confirmed_sales_do(fresh_pack):
    s = Session.create(session_id="ses_cash", pack=fresh_pack, bankroll_raw=1_000_000, mask_seed="m", engine_seed="e")
    sim = s.sim
    sim.process_until(300_000)
    pk = sim.discovered_pools(sim.now_ms)[0]
    pool = sim.pools[pk]
    cash = fresh_pack.numeraire
    token = pool.asset1 if pool.asset0 == cash else pool.asset0
    buy, _ = sim.submit(pool_key=pk, asset_in=cash, asset_out=token, amount_in=1000, min_amount_out=0, deadline_ms=400_000, idempotency_key="buy")
    sim.process_until(320_000)
    assert str(buy.state) == "confirmed"
    report = build_report(s, run_meta={})
    oc = report["outcome"]
    assert oc["primary_metric"] == "final_cash_return_v1"
    assert int(oc["final_cash_raw"]) == 1_000_000 - 1000 - buy.gas_charged
    assert Decimal(oc["headline_return"]) < Decimal(oc["liquidatable_portfolio_return"])
    before = int(oc["final_cash_raw"])
    sale, _ = sim.submit(pool_key=pk, asset_in=token, asset_out=cash, amount_in=sim.ledger.balance(AGENT_AVAILABLE, token), min_amount_out=0, deadline_ms=400_000, idempotency_key="sell")
    sim.process_until(340_000)
    assert str(sale.state) == "confirmed"
    assert int(build_report(s, run_meta={})["outcome"]["final_cash_raw"]) == before + sale.amount_out - sale.gas_charged


def test_reserved_cash_counts_pending_proceeds_do_not_and_missing_value_is_secondary(fresh_pack):
    s = Session.create(session_id="ses_cash", pack=fresh_pack, bankroll_raw=1_000_000, mask_seed="m", engine_seed="e")
    cash = fresh_pack.numeraire
    s.sim.ledger.post(0, "reserve", "test", [Leg(AGENT_AVAILABLE, cash, -100), Leg(AGENT_RESERVED, cash, 100)])
    s.sim.ledger.post(0, "fill", "test", [Leg("pool.test", cash, -500), Leg(AGENT_PENDING, cash, 500)])
    valuation = s.sim.value_portfolio()
    valuation.equity = None
    valuation.complete = False
    s.sim.value_portfolio = lambda: valuation
    oc = build_report(s, run_meta={})["outcome"]
    assert oc["final_cash_raw"] == "1000000"
    assert Decimal(oc["headline_return"]) == 0
    assert oc["liquidatable_portfolio_return"] is None
    assert not oc["valuation_complete"]


def test_leaderboard_accepts_cash_score_without_token_valuation_and_excludes_legacy(tmp_path, dev_pack_dir):
    mgr = RunManager(data_dir=tmp_path / "data", hosted=True)
    try:
        mgr.import_pack(dev_pack_dir, "test")
        agent = mgr.register_agent(name="cash", version="1", runtime="external", capabilities=[], config={})
        run = mgr.create_run(agent_id=agent["agent_id"], pack_ref="test")
        mgr.handle_command(run["session_credential"]["token"], "finish", "session.finish", {"confirm": True})
        row = dict(mgr.store.run(run["run_id"]))
        rep = json.loads(row["report_json"])
        rep["outcome"]["valuation_complete"] = False
        # Exercise ranking on stored reports, independently of the session cache.
        row["report_json"] = json.dumps(rep)
        original = mgr.store.runs
        mgr.store.runs = lambda **kwargs: [row]
        assert len(mgr.leaderboard(pack_id="test")["rows"]) == 1
        rep["outcome"].pop("primary_metric")
        row["report_json"] = json.dumps(rep)
        assert mgr.leaderboard(pack_id="test")["rows"] == []
        mgr.real_weeks = lambda: [dict(mgr.store.pack("test"))]
        assert mgr.episodes_for(agent["agent_id"])[0]["status"] == "new"
        mgr.store.runs = original
    finally:
        mgr.close()


def test_comparisons_do_not_mix_old_portfolio_scores_with_cash_scores():
    old = {"run_id": "old", "pack_id": "p", "state": "completed", "report": {"outcome": {"headline_return": "2"}}}
    new = {"run_id": "new", "pack_id": "p", "state": "completed", "report": {"outcome": {"primary_metric": "final_cash_return_v1", "headline_return": "0"}}}
    result = pair_runs([old], [new])
    assert result["summary"]["episodes_paired"] == 0
    assert "LEGACY_PORTFOLIO_SCORES_EXCLUDED_RERUN_REQUIRED" in result["warnings"]


def test_finish_does_not_liquidate_unsold_holdings(fresh_pack):
    s = Session.create(session_id="ses_finish", pack=fresh_pack, bankroll_raw=1_000_000, mask_seed="m", engine_seed="e")
    cash = fresh_pack.numeraire
    pools = s.handle("list", "markets.list", {}).data["items"]
    pool = next(p for p in pools if p["execution_supported"])
    private = s.sim.pools[s.alias.resolve(pool["pool_id"])[1]]
    token = private.asset1 if private.asset0 == cash else private.asset0
    order, _ = s.sim.submit(pool_key=s.alias.resolve(pool["pool_id"])[1], asset_in=cash, asset_out=token, amount_in=1000, min_amount_out=0, deadline_ms=100_000, idempotency_key="buy")
    s.finish()
    assert str(order.state) == "confirmed"
    assert s.sim.ledger.balance(AGENT_AVAILABLE, token) > 0
    outcome = build_report(s, run_meta={})["outcome"]
    assert int(outcome["final_cash_raw"]) == 1_000_000 - 1000 - order.gas_charged
