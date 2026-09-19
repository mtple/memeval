"""Resource profiles and privacy compatibility after retiring private bundles."""

import json

import pytest
from fastapi.testclient import TestClient

from market_replay.service.app import create_app
from market_replay.service.runs import ApiError, RunManager


def finish(manager, run):
    result = manager.handle_command(run["session_credential"]["token"], "finish", "session.finish", {})
    assert result.status == "ok", result


def test_run_profiles_are_separate_and_measured_lane_requires_runner(tmp_path, dev_pack_dir):
    manager = RunManager(data_dir=tmp_path / "public", hosted=True)
    try:
        manager.import_pack(dev_pack_dir, "episode")
        agent = manager.register_agent(
            name="profiles", version="1", runtime="python", capabilities=[], config={}
        )
        for profile in ("pack_defaults_v1", "controlled_v1", "adverse_execution_v1"):
            run = manager.create_run(
                agent_id=agent["agent_id"], pack_ref="episode", resource_profile_id=profile
            )
            finish(manager, run)
        rows = manager.leaderboard(pack_id="episode")["rows"]
        assert len(rows) == 3 and all(r["rank"] == 1 for r in rows)
        assert len({r["comparison_group"] for r in rows}) == 3
        with pytest.raises(ApiError, match="measured"):
            manager.create_run(
                agent_id=agent["agent_id"], pack_ref="episode", resource_profile_id="deployment_v1"
            )
        run = manager.create_run(
            agent_id=agent["agent_id"],
            pack_ref="episode",
            resource_profile_id="deployment_v1",
            execute="inprocess",
            launch_spec={"runtime": "python", "name": "cash_only"},
        )
        assert run["state"] == "completed", run
        assert manager.replay(run["run_id"])["state_matches"]
        assert sum(t["decision_elapsed_ms"] for t in manager.store.trace(run["run_id"])) > 0
        with pytest.raises(ApiError, match="restricted"):
            manager.create_run(
                agent_id=agent["agent_id"], pack_ref="episode", isolation="restricted_local_runner"
            )
    finally:
        manager.close()


@pytest.fixture
def run_server(tmp_path, dev_pack_dir):
    manager = RunManager(data_dir=tmp_path / "data", hosted=True)
    pack = manager.import_pack(dev_pack_dir, "episode")
    identity = manager.enroll(agent={"name": "Candidate", "version": "1"})
    run = manager.create_run(agent_id=identity["agent_id"], pack_ref=pack["pack_id"])
    finish(manager, run)
    try:
        with TestClient(create_app(manager, admin_token="adm_protocol")) as client:
            yield manager, client, pack, run, identity
    finally:
        manager.close()


def test_retired_workflow_is_unavailable(run_server, dev_pack_dir):
    manager, client, _, _, _ = run_server
    schema = client.get("/openapi.json").json()
    assert "assessment" not in json.dumps(schema).lower()
    for path in ("assessment-bundles", "assessments/old", "assessment-bundles/old/leaderboard"):
        assert client.get(f"/api/v1/{path}").status_code == 404
    response = client.post(
        "/agent/mcp",
        headers={"Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert response.status_code == 200, response.text
    names = {tool["name"] for tool in response.json()["result"]["tools"]}
    assert {"enroll", "play", "session_snapshot", "clock_wait"} <= names
    assert not any(name.startswith("assessment_") for name in names)
    response = client.post(
        "/api/v1/packs/import",
        headers={"Authorization": "Bearer adm_protocol"},
        json={"path": str(dev_pack_dir), "name": "secret", "visibility": "holdout"},
    )
    assert response.status_code == 422
    assert manager.store.pack("secret") is None
    assert client.get("/api/v1/resource-profiles").status_code == 200
    assert client.get("/api/v1/does-not-exist").status_code == 404
    assert client.get("/%2e%2e%2fpackage.json").status_code == 404


def test_archived_private_data_stays_private_after_reimport_and_restart(run_server, tmp_path, dev_pack_dir):
    manager, client, pack, run, identity = run_server
    pid, rid = pack["pack_id"], run["run_id"]
    # Simulate records from the retired workflow without relying on its deleted creation code.
    manager.store.execute("UPDATE packs SET visibility='holdout' WHERE pack_id=?", (pid,))
    manager.store.execute(
        "INSERT INTO assessment_episodes (assessment_id, slot, pack_id, run_id) VALUES (?,?,?,?)",
        ("archived", 0, pid, rid),
    )
    manager.import_pack(dev_pack_dir, "episode")
    assert manager.store.pack(pid)["visibility"] == "holdout"
    for suffix in ("", "/descriptor", "/validation", "/health", "/markets"):
        assert client.get(f"/api/v1/packs/{pid}{suffix}").status_code == 404
    for suffix in ("", "/report", "/export", "/timeline", "/observed", "/trade-review"):
        assert client.get(f"/api/v1/runs/{rid}{suffix}").status_code == 404
    for path in ("packs", "runs"):
        assert client.get(f"/api/v1/{path}").json()["items"] == []
    assert client.get(f"/api/v1/leaderboard?pack_id={pid}").status_code == 404
    assert client.get("/api/v1/leaderboard?all=1").json()["rows"] == []
    assert pid not in json.dumps(manager.agent_history(identity["agent_id"]))
    assert rid not in json.dumps(manager.agent_history(identity["agent_id"]))
    with pytest.raises(ApiError, match="unknown resource"):
        manager.create_run(agent_id=identity["agent_id"], pack_ref=pid)
    with pytest.raises(ApiError, match="unknown resource"):
        manager.compare(suite_id=None, agent_a=identity["agent_id"], agent_b=identity["agent_id"], run_ids_a=[rid])
    assert client.get(f"/api/v1/runs/{rid}", headers={"Authorization": "Bearer adm_protocol"}).status_code == 200
    restarted = RunManager(data_dir=tmp_path / "data", hosted=True)
    try:
        assert restarted.is_private_run(rid)
        # Membership preserves run privacy even if a later operator removes the source pack.
        restarted.store.execute("DELETE FROM packs WHERE pack_id=?", (pid,))
        assert restarted.is_private_run(rid)
        assert client.get(f"/api/v1/runs/{rid}").status_code == 404
    finally:
        restarted.close()
