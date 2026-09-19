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
from market_replay.service.runs import ApiError, RunManager

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
    assert lb["category"]["label"] == "Practice: short warm-up, 2 hours (artificial)"
    assert c.get("/api/v1/leaderboard?pack_id=gen_dev_short&include_artificial=0").json()["categories"] == []
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
    enrolled = c.post("/api/v1/enroll", json={"agent": {"name": "holder"}}).json()
    # the name is the agent, so this is the same "holder" that already ran above, not a new one
    before = len(c.get("/api/v1/runs", params={"agent_id": enrolled["agent_id"]}).json()["items"])
    assert enrolled["results_url"].endswith(f"/?agent={enrolled['agent_id']}") and enrolled["agent_token"].startswith("agn_")
    played = c.post("/api/v1/play", headers={"Authorization": "Bearer " + enrolled["agent_token"]}, json={"pack_id": "gen_dev_short"})
    assert played.status_code == 201 and len(played.json()["runs"]) == 1
    # one name per agent: a second name from the same address is refused
    second = c.post("/api/v1/enroll", json={"agent": {"name": "holder-momentum"}})
    assert second.status_code == 409 and second.json()["code"] == "ONE_NAME" and "holder" in second.json()["message"]
    # the name is the agent: joining again under it is the same agent, with a fresh token, whatever
    # the strategy did in between — and a legacy client still sending a version gets that same agent
    again = c.post("/api/v1/enroll", json={"agent": {"name": "holder", "version": "7"}})
    assert again.status_code == 201 and again.json()["agent_id"] == enrolled["agent_id"]
    assert again.json()["agent_token"] != enrolled["agent_token"]
    played_again = c.post("/api/v1/play", headers={"Authorization": "Bearer " + again.json()["agent_token"]}, json={"pack_id": "gen_dev_short"})
    assert played_again.status_code == 201
    mine = c.get("/api/v1/runs", params={"agent_id": enrolled["agent_id"]}).json()["items"]
    assert len(mine) == before + 2  # every run this name makes lists under it, across re-joins
    assert {r["agent_name"] for r in mine} == {"holder"}
    mgr.close()


def test_weeks_committed_to_the_repository_are_registered_on_start_and_served_from_disk(tmp_path: Path, dev_pack_dir: Path, monkeypatch):
    import shutil

    import market_replay.service.runs as runs_mod

    weeks = tmp_path / "weeks"
    shutil.copytree(dev_pack_dir, weeks / "base_week_2026-09-07")
    (weeks / "not_a_pack").mkdir()
    monkeypatch.setattr(runs_mod, "WEEKS_DIR", weeks)
    store = str(tmp_path / "store.sqlite")
    mgr = RunManager(data_dir=tmp_path / "a", store_url=store, hosted=True)
    views = mgr.register_shipped_weeks()
    assert [v["name"] for v in views] == ["base_week_2026-09-07"] and views[0]["runnable"]
    assert mgr.register_shipped_weeks()[0]["pack_id"] == views[0]["pack_id"]  # idempotent, no second validation
    c = TestClient(create_app(mgr, "adm_public_test"))
    assert c.post("/api/v1/packs/upload?name=x", content=b"gone").status_code in (404, 405)  # no upload path any more
    mgr.close()
    # another instance with an empty scratch dir: the week is read from the checkout, never downloaded
    fresh = RunManager(data_dir=tmp_path / "b", store_url=store, hosted=True)
    row, pack = fresh.load_pack("base_week_2026-09-07")
    assert pack.pack_id == views[0]["pack_id"] and Path(row["path"]) == (weeks / "base_week_2026-09-07").resolve()
    c2 = TestClient(create_app(fresh, "adm_public_test"))
    run = c2.post("/api/v1/runs", json={"agent": {"name": "u", "version": "1", "runtime": "python"}, "pack_id": "base_week_2026-09-07", "launch": {"name": "cash_only", "runtime": "python"}}).json()
    assert run["state"] == "completed"
    # removing the directory from the repository withdraws the week on the next start: off the
    # catalogue and the board, while the finished run keeps its report
    shutil.rmtree(weeks / "base_week_2026-09-07")
    assert fresh.register_shipped_weeks() == [] and fresh.real_weeks() == []
    assert fresh.store.pack(views[0]["pack_id"])["use_status"] == "withdrawn"
    assert not any(p["runnable"] for p in fresh.packs())
    assert c2.get(f"/api/v1/runs/{run['run_id']}").json()["state"] == "completed"
    fresh.close()


def test_category_labels_and_descriptions_for_real_and_artificial_weeks():
    from market_replay.service.runs import _episode_description, _episode_label

    fixture = {"name": "gen_week_trending", "is_full_week": 1, "duration_ms": 604_800_000, "origin": "generated_fixture", "chain": "generated", "summary_json": '{"scenario": "Sustained directional flow."}'}
    assert _episode_label(fixture) == "Practice week: trending market (artificial)"
    assert _episode_description(fixture).startswith("Practice material, not real data: an artificial market with known rules, made for testing agents. Sustained")
    real = {"name": "base_week_03", "is_full_week": 1, "duration_ms": 604_800_000, "origin": "historical_reconstruction", "chain": "base", "start_utc": "2026-09-08T00:00:00Z", "end_utc": "2026-09-15T00:00:00Z", "summary_json": '{"pools_executable": 16}'}
    assert _episode_label(real) == "Base week of 2026-09-08"
    day = {**real, "name": "base_day_2026-09-08", "is_full_week": 0, "duration_ms": 86_400_000, "end_utc": "2026-09-09T00:00:00Z"}
    assert _episode_label(day) == "Base day of 2026-09-08"
    assert "from 2026-09-08 to 2026-09-09 (1 day" in _episode_description(day)
    d = _episode_description(real)
    assert "Real swaps recorded on Base from 2026-09-08 to 2026-09-15 (7 days)" in d and "16 tradable pools" in d and "generic names" in d


