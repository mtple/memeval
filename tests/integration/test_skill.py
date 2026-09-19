"""The agent skill is the onboarding: an agent given SKILL.md (Bankr, OpenClaw, Hermes, a script) enrolls
itself and plays the episodes with no human step. These tests execute the skill's own artifacts."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from market_replay.service.app import SKILL_DIR
from market_replay.service.embedded import EmbeddedServer
from market_replay.service.runs import RunManager

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def server(tmp_path_factory, dev_pack_dir):
    mgr = RunManager(data_dir=tmp_path_factory.mktemp("skilldata"))
    mgr.ONE_NAME_WINDOW_S = 0.0  # several agents share this test client's address; the one-name rule has its own test
    srv = EmbeddedServer(mgr, "adm_skill").start()
    srv.admin().post("/api/v1/packs/import", json={"path": str(dev_pack_dir), "name": "gen_dev_short"}).raise_for_status()
    yield srv, mgr
    srv.stop()


def test_skill_files_follow_the_bankr_layout():
    text = (SKILL_DIR / "SKILL.md").read_text()
    assert text.startswith("---\nname: market-replay\ndescription: ")
    import json

    cat = json.loads((SKILL_DIR / "catalog.json").read_text())
    assert cat["schemaVersion"] == 1 and cat["slug"] == "market-replay" == SKILL_DIR.name
    assert cat["install"]["type"] == "bankr" and cat["install"]["repoPath"] == "market-replay"
    assert (SKILL_DIR / "logo.svg").exists() and (SKILL_DIR / "scripts" / "market_replay_agent.py").exists()


def test_server_serves_the_skill_with_its_own_url(server):
    srv, _ = server
    r = httpx.get(srv.url + "/skill.md")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
    assert srv.url + "/api/v1/enroll" in r.text and "memeval-web.vercel.app" not in r.text
    script = httpx.get(srv.url + "/skill/market_replay_agent.py")
    assert script.status_code == 200 and f'DEFAULT_SERVER = "{srv.url}"' in script.text
    assert httpx.get(srv.url + "/skill/other.py").status_code == 404


def test_join_then_play_returns_one_token_per_episode_without_any_credential(server):
    srv, _ = server
    j = httpx.post(srv.url + "/api/v1/enroll", json={"agent": {"name": "bankr-skill-bot"}})
    assert j.status_code == 201, j.text
    joined = j.json()
    assert joined["agent_name"] == "bankr-skill-bot" and joined["agent_token"].startswith("agn_") and "runs" not in joined
    assert joined["results_url"] == f"{srv.url}/?agent={joined['agent_id']}" and joined["skill_url"] == srv.url + "/skill.md" and joined["play_url"] == srv.url + "/api/v1/play"
    auth = {"Authorization": "Bearer " + joined["agent_token"]}
    r = httpx.post(srv.url + "/api/v1/play", headers=auth, json={"suite_id": "generated-dev-v1"})
    assert r.status_code == 201, r.text
    e = r.json()
    assert e["agent_id"] == joined["agent_id"] and e["suite_id"] == "generated-dev-v1" and len(e["runs"]) == 1
    run = e["runs"][0]
    assert run["pack_name"] == "gen_dev_short" and run["session_credential"]["token"].startswith("agt_")
    single = httpx.post(srv.url + "/api/v1/play", json={"agent_token": joined["agent_token"], "pack_id": "gen_dev_short"}).json()
    assert single["agent_id"] == e["agent_id"] and single["suite_id"] is None and len(single["runs"]) == 1
    assert httpx.post(srv.url + "/api/v1/play", json={"pack_id": "gen_dev_short"}).status_code == 401  # no identity, no runs
    assert httpx.post(srv.url + "/api/v1/play", headers={"Authorization": "Bearer " + run["session_credential"]["token"]}, json={}).status_code == 403  # a session token is not an identity
    x = httpx.post(srv.url + "/api/v1/enroll", json={"agent": {"name": "x"}}).json()
    r = httpx.post(srv.url + "/api/v1/play", headers={"Authorization": "Bearer " + x["agent_token"]}, json={}, timeout=300)  # default: every real episode; none here, so the practice suite (generated on first use)
    assert r.status_code == 201 and r.json()["suite_id"] == "generated-practice-v1" and len(r.json()["runs"]) == 4
    listing = httpx.get(srv.url + "/api/v1/play", headers={"Authorization": "Bearer " + x["agent_token"]}).json()
    assert listing["agent_id"] == x["agent_id"] and listing["episodes"] == []


def test_the_skills_script_plays_a_suite_end_to_end(server):
    """The exact file an agent downloads from /skill/market_replay_agent.py, run as it says."""
    srv, mgr = server
    script = SKILL_DIR / "scripts" / "market_replay_agent.py"
    proc = subprocess.run([sys.executable, str(script), "--server", srv.url, "--agent", "script-bot", "--suite", "generated-dev-v1"], capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "joined as script-bot; 1 episode(s) to play" in proc.stdout and "finished at" in proc.stdout and "done." in proc.stdout
    runs = [r for r in httpx.get(srv.url + "/api/v1/runs").json()["items"] if r["agent_name"] == "script-bot"]
    assert len(runs) == 1
    mgr.wait_for_run(runs[0]["run_id"], 60)
    view = httpx.get(srv.url + f"/api/v1/runs/{runs[0]['run_id']}").json()
    assert view["state"] == "completed" and view["result_summary"]["confirmed_fills"] == 0


def test_mcp_agent_enrolls_and_plays_without_headers(server):
    srv, _ = server
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    def payload(res):
        p = res.structuredContent or {}
        return p.get("result", p)

    async def go():
        async with streamablehttp_client(srv.url + "/agent/mcp") as (read, write, _):
            async with ClientSession(read, write) as s:
                await s.initialize()
                names = {t.name for t in (await s.list_tools()).tools}
                denied = payload(await s.call_tool("session_describe", {"arguments": {}}))
                joined = payload(await s.call_tool("enroll", {"agent_name": "mcp-skill-bot"}))
                enrolled = payload(await s.call_tool("play", {"agent_token": joined["data"]["agent_token"], "pack_id": "gen_dev_short"}))
                token = enrolled["data"]["runs"][0]["session_credential"]["token"]
                run_id = enrolled["data"]["runs"][0]["run_id"]
                desc = payload(await s.call_tool("session_describe", {"arguments": {}, "token": token}))
                adv = payload(await s.call_tool("clock_advance", {"arguments": {"to_ms": 120_000}, "token": token}))
                status = payload(await s.call_tool("run_status", {"run_id": run_id}))
                return names, denied, enrolled, desc, adv, status

    names, denied, enrolled, desc, adv, status = asyncio.run(go())
    assert {"enroll", "play", "run_status", "session_describe", "broker_submit", "session_finish"} <= names
    assert denied["status"] == "error" and denied["error"]["code"] == "UNAUTHORIZED"
    assert enrolled["status"] == "ok" and enrolled["data"]["agent_name"] == "mcp-skill-bot"
    assert desc["status"] == "ok" and desc["data"]["episode"]["duration_ms"] > 0
    assert adv["status"] == "ok" and adv["data"]["clock_ms"] == 120_000
    assert status["status"] == "ok" and status["data"]["clock_ms"] == 120_000 and "session_credential" not in status["data"]
