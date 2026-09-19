"""Recover a reporting failure without another attempt or any new session commands."""
import json
from decimal import InvalidOperation

import pytest
from fastapi.testclient import TestClient

from market_replay.evaluation.report import build_report
from market_replay.service.app import create_app
from market_replay.service.runs import RunManager


@pytest.fixture
def failed_run(tmp_path, dev_pack_dir, monkeypatch):
    manager = RunManager(data_dir=tmp_path / "data")
    manager.import_pack(dev_pack_dir, "test")
    agent = manager.register_agent(name="repair", version="1", runtime="external", capabilities=[], config={})
    run = manager.create_run(agent_id=agent["agent_id"], pack_ref="test")
    def fail(*args, **kwargs):
        raise InvalidOperation("test report failure")
    monkeypatch.setattr("market_replay.service.runs.build_report", fail)
    assert manager.handle_command(run["session_credential"]["token"], "finish", "session.finish", {}).status == "ok"
    monkeypatch.setattr("market_replay.service.runs.build_report", build_report)
    client = TestClient(create_app(manager, "adm_repair", public_runs=True))
    yield manager, client, run
    manager.close()


@pytest.mark.parametrize("legacy", [False, True])
def test_repair_from_cold_state_preserves_trace_and_finish_time(failed_run, legacy):
    manager, client, run = failed_run
    rid = run["run_id"]
    before = dict(manager.store.run(rid))
    trace = manager.store.trace(rid)
    old = json.loads(before["report_json"])
    if legacy:
        old.pop("report_failure")
        manager.store.update_run(rid, report_json=json.dumps(old))
    assert client.get(f"/api/v1/runs/{rid}").json()["result_summary"] is None
    failed_report = client.get(f"/api/v1/runs/{rid}/report")
    assert failed_report.status_code == 503
    assert failed_report.json()["code"] == "REPORT_GENERATION_FAILED"
    hashes = manager._ctx(rid).session.result_hash()
    manager._contexts.clear()
    response = client.post(f"/api/v1/runs/{rid}/report/repair")
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["activity"]["confirmed_fills"] == 0  # Real zero for this cash-only test.
    assert report["run"]["state"] == "completed"
    assert report["report_recovery"]["previous_error"] == before["error"]
    after = manager.store.run(rid)
    assert after["error"] is None
    assert after["finished_at"] == before["finished_at"]
    assert after["clock_ms"] == before["clock_ms"]
    assert manager.store.trace(rid) == trace
    assert manager._ctx(rid).session.result_hash() == hashes
    assert len(manager.store.runs()) == 1
    assert client.post(f"/api/v1/runs/{rid}/report/repair").json() == report


@pytest.mark.parametrize("problem", ["other_failure", "missing_receipt", "mismatched_receipt", "wrong_state"])
def test_repair_refuses_to_change_unverified_terminal_state(failed_run, problem):
    manager, client, run = failed_run
    rid = run["run_id"]
    if problem == "other_failure":
        manager.store.update_run(rid, error="runner crashed")
    elif problem == "wrong_state":
        manager.store.update_run(rid, state="aborted")
    else:
        # Override the stored trace read, leaving the cached terminal state intact.
        trace = manager.store.trace(rid)
        if problem == "missing_receipt":
            trace[-1]["delivered"]["payload"] = None
        else:
            trace[-1]["delivered"]["payload"]["data"]["clock_ms"] += 1
        manager.store.trace = lambda *args, **kwargs: [] if "after" in kwargs else trace
    before = dict(manager.store.run(rid))
    assert client.post(f"/api/v1/runs/{rid}/report/repair").status_code == 409
    assert manager.store.run(rid) == before


def test_failed_repair_leaves_original_failure_and_auth_boundary(failed_run, monkeypatch):
    manager, client, run = failed_run
    path = f"/api/v1/runs/{run['run_id']}/report/repair"
    before = dict(manager.store.run(run["run_id"]))
    assert client.post(path, headers={"Authorization": "Bearer " + run["session_credential"]["token"]}).status_code == 403
    def fail(*args, **kwargs):
        raise RuntimeError("still failing")
    monkeypatch.setattr("market_replay.service.runs.build_report", fail)
    with pytest.raises(RuntimeError, match="still failing"):
        client.post(path)
    assert manager.store.run(run["run_id"]) == before


def test_recovery_preserves_fidelity_exclusion(failed_run):
    from market_replay.engine.simulation import FidelityFlag
    manager, client, run = failed_run
    ctx = manager._ctx(run["run_id"])
    ctx.session.sim.fidelity_flags.append(FidelityFlag(time_ms=ctx.session.now,
        pool=next(iter(ctx.session.pack.pools)), code="ENVIRONMENT_FIDELITY_LIMIT", message="recorded failure"))
    response = client.post(f"/api/v1/runs/{run['run_id']}/report/repair")
    assert response.status_code == 200
    assert response.json()["provisional"]
    assert response.json()["execution_validity"]["failed_gates"] == ["no_fidelity_failures"]
    assert len(ctx.session.sim.fidelity_flags) == 1


def test_repair_respects_public_write_policy_and_private_runs(failed_run):
    manager, client, run = failed_run
    path = f"/api/v1/runs/{run['run_id']}/report/repair"
    closed = TestClient(create_app(manager, "adm_repair", public_runs=False))
    assert closed.post(path).status_code == 401
    manager.store.execute("UPDATE packs SET visibility=? WHERE pack_id=?", ("holdout", run["pack_id"]))
    assert client.post(path).status_code == 404
    assert closed.post(path, headers={"Authorization": "Bearer adm_repair"}).status_code == 200


def test_metadata_omits_stale_cached_live_state(failed_run):
    manager, client, run = failed_run
    ctx = manager._ctx(run["run_id"])
    ctx.session.sim.now_ms -= 1  # Another instance persisted a later clock.
    assert "live" not in client.get(f"/api/v1/runs/{run['run_id']}").json()
