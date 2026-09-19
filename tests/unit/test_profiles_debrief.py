"""Resource clocks and metadata must survive action replay without changing trade semantics."""

from dataclasses import asdict
from fractions import Fraction

import httpx
import pytest

from market_replay.domain.envelope import Envelope
from market_replay.domain.profiles import PROFILES, effective_pack
from market_replay.engine.session import Session, replay_trace
from market_replay.evaluation.debrief import attribution, timeline
from market_replay.evaluation.report import build_report
from market_replay.evaluation.validity import execution_validity
from market_replay.runners.inprocess import InProcessTransport


def session(pack, profile="controlled_v1"):
    return Session.create(
        session_id="ses_profile",
        pack=pack,
        bankroll_raw=1_000_000,
        mask_seed="m",
        engine_seed="e",
        resource_profile=PROFILES[profile].model_dump(),
    )


@pytest.mark.parametrize("profile", ["controlled_v1", "deployment_v1", "adverse_execution_v1"])
def test_time_charges_and_crossed_deadline_replay(fresh_pack, profile):
    s = session(fresh_pack, profile)
    first = s.handle("1", "session.snapshot", {}, decision_elapsed_ms=1_000)
    expected = 1_000 if profile == "deployment_v1" else 500
    assert first.clock_ms == expected + s.params.data_latency_ms
    assert s.budget.max_requests == 20_000
    before = s.now
    env = s.handle("2", "clock.wait", {"until_ms": before + 1}, decision_elapsed_ms=1_000)
    assert env.status == "ok" and env.data["reason"] == "deadline"
    env = s.handle("3", "clock.advance", {"to_ms": s.now + 1}, decision_elapsed_ms=1_000)
    assert env.status == "ok"
    assert s.handle("4", "clock.wait", {"until_ms": 0}).status == "error"
    s.handle("finish", "session.finish", {"confirm": True}, decision_elapsed_ms=1_000)
    trace = [asdict(r) for r in s.trace]
    replay = replay_trace(
        fresh_pack,
        trace,
        bankroll_raw=1_000_000,
        mask_seed="m",
        engine_seed="e",
        session_id=s.session_id,
        resource_profile=PROFILES[profile].model_dump(),
    )
    assert replay.result_hash() == s.result_hash()
    assert [asdict(r) for r in replay.trace] == trace
    assert timeline(s, 0, 2)["next_cursor"] == 2
    assert timeline(s, 100)["items"] == []
    assert all(r["delivered"]["sha256"] for r in timeline(s)["items"])


def test_stress_is_explicit_and_leaves_source_pack_unchanged(fresh_pack):
    base = fresh_pack.params.model_dump()
    stressed = effective_pack(fresh_pack, PROFILES["adverse_execution_v1"])
    assert stressed.params.gas_cost == 2 * fresh_pack.params.gas_cost
    assert stressed.params.submit_latency_ms == 2 * fresh_pack.params.submit_latency_ms
    assert stressed.params.confirm_blocks == 2 * fresh_pack.params.confirm_blocks
    assert fresh_pack.params.model_dump() == base
    report = build_report(session(fresh_pack, "adverse_execution_v1"), run_meta={"state": "completed"})
    assert report["execution_evidence"]["flow_basis"] == "fixed_flow_with_stress_assumptions"
    assert report["execution_evidence"]["mechanics"]["transaction_cancellation"] == "excluded"


def test_runner_charges_thinking_and_excludes_handler_time(monkeypatch):
    # First response returns at 200ms, next request arrives at 203ms. The server's
    # first 198ms is excluded; only 2ms then 3ms are charged.
    ticks = iter([0, 2_000_000, 200_000_000, 203_000_000, 400_000_000])
    monkeypatch.setattr("market_replay.runners.inprocess.time.monotonic_ns", lambda: next(ticks))
    elapsed = []

    def handler(*args, **kwargs):
        elapsed.append(kwargs["measured_elapsed_ms"])
        return Envelope.ok(request_id="r", session_id="s", clock_ms=0, data={})

    transport = InProcessTransport(handler, "agt_test", True)
    request = httpx.Request(
        "POST", "http://inprocess/agent/v1/commands", headers={"Authorization": "Bearer agt_test"}, json={}
    )
    assert transport.handle_request(request).status_code == 200
    assert transport.handle_request(request).status_code == 200
    assert elapsed == [2, 3]


