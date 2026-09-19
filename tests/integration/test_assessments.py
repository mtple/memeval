"""Frozen assignments survive recovery; private episodes never use public result surfaces."""

import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from market_replay.datasets.generator import dev_short_config, generate_pack
from market_replay.service import assessments
from market_replay.service.app import create_app
from market_replay.service.runs import ApiError, RunManager


@pytest.fixture
def assessment_server(tmp_path):
    manager = RunManager(data_dir=tmp_path / "data", hosted=True)
    packs = []
    for index in range(2):
        path = tmp_path / f"private_{index}"
        generate_pack(
            replace(
                dev_short_config(),
                name=f"private_{index}",
                seed=f"private-test-{index}",
                duration_ms=60_000,
                prehistory_ms=20_000,
                n_pools=2,
            ),
            path,
        )
        packs.append(manager.import_pack(path, f"private_{index}", visibility="holdout")["pack_id"])
    bundle = assessments.create_bundle(manager, label="Private protocol fixture", pack_ids=packs)
    identity = manager.enroll(agent={"name": "Candidate", "version": "1", "runtime": "external"})
    with TestClient(create_app(manager, admin_token="adm_assess", public_runs=True)) as client:
        yield manager, client, packs, bundle, identity
    manager.close()


def entry(client, bundle, identity):
    response = client.post(
        "/api/v1/assessments",
        json={
            "agent_token": identity["agent_token"],
            "bundle_id": bundle["bundle_id"],
            "code_sha256": "a" * 64,
            "config": {"cadence_ms": 10_000},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def finish(manager, run):
    result = manager.handle_command(run["session_credential"]["token"], "finish", "session.finish", {})
    assert result.status == "ok", result


def test_private_routes_and_all_attempts(assessment_server):
    manager, client, packs, bundle, identity = assessment_server
    enrolled = entry(client, bundle, identity)
    aid = enrolled["assessment_id"]
    assert len(enrolled["runs"]) == 2
    assert len(manager.store.query("SELECT * FROM assessment_episodes")) == 2
    assert (
        client.post(
            "/api/v1/assessments",
            json={
                "agent_token": identity["agent_token"],
                "bundle_id": bundle["bundle_id"],
                "code_sha256": "b" * 64,
                "config": {},
            },
        ).status_code
        == 409
    )
    for pack in packs:
        for suffix in ("", "/descriptor", "/validation", "/health", "/markets"):
            assert client.get(f"/api/v1/packs/{pack}{suffix}").status_code == 404
        assert client.get(f"/api/v1/leaderboard?pack_id={pack}").status_code == 404
        assert (
            client.post("/api/v1/runs", json={"agent_id": identity["agent_id"], "pack_id": pack}).status_code
            == 404
        )
    for run in enrolled["runs"]:
        rid = run["run_id"]
        for suffix in ("", "/report", "/export", "/timeline", "/observed", "/trade-review"):
            assert client.get(f"/api/v1/runs/{rid}{suffix}").status_code == 404, suffix
        assert (
            client.get(f"/api/v1/runs/{rid}", headers={"Authorization": "Bearer adm_assess"}).status_code
            == 200
        )
        assert (
            client.get(
                "/api/v1/assessment-bundles",
                headers={"Authorization": f"Bearer {run['session_credential']['token']}"},
            ).status_code
            == 403
        )
    finish(manager, enrolled["runs"][0])
    partial = client.get(f"/api/v1/assessments/{aid}").json()
    assert partial["coverage"] == {"assigned": 2, "finished": 1}
    assert partial["median_return"] is None
    assert all(r["headline_return"] is None for r in partial["episodes"])
    for path in (
        "/packs",
        "/runs",
        "/meta",
        "/leaderboard?all=1",
        f"/agents/{identity['agent_id']}/history",
        "/assessment-bundles",
        f"/assessments/{aid}",
    ):
        body = client.get("/api/v1" + path).text
        assert all(p not in body for p in packs), path
        assert "cadence_ms" not in body and "mask_seed" not in body, path
    finish(manager, enrolled["runs"][1])
    complete = client.get(f"/api/v1/assessments/{aid}").json()
    assert complete["complete"] and complete["eligible"], complete
    assert complete["median_return"] == "0E-8"
    board = client.get(f"/api/v1/assessment-bundles/{bundle['bundle_id']}/leaderboard").json()
    assert len(board["rows"]) == len(board["attempts"]) == 1
    assert client.get("/api/v1/leaderboard?all=1").json()["rows"] == []
    assert client.get("/api/v1/runs").json()["items"] == []


def test_recover_across_instance_preserves_assignment_trace_and_revokes_old_tokens(assessment_server):
    manager, client, packs, bundle, identity = assessment_server
    enrolled = entry(client, bundle, identity)
    first = enrolled["runs"][0]
    old_token = first["session_credential"]["token"]
    before = manager.handle_command(old_token, "watch", "clock.wait", {"until_ms": 10_000})
    manager2 = RunManager(data_dir=manager.data_dir, hosted=True)
    try:
        recovered = assessments.recover(manager2, enrolled["assessment_id"], identity["agent_token"])
        assert [r["run_id"] for r in recovered["runs"]] == [r["run_id"] for r in enrolled["runs"]]
        with pytest.raises(ApiError, match="credential"):
            manager.handle_command(old_token, "old", "session.describe", {})
        token = recovered["runs"][0]["session_credential"]["token"]
        after = manager2.handle_command(token, "new", "session.describe", {})
        assert after.clock_ms == before.clock_ms
        assert after.data["resource_profile"]["profile_id"] == "controlled_v1"
        assert manager.store.trace_len(first["run_id"]) == 2
        assert all(r["count"] == 1 for r in manager.store.query("SELECT * FROM attempts"))
        stopped = assessments.abort(manager2, enrolled["assessment_id"], identity["agent_token"])
        assert stopped["complete"] and not stopped["eligible"]
        assert all("run_completed" in r["failed_gates"] for r in stopped["episodes"])
        assert len(assessments.leaderboard(manager, bundle["bundle_id"])["attempts"]) == 1
    finally:
        manager2.close()


def test_initialization_failure_remains_in_assignment(assessment_server, monkeypatch):
    manager, client, packs, bundle, identity = assessment_server
    original = manager.create_run
    calls = 0

    def fail_second(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("lost worker")
        return original(**kwargs)

    monkeypatch.setattr(manager, "create_run", fail_second)
    response = client.post(
        "/api/v1/assessments",
        json={
            "agent_token": identity["agent_token"],
            "bundle_id": bundle["bundle_id"],
            "code_sha256": "c" * 64,
        },
    )
    assert response.status_code == 409
    board = assessments.leaderboard(manager, bundle["bundle_id"])
    assert not board["rows"]
    assert board["attempts"][0]["coverage"]["assigned"] == 2
    assert not board["attempts"][0]["eligible"]


def test_profile_gate_prior_exposure_and_no_relabeling(assessment_server):
    manager, client, packs, bundle, identity = assessment_server
    enrolled = entry(client, bundle, identity)
    for run in enrolled["runs"]:
        finish(manager, run)
    rid = enrolled["runs"][0]["run_id"]
    report = json.loads(manager.store.run(rid)["report_json"])
    report["unresolved"]["environment_fidelity_flags"] = [{"code": "material_failure"}]
    manager.store.update_run(rid, report_json=json.dumps(report))
    result = assessments.result(manager, enrolled["assessment_id"])
    assert result["complete"] and not result["eligible"]
    assert "no_fidelity_failures" in result["episodes"][0]["failed_gates"]
    with pytest.raises(ApiError):
        manager.import_pack(manager.load_pack(packs[0])[1].path, "published", visibility="public")
    v2 = manager.enroll(agent={"name": "Candidate", "version": "2", "runtime": "external"})
    second = entry(client, bundle, v2)
    for run in second["runs"]:
        finish(manager, run)
    res = assessments.result(manager, second["assessment_id"])
    assert not res["eligible"]
    assert all("prior_service_exposure" in e["failed_gates"] for e in res["episodes"])


def test_practice_profiles_are_separate_and_measured_lane_requires_runner(tmp_path, dev_pack_dir):
    manager = RunManager(data_dir=tmp_path / "public", hosted=True)
    try:
        manager.import_pack(dev_pack_dir, "practice")
        agent = manager.register_agent(
            name="profiles", version="1", runtime="python", capabilities=[], config={}
        )
        for profile in ("pack_defaults_v1", "controlled_v1", "adverse_execution_v1"):
            run = manager.create_run(
                agent_id=agent["agent_id"], pack_ref="practice", resource_profile_id=profile
            )
            finish(manager, run)
        rows = manager.leaderboard(pack_id="practice")["rows"]
        assert len(rows) == 3 and all(r["rank"] == 1 for r in rows)
        assert len({r["comparison_group"] for r in rows}) == 3
        with pytest.raises(ApiError, match="measured"):
            manager.create_run(
                agent_id=agent["agent_id"], pack_ref="practice", resource_profile_id="deployment_v1"
            )
        run = manager.create_run(
            agent_id=agent["agent_id"],
            pack_ref="practice",
            resource_profile_id="deployment_v1",
            execute="inprocess",
            launch_spec={"runtime": "python", "name": "cash_only"},
        )
        assert run["state"] == "completed", run
        assert manager.replay(run["run_id"])["state_matches"]
        assert sum(t["decision_elapsed_ms"] for t in manager.store.trace(run["run_id"])) > 0
        with pytest.raises(ApiError, match="restricted"):
            manager.create_run(
                agent_id=agent["agent_id"], pack_ref="practice", isolation="restricted_local_runner"
            )
        with pytest.raises(ApiError, match="holdout"):
            manager.import_pack(dev_pack_dir, "secret-renamed", visibility="holdout")
    finally:
        manager.close()
    fresh = RunManager(data_dir=tmp_path / "unexposed", hosted=True)
    try:
        with pytest.raises(ApiError, match="shipped"):
            fresh.import_pack(dev_pack_dir, "unrecognizable-alias", visibility="holdout")
    finally:
        fresh.close()


def test_remote_mcp_assessment_tools_keep_run_status_private(assessment_server):
    manager, client, packs, bundle, identity = assessment_server

    def call(name, arguments):
        response = client.post(
            "/agent/mcp",
            headers={"Accept": "application/json, text/event-stream"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        )
        assert response.status_code == 200, response.text
        result = response.json()["result"]["structuredContent"]
        return result.get("result", result)

    catalog = call("assessment_bundles", {})
    assert catalog["data"]["items"][0]["episode_count"] == 2
    enrolled = call(
        "assessment_enter",
        {
            "agent_token": identity["agent_token"],
            "bundle_id": bundle["bundle_id"],
            "code_sha256": "d" * 64,
            "config": {},
        },
    )
    assert enrolled["status"] == "ok", enrolled
    data = enrolled["data"]
    for run in data["runs"]:
        hidden = call("run_status", {"run_id": run["run_id"]})
        assert hidden["error"]["code"] == "NOT_FOUND"
        snap = call(
            "session_snapshot", {"token": run["session_credential"]["token"], "arguments": {"limit": 1}}
        )
        assert snap["status"] == "ok", snap
    result = call("assessment_result", {"assessment_id": data["assessment_id"]})
    assert result["data"]["coverage"] == {"assigned": 2, "finished": 0}
    assert not any(pack in json.dumps(result) for pack in packs)
    assert call(
        "assessment_abort", {"assessment_id": data["assessment_id"], "agent_token": identity["agent_token"]}
    )["data"]["complete"]


def test_api_misses_and_ui_traversal_do_not_serve_files(assessment_server):
    _, client, _, _, _ = assessment_server
    assert client.get("/api/v1/does-not-exist").status_code == 404
    assert client.get("/%2e%2e%2fpackage.json").status_code == 404


def test_assessment_identity_cannot_be_claimed_by_name_or_renamed(assessment_server):
    manager, client, _, bundle, identity = assessment_server
    enrolled = entry(client, bundle, identity)
    attack = client.post("/api/v1/enroll", json={"agent": {"name": "Candidate", "version": "1"}})
    assert attack.status_code == 403
    assert attack.json()["code"] == "IDENTITY_LOCKED"
    with pytest.raises(ApiError, match="renamed"):
        manager.rename_agent(agent_token=identity["agent_token"], name="Fresh")
    rotated = client.post(
        "/api/v1/enroll",
        headers={"Authorization": f"Bearer {identity['agent_token']}"},
        json={"agent": {"name": "Candidate", "version": "1"}},
    )
    assert rotated.status_code == 201, rotated.text
    recovered = client.post(
        f"/api/v1/assessments/{enrolled['assessment_id']}/credentials",
        json={"agent_token": rotated.json()["agent_token"]},
    )
    assert recovered.status_code == 200
    assert len(recovered.json()["runs"]) == 2
