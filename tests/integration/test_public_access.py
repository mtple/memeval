"""Public self-serve access model.

Anyone can read results, register an agent and start a run (getting a one-time session token for
their own agent). Operator actions stay behind the admin token. Agent tokens still never reach
the control plane. Per-IP rate limits and the global caps bound the cost of a public form.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from market_replay.service.app import create_app
from market_replay.service.runs import RunManager

ADMIN = {"Authorization": "Bearer adm_public_test"}


@pytest.fixture()
def app(tmp_path: Path, dev_pack_dir: Path):
    mgr = RunManager(data_dir=tmp_path / "data", max_runs_per_hour_per_ip=3)
    mgr.import_pack(dev_pack_dir, "gen_dev_short")
    application = create_app(mgr, "adm_public_test")
    yield TestClient(application), mgr
    mgr.close()


def test_anonymous_can_read_everything_public(app):
    c, _ = app
    for path in ("/api/v1/meta", "/api/v1/packs", "/api/v1/agents", "/api/v1/runs", "/api/v1/suites", "/api/v1/comparisons", "/api/v1/usage"):
        assert c.get(path).status_code == 200, path
    meta = c.get("/api/v1/meta").json()
    assert meta["role"] == "public" and meta["public_runs"] is True
    assert c.get("/api/v1/meta", headers=ADMIN).json()["role"] == "admin"


def test_anonymous_self_serve_run_returns_a_working_session_token(app):
    c, mgr = app
    r = c.post("/api/v1/runs", json={"agent": {"name": "bankr-bot", "version": "3"}, "pack_id": "gen_dev_short"})
    assert r.status_code == 201, r.text
    run = r.json()
    cred = run["session_credential"]
    assert cred["token"].startswith("agt_") and cred["commands_url"].endswith("/agent/v1/commands") and cred["mcp_url"].endswith("/agent/mcp")
    assert run["agent_name"] == "bankr-bot"
    env = c.post("/agent/v1/commands", headers={"Authorization": f"Bearer {cred['token']}"}, json={"request_id": "r1", "tool": "session.describe", "arguments": {}}).json()
    assert env["status"] == "ok"
    # the same agent name/version is reused, not duplicated, on the next run
    r2 = c.post("/api/v1/runs", json={"agent": {"name": "bankr-bot", "version": "3"}, "pack_id": "gen_dev_short"})
    assert r2.status_code == 201 and r2.json()["agent_id"] == run["agent_id"]
    assert len([a for a in c.get("/api/v1/agents").json()["items"] if a["name"] == "bankr-bot"]) == 1
    # public run list shows it without any credential
    assert any(x["run_id"] == run["run_id"] for x in c.get("/api/v1/runs").json()["items"])
    # the run view never leaks the token again
    assert "session_credential" not in c.get(f"/api/v1/runs/{run['run_id']}").json()


def test_operator_actions_need_the_admin_token(app):
    c, _ = app
    run = c.post("/api/v1/runs", json={"agent": {"name": "x", "version": "1"}, "pack_id": "gen_dev_short"}).json()
    rid = run["run_id"]
    assert c.post("/api/v1/packs/import", json={"path": "/nowhere"}).status_code == 401
    for verb in ("pause", "resume", "abort", "replay"):
        assert c.post(f"/api/v1/runs/{rid}/{verb}").status_code == 401, verb
    assert c.get(f"/api/v1/runs/{rid}/export?role=admin").status_code == 401
    assert c.get(f"/api/v1/runs/{rid}/export?role=participant").status_code == 200
    assert c.post("/api/v1/studies", json={"candidates": [], "pack_ids": [], "intended_outcome": "", "prediction": ""}).status_code == 401
    assert c.post(f"/api/v1/runs/{rid}/pause", headers=ADMIN).status_code == 200
    assert c.post(f"/api/v1/runs/{rid}/resume", headers=ADMIN).status_code == 200


def test_agent_tokens_and_wrong_tokens_never_reach_the_control_plane(app):
    c, _ = app
    run = c.post("/api/v1/runs", json={"agent": {"name": "x", "version": "1"}, "pack_id": "gen_dev_short"}).json()
    agt = {"Authorization": f"Bearer {run['session_credential']['token']}"}
    for path in ("/api/v1/packs", "/api/v1/runs", "/api/v1/meta"):
        assert c.get(path, headers=agt).status_code == 403, path
    assert c.post("/api/v1/runs", headers=agt, json={"agent": {"name": "y", "version": "1"}, "pack_id": "gen_dev_short"}).status_code == 403
    assert c.get("/api/v1/packs", headers={"Authorization": "Bearer adm_wrong"}).status_code == 403


def test_public_run_creation_is_rate_limited_per_ip(app):
    c, _ = app
    body = {"agent": {"name": "spam", "version": "1"}, "pack_id": "gen_dev_short"}
    codes = [c.post("/api/v1/runs", json=body, headers={"X-Forwarded-For": "203.0.113.9"}).status_code for _ in range(4)]
    assert codes == [201, 201, 201, 429]
    limited = c.post("/api/v1/runs", json=body, headers={"X-Forwarded-For": "203.0.113.9"})
    assert limited.json()["code"] == "RATE_LIMITED"
    # another address is unaffected; the operator is never limited
    assert c.post("/api/v1/runs", json=body, headers={"X-Forwarded-For": "198.51.100.1"}).status_code == 201
    assert c.post("/api/v1/runs", json=body, headers={**ADMIN, "X-Forwarded-For": "203.0.113.9"}).status_code == 201


def test_public_runs_can_be_switched_off(tmp_path: Path, dev_pack_dir: Path):
    mgr = RunManager(data_dir=tmp_path / "data")
    mgr.import_pack(dev_pack_dir, "gen_dev_short")
    c = TestClient(create_app(mgr, "adm_public_test", public_runs=False))
    assert c.get("/api/v1/runs").status_code == 200  # reads stay public
    assert c.post("/api/v1/runs", json={"agent": {"name": "x", "version": "1"}, "pack_id": "gen_dev_short"}).status_code == 401
    assert c.post("/api/v1/agents", json={"name": "x", "version": "1"}).status_code == 401
    assert c.get("/api/v1/meta").json()["public_runs"] is False
    assert c.post("/api/v1/runs", headers=ADMIN, json={"agent": {"name": "x", "version": "1"}, "pack_id": "gen_dev_short"}).status_code == 201
    mgr.close()


def test_run_list_carries_a_result_summary_once_a_report_exists(tmp_path: Path, dev_pack_dir: Path):
    mgr = RunManager(data_dir=tmp_path / "data", hosted=True)  # hosted: the reference participant runs inside the request
    mgr.import_pack(dev_pack_dir, "gen_dev_short")
    c = TestClient(create_app(mgr, "adm_public_test"))
    run = c.post("/api/v1/runs", json={"agent": {"name": "ref", "version": "1", "runtime": "python"}, "pack_id": "gen_dev_short", "launch": {"name": "cash_only", "runtime": "python"}}).json()
    mgr.wait_for_run(run["run_id"], 120)
    view = c.get(f"/api/v1/runs/{run['run_id']}").json()
    assert view["state"] == "completed" and view["has_report"]
    s = view["result_summary"]
    assert set(s) >= {"headline_return", "valuation_complete", "max_drawdown", "confirmed_fills", "gas_total_raw"}
    assert s["confirmed_fills"] == 0
    listed = next(x for x in c.get("/api/v1/runs").json()["items"] if x["run_id"] == run["run_id"])
    assert listed["result_summary"]["headline_return"] == s["headline_return"] and listed["agent_name"] == "ref"
    mgr.close()


def test_leaderboard_ranks_agents_per_category_and_join_serves_the_skill(tmp_path: Path, dev_pack_dir: Path):
    mgr = RunManager(data_dir=tmp_path / "data", hosted=True)
    mgr.import_pack(dev_pack_dir, "gen_dev_short")
    c = TestClient(create_app(mgr, "adm_public_test"))
    for name, example in (("holder", "cash_only"), ("basket", "scheduled_basket")):
        r = c.post("/api/v1/runs", json={"agent": {"name": name, "version": "1", "runtime": "python"}, "pack_id": "gen_dev_short", "launch": {"name": example, "runtime": "python"}})
        assert r.status_code == 201 and r.json()["state"] == "completed", r.text
    lb = c.get("/api/v1/leaderboard?pack_id=gen_dev_short").json()
    assert lb["category"]["kind"] == "pack" and [x["kind"] for x in lb["categories"]][:1] == ["suite"]
    names = [r["agent_name"] for r in lb["rows"]]
    assert set(names) == {"holder", "basket"} and [r["rank"] for r in lb["rows"]] == [1, 2]
    assert all(r["episodes_valued"] == 1 and r["covers_all"] for r in lb["rows"])
    assert "not an edge" in lb["note"]
    suite = c.get("/api/v1/leaderboard?suite_id=generated-dev-v1").json()
    assert suite["category"]["episodes"] == ["gen_dev_short"] and len(suite["rows"]) == 2
    assert c.get("/api/v1/leaderboard?suite_id=nope").status_code == 404
    default = c.get("/api/v1/leaderboard").json()  # practice suite has no rows here: falls back to a category that does
    assert default["rows"] and default["category"]["id"] != "generated-practice-v1"
    assert c.get("/api/v1/leaderboard?all=1").json()["category"]["kind"] == "all"
    join = c.get("/join")
    assert join.status_code == 200 and join.text.startswith("---\nname: market-replay")
    enrolled = c.post("/api/v1/enroll", json={"agent": {"name": "holder", "version": "1"}, "pack_id": "gen_dev_short"}).json()
    assert enrolled["results_url"].endswith(f"/?agent={enrolled['agent_id']}")
    mgr.close()


def _targz(directory: Path) -> bytes:
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        tf.add(directory, arcname=directory.name)
    return buf.getvalue()


def test_uploaded_pack_is_kept_in_the_store_and_materialized_on_a_fresh_instance(tmp_path: Path, dev_pack_dir: Path):
    import shutil

    store = str(tmp_path / "store.sqlite")
    mgr = RunManager(data_dir=tmp_path / "a", store_url=store, hosted=True)
    c = TestClient(create_app(mgr, "adm_public_test"))
    archive = _targz(dev_pack_dir)
    assert c.post("/api/v1/packs/upload?name=uploaded_week", content=archive).status_code == 401  # operator only
    r = c.post("/api/v1/packs/upload?name=uploaded_week", content=archive, headers={**ADMIN, "content-type": "application/gzip"})
    assert r.status_code == 201, r.text
    view = r.json()
    assert view["name"] == "uploaded_week" and view["runnable"]
    bad = c.post("/api/v1/packs/upload", content=b"not a tar", headers=ADMIN)
    assert bad.status_code == 400
    mgr.close()
    # a second instance with an empty filesystem: the pack comes back from the archive and runs
    shutil.rmtree(tmp_path / "a" / "packs")
    fresh = RunManager(data_dir=tmp_path / "a", store_url=store, hosted=True)
    row, pack = fresh.load_pack("uploaded_week")
    assert pack.pack_id == view["pack_id"]
    c2 = TestClient(create_app(fresh, "adm_public_test"))
    run = c2.post("/api/v1/runs", json={"agent": {"name": "u", "version": "1", "runtime": "python"}, "pack_id": "uploaded_week", "launch": {"name": "cash_only", "runtime": "python"}}).json()
    assert run["state"] == "completed"
    fresh.close()


def test_category_labels_and_descriptions_for_real_and_artificial_weeks():
    from market_replay.service.runs import _episode_description, _episode_label

    fixture = {"name": "gen_week_trending", "is_full_week": 1, "duration_ms": 604_800_000, "origin": "generated_fixture", "chain": "generated", "summary_json": '{"scenario": "Sustained directional flow."}'}
    assert _episode_label(fixture) == "Week: trending"
    assert _episode_description(fixture).startswith("Artificial market with known rules. Sustained")
    real = {"name": "base_week_03", "is_full_week": 1, "duration_ms": 604_800_000, "origin": "historical_reconstruction", "chain": "base", "start_utc": "2026-09-08T00:00:00Z", "end_utc": "2026-09-15T00:00:00Z", "summary_json": '{"pools_executable": 16}'}
    assert _episode_label(real) == "Base week 3"
    d = _episode_description(real)
    assert "Real swaps recorded on Base over 7 days" in d and "16 tradable pools" in d and "sealed" in d and "2026" not in d