def test_listing_runs_never_rebuilds_a_session(tmp_path: Path, dev_pack_dir: Path, monkeypatch):
    """A fresh instance answers a run list from the stored rows alone; replaying an unfinished run's
    trace is the agent's own command path's job, once per instance."""
    import market_replay.service.runs as runs_mod

    store = str(tmp_path / "store.sqlite")
    a = RunManager(data_dir=tmp_path / "a", store_url=store, hosted=True)
    a.import_pack(dev_pack_dir, "gen_dev_short")
    joined = a.enroll(agent={"name": "walker", "version": "1"})
    enrolled = a.play(agent_token=joined["agent_token"], pack_id="gen_dev_short")
    token = enrolled["runs"][0]["session_credential"]["token"]
    a.handle_command(token, "r1", "session.describe", {})
    a.handle_command(token, "r2", "clock.advance", {"to_ms": 600_000})
    assert "live" in a.run_view(enrolled["runs"][0]["run_id"])  # this instance holds the session
    a.close()
    b = RunManager(data_dir=tmp_path / "b", store_url=store, hosted=True)
    monkeypatch.setattr(runs_mod, "replay_trace", lambda *args, **kw: (_ for _ in ()).throw(AssertionError("a listing replayed a trace")))
    rows = b.runs()
    assert len(rows) == 1 and rows[0]["state"] == "running" and rows[0]["clock_ms"] == 600_000 and "live" not in rows[0]
    b.close()


def test_default_bankroll_is_one_whole_unit_of_the_cash_asset(tmp_path: Path, dev_pack_dir: Path):
    """A practice pack's CASH has 6 decimals, so the default is 1,000,000 raw; a real Base day whose cash
    is ETH gets 10^18. The old fixed 1,000,000 was a trillionth of an ETH."""
    mgr = RunManager(data_dir=tmp_path / "data")
    mgr.import_pack(dev_pack_dir, "gen_dev_short")
    c = TestClient(create_app(mgr, "adm_public_test"))
    r = c.post("/api/v1/runs", json={"agent": {"name": "b", "version": "1"}, "pack_id": "gen_dev_short"})
    assert r.status_code == 201, r.text
    assert r.json()["bankroll_raw"] == str(10**6)
    r2 = c.post("/api/v1/runs", json={"agent": {"name": "b", "version": "1"}, "pack_id": "gen_dev_short", "bankroll_raw": "5"})
    assert r2.status_code == 201 and r2.json()["bankroll_raw"] == "5"
    mgr.close()


def test_enrolling_again_adds_only_unfinished_episodes(tmp_path: Path, dev_pack_dir: Path, monkeypatch):
    """A returning agent plays the new day and not the episodes it already completed."""
    import market_replay.service.runs as runs_mod
    from tests.unit.test_engine_clmm import make_cl_pack

    weeks = tmp_path / "weeks"
    make_cl_pack(weeks / "base_day_2026-09-08")
    monkeypatch.setattr(runs_mod, "WEEKS_DIR", weeks)
    mgr = RunManager(data_dir=tmp_path / "a", store_url=str(tmp_path / "store.sqlite"), hosted=True)
    mgr.register_shipped_weeks()
    joined = mgr.enroll(agent={"name": "turtle"})
    assert joined["runs" if "runs" in joined else "episodes"] and joined["episodes"][0]["status"] == "new"
    first = mgr.play(agent_token=joined["agent_token"])
    assert [r["pack_name"] for r in first["runs"]] == ["base_day_2026-09-08"] and first["skipped"] == []
    assert mgr.episodes_for(joined["agent_id"])[0]["status"] == "running"
    token = first["runs"][0]["session_credential"]["token"]
    mgr.handle_command(token, "r1", "session.describe", {})
    duration = mgr.handle_command(token, "r2", "session.describe", {}).data["episode"]["duration_ms"]
    mgr.handle_command(token, "r3", "clock.advance", {"to_ms": duration})
    assert mgr.handle_command(token, "r4", "session.finish", {}).status == "ok"
    again = mgr.play(agent_token=joined["agent_token"])
    assert again["runs"] == [] and [s["pack_name"] for s in again["skipped"]] == ["base_day_2026-09-08"]
    assert "skipped" in again["note"] and mgr.episodes_for(joined["agent_id"])[0]["status"] == "finished"
    # joining again retires the old token and issues a new one
    rejoined = mgr.enroll(agent={"name": "turtle"})
    assert rejoined["agent_id"] == joined["agent_id"] and rejoined["agent_token"] != joined["agent_token"]
    with pytest.raises(ApiError):
        mgr.play(agent_token=joined["agent_token"])
    # the name is the agent: a legacy client sending a different version is still that same agent,
    # with the same finished episodes, and re-trading a finished day is an explicit pack_id
    legacy = mgr.enroll(agent={"name": "turtle", "version": "2"})
    assert legacy["agent_id"] == joined["agent_id"]
    assert mgr.play(agent_token=legacy["agent_token"])["runs"] == []
    pack_id = mgr.episodes_for(joined["agent_id"])[0]["pack_id"]
    replayed = mgr.play(agent_token=legacy["agent_token"], pack_id=pack_id)
    assert [r["pack_name"] for r in replayed["runs"]] == ["base_day_2026-09-08"]
    mgr.close()
