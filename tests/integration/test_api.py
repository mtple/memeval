"""Acceptance 30, 33, 35, 36, 37, 38 through the real HTTP service, SDKs and MCP."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from market_replay.service.embedded import EmbeddedServer
from market_replay.service.runs import RunManager

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def server(tmp_path_factory, dev_pack_dir):
    data = tmp_path_factory.mktemp("data")
    mgr = RunManager(data_dir=data)
    srv = EmbeddedServer(mgr, "adm_test_token")
    srv.start()
    admin = srv.admin()
    admin.post("/api/v1/packs/import", json={"path": str(dev_pack_dir), "name": "gen_dev_short"}).raise_for_status()
    yield srv, admin, mgr
    srv.stop()


def register(admin: httpx.Client, name: str, runtime: str = "python") -> str:
    r = admin.post("/api/v1/agents", json={"name": name, "version": "1", "runtime": runtime})
    if r.status_code == 409:
        return next(a["agent_id"] for a in admin.get("/api/v1/agents").json()["items"] if a["name"] == name)
    return r.json()["agent_id"]


def test_agent_credentials_cannot_reach_control_plane(server):
    srv, admin, mgr = server
    aid = register(admin, "ext")
    run = admin.post("/api/v1/runs", json={"agent_id": aid, "pack_id": "gen_dev_short"}).json()
    tok = run["session_credential"]["token"]
    h = {"Authorization": f"Bearer {tok}"}
    for path in ("/api/v1/packs", "/api/v1/runs", f"/api/v1/runs/{run['run_id']}/report", f"/api/v1/runs/{run['run_id']}/export", "/api/v1/agents", "/api/v1/meta"):
        r = httpx.get(srv.url + path, headers=h)
        assert r.status_code == 403, path
    assert httpx.get(srv.url + "/api/v1/packs").status_code == 200  # reads are public; agent tokens still are not
    # another run's session id with this credential is refused
    other = admin.post("/api/v1/runs", json={"agent_id": aid, "pack_id": "gen_dev_short"}).json()
    r = httpx.post(srv.url + "/agent/v1/commands", headers=h, json={"request_id": "x", "tool": "session.describe", "arguments": {}, "session_id": "ses_" + other["run_id"][4:]})
    assert r.status_code == 403
    # admin token cannot be used on the agent plane
    r = httpx.post(srv.url + "/agent/v1/commands", headers={"Authorization": "Bearer adm_test_token"}, json={"request_id": "x", "tool": "session.describe", "arguments": {}})
    assert r.status_code == 403
    # invalid credential
    r = httpx.post(srv.url + "/agent/v1/commands", headers={"Authorization": "Bearer agt_nope"}, json={"request_id": "x", "tool": "session.describe", "arguments": {}})
    assert r.status_code == 401


def test_python_and_typescript_clients_equivalent_semantics(server):
    srv, admin, mgr = server
    aid = register(admin, "equiv")
    script = [
        ("session.describe", {}),
        ("markets.list", {"limit": 3}),
        ("clock.advance", {"to_ms": 600_000}),
        ("session.snapshot", {"since_ms": 0, "limit": 2}),
        ("clock.wait", {"until_ms": 900_000, "conditions": [{"kind": "new_pool"}]}),
        ("markets.get", {"pool_id": "pool_nonexistent"}),
        ("wallet.history", {}),
        ("portfolio.get", {}),
    ]
    run_py = admin.post("/api/v1/runs", json={"agent_id": aid, "pack_id": "gen_dev_short", "mask_seed": "same", "engine_seed": "same"}).json()
    run_ts = admin.post("/api/v1/runs", json={"agent_id": aid, "pack_id": "gen_dev_short", "mask_seed": "same", "engine_seed": "same"}).json()
    sys.path.insert(0, str(REPO / "sdk" / "python"))
    from market_replay_client import MarketReplayClient

    py = MarketReplayClient(srv.url, run_py["session_credential"]["token"])
    py_out = [py.call(t, a, request_id=f"r{i}") for i, (t, a) in enumerate(script)]
    ts_script = f"""
    import {{ MarketReplayClient }} from "{(REPO / 'sdk' / 'typescript' / 'src' / 'index.ts').as_posix()}";
    const c = new MarketReplayClient("{srv.url}", "{run_ts['session_credential']['token']}");
    const script = {json.dumps(script)};
    const out = [];
    for (let i = 0; i < script.length; i++) out.push(await c.call(script[i][0], script[i][1], "r" + i));
    console.log(JSON.stringify(out));
    """
    ts_file = Path(mgr.data_dir) / "equiv.mts"
    ts_file.write_text(ts_script)
    r = subprocess.run(["node", "--experimental-strip-types", "--no-warnings", str(ts_file)], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    ts_out = json.loads(r.stdout)

    def strip(env):
        e = dict(env)
        e.pop("session_id")
        if isinstance(e.get("data"), dict):
            e["data"] = {k: v for k, v in e["data"].items() if k != "session_id"}
        return e

    assert [strip(e) for e in py_out] == [strip(e) for e in ts_out]
    by_tool = {tool: result for (tool, _), result in zip(script, py_out, strict=True)}
    assert by_tool["markets.get"]["error"]["code"] == "NOT_YET_DISCOVERED"
    assert by_tool["wallet.history"]["error"]["code"] == "UNSUPPORTED_CAPABILITY"


def test_mcp_reaches_same_handler(server):
    srv, admin, mgr = server
    from market_replay.service.mcp_server import Forwarder, build_mcp

    aid = register(admin, "mcp")
    run = admin.post("/api/v1/runs", json={"agent_id": aid, "pack_id": "gen_dev_short"}).json()
    fwd = Forwarder(srv.url, run["session_credential"]["token"])
    mcp = build_mcp(fwd)
    import asyncio

    async def go():
        tools = await mcp.list_tools()
        names = {t.name for t in tools}
        assert {"session_describe", "broker_submit", "clock_advance", "session_finish"} <= names
        res = await mcp.call_tool("session_describe", {"arguments": {}})
        return res

    res = asyncio.run(go())
    content, structured = res if isinstance(res, tuple) else (res, None)
    payload = structured if structured else json.loads(content[0].text)
    payload = payload.get("result", payload)
    assert payload["status"] == "ok" and payload["data"]["tools"]
    # the same session clock is shared with a direct HTTP call: MCP hit the same handler
    direct = httpx.post(srv.url + "/agent/v1/commands", headers={"Authorization": f"Bearer {run['session_credential']['token']}"}, json={"request_id": "d", "tool": "session.describe", "arguments": {}}).json()
    assert direct["data"]["budgets"]["requests_used"] >= 2


def test_two_reference_agents_run_without_engine_changes_and_replay(server):
    srv, admin, mgr = server
    results = {}
    for name, runtime in (("cash_only", "python"), ("scheduled_basket", "python"), ("random_actions", "typescript"), ("model_client", "python")):
        aid = register(admin, f"{name}_{runtime}", runtime)
        run = admin.post("/api/v1/runs", json={"agent_id": aid, "pack_id": "gen_dev_short", "agent_seed": "3", "mask_seed": "paired-reference-mask", "launch": {"name": name, "runtime": runtime}}).json()
        view = mgr.wait_for_run(run["run_id"], 300)
        assert view["state"] == "completed", (name, view["error"], mgr.agent_log(run["run_id"])[-500:])
        rep = admin.get(f"/api/v1/runs/{run['run_id']}/report").json()
        assert rep["outcome"]["valuation_complete"] is True
        replay = admin.post(f"/api/v1/runs/{run['run_id']}/replay").json()
        assert replay["ledger_matches"] and replay["state_matches"]
        results[name] = rep
    assert results["cash_only"]["outcome"]["headline_return"] == "0.00000000"
    assert results["cash_only"]["activity"]["orders_total"] == 0
    assert results["scheduled_basket"]["activity"]["confirmed_fills"] > 0
    assert results["model_client"]["inference"] == {"recorded": False} or results["model_client"]["activity"]["orders_total"] == 0


def test_no_live_provider_or_transactions_in_demo(server):
    """The network guard in conftest blocks non-loopback sockets; the demo path used only 127.0.0.1."""
    srv, admin, mgr = server
    assert srv.url.startswith("http://127.0.0.1")
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_pause_resume_and_comparison(server):
    srv, admin, mgr = server
    aid = register(admin, "pauser")
    run = admin.post("/api/v1/runs", json={"agent_id": aid, "pack_id": "gen_dev_short"}).json()
    tok = run["session_credential"]["token"]
    h = {"Authorization": f"Bearer {tok}"}
    admin.post(f"/api/v1/runs/{run['run_id']}/pause").raise_for_status()
    r = httpx.post(srv.url + "/agent/v1/commands", headers=h, json={"request_id": "p", "tool": "markets.list", "arguments": {}}).json()
    assert r["error"]["code"] == "RUN_PAUSED"
    admin.post(f"/api/v1/runs/{run['run_id']}/resume").raise_for_status()
    r = httpx.post(srv.url + "/agent/v1/commands", headers=h, json={"request_id": "p", "tool": "markets.list", "arguments": {}}).json()
    assert r["status"] == "ok"
    httpx.post(srv.url + "/agent/v1/commands", headers=h, json={"request_id": "f", "tool": "session.finish", "arguments": {}})
    a = next(a["agent_id"] for a in admin.get("/api/v1/agents").json()["items"] if a["name"] == "scheduled_basket_python")
    b = next(a["agent_id"] for a in admin.get("/api/v1/agents").json()["items"] if a["name"] == "cash_only_python")
    cmp = admin.post("/api/v1/comparisons", json={"agent_a": a, "agent_b": b}).json()
    assert cmp["summary"]["episodes_paired"] == 1
    assert "FEW_DISTINCT_PERIODS_DESCRIPTIVE_ONLY" in cmp["warnings"]
    assert "significance" not in json.dumps(cmp["summary"])
    study = admin.post("/api/v1/studies", json={"candidates": [a, b], "pack_ids": [cmp["per_episode"][0]["runs_a"][0]["run_id"]], "comparison_id": cmp["comparison_id"], "intended_outcome": "forward test", "prediction": "a differs from b"}).json()
    assert study["predictive_validity"] == "not_established"


def test_export_roles_and_exposure(server):
    srv, admin, mgr = server
    runs = admin.get("/api/v1/runs").json()["items"]
    done = next(r for r in runs if r["state"] == "completed" and r["has_report"])
    part = admin.get(f"/api/v1/runs/{done['run_id']}/export", params={"role": "participant"}).json()
    assert "ledger" not in part and "alias_mappings" not in part and part["report"]["versions"]["pack_id"] == "hidden"
    assert "pack_id" not in part["run"]
    adm = admin.get(f"/api/v1/runs/{done['run_id']}/export", params={"role": "admin", "include_mappings": "true"}).json()
    assert "alias_mappings" in adm and adm["run"]["exposed"] is True
    assert admin.get(f"/api/v1/runs/{done['run_id']}").json()["exposed"] is True


def test_conformance_suite_passes(server):
    srv, admin, mgr = server
    aid = register(admin, "conformance")
    run = admin.post("/api/v1/runs", json={"agent_id": aid, "pack_id": "gen_dev_short"}).json()
    sys.path.insert(0, str(REPO / "sdk" / "python"))
    from market_replay_client import MarketReplayClient
    from market_replay_client.conformance import run_conformance

    results = run_conformance(MarketReplayClient(srv.url, run["session_credential"]["token"]))
    failed = [r for r in results if not r[1]]
    assert not failed, failed