def test_intent_is_presubmission_scanned_idempotent_and_attributed(fresh_pack):
    s = session(fresh_pack, "pack_defaults_v1")
    s.handle("advance", "clock.advance", {"to_ms": 300_000})
    key = s.sim.discovered_pools(s.now)[0]
    st = s.sim.pools[key]
    cash = fresh_pack.numeraire
    token = st.asset1 if st.asset0 == cash else st.asset0
    args = {
        "pool_id": s.alias.pool(key),
        "asset_in": s.alias.asset(cash),
        "asset_out": s.alias.asset(token),
        "amount_in_raw": "1000",
        "min_amount_out_raw": "0",
        "deadline_ms": 400_000,
        "idempotency_key": "buy",
        "reason": "Reviewing visible activity",
        "exit_condition": "Review at the next deadline",
    }
    assert s.handle("oversize", "broker.submit", {**args, "reason": "x" * 513}).status == "error"
    assert s.handle("private", "broker.submit", {**args, "reason": key}).status == "error"
    submitted = s.handle("buy", "broker.submit", args)
    assert submitted.status == "ok", submitted.error
    assert s.handle("retry", "broker.submit", args).status == "ok"
    assert s.handle("conflict", "broker.submit", {**args, "reason": "different"}).status == "error"
    assert len(s.sim.orders) == 1
    buy = next(iter(s.sim.orders.values()))
    assert buy.intent["reason"] == args["reason"]
    s.handle("wait", "clock.wait", {"until_ms": 320_000})
    assert str(buy.state) == "confirmed"
    sale, _ = s.sim.submit(
        pool_key=key,
        asset_in=token,
        asset_out=cash,
        amount_in=buy.amount_out,
        min_amount_out=0,
        deadline_ms=400_000,
        idempotency_key="sale",
    )
    s.sim.process_until(340_000)
    a = attribution(s)
    assert len(a["sales"]) == 1
    assert (
        Fraction(a["sales"][0]["contribution_raw"])
        == sale.amount_out - sale.gas_charged - buy.amount_in - buy.gas_charged
    )
    assert a["largest_asset_share_of_buy_notional"] == "1.00000000"
    rows = timeline(s)["items"]
    assert next(r for r in rows if r["request"].get("reason") == "different")["order"] is None
    delivered = next(r for r in rows if r["tool"] == "broker.submit" and r["status"] == "ok")
    assert delivered["delivered"]["payload"]["data"]["order"]["intent"] == buy.intent


def test_eligibility_gates_are_separate_from_cash_and_capacity(fresh_pack):
    s = session(fresh_pack)
    s.finish()
    report = build_report(s, run_meta={"state": "completed"})
    assert report["execution_validity"]["eligible"]
    report["outcome"]["valuation_complete"] = False
    report["activity"]["model_capacity_rejected"] = 5
    assert execution_validity(report)["eligible"]
    report["unresolved"]["environment_fidelity_flags"] = [{"code": "failure"}]
    assert execution_validity(report)["failed_gates"] == ["no_fidelity_failures"]
    report["activity"]["budget_exhausted"] = True
    assert "within_request_and_decision_budgets" in execution_validity(report)["failed_gates"]


def test_comparison_excludes_incompatible_and_failed_runs(fresh_pack):
    from copy import deepcopy

    from market_replay.evaluation.compare import pair_runs

    s = session(fresh_pack)
    s.finish()
    report = build_report(s, run_meta={"state": "completed"})
    a = {
        "run_id": "a",
        "pack_id": "p",
        "state": "completed",
        "report": report,
        "profile_hash": "profile",
        "mask_seed": "same",
    }
    b = deepcopy(a) | {"run_id": "b"}
    assert pair_runs([a], [b])["summary"]["episodes_paired"] == 1
    b["profile_hash"] = "different"
    compared = pair_runs([a], [b])
    assert compared["summary"]["episodes_paired"] == 0
    assert compared["per_episode"][0]["incompatible_dimensions"] == ["profile_hash"]
    b["profile_hash"] = "profile"
    b["report"]["unresolved"]["environment_fidelity_flags"] = [{"code": "failure"}]
    assert pair_runs([a], [b])["summary"]["episodes_paired"] == 0
