"""Seam: agents that speak MCP over HTTP (OpenClaw, Hermes and similar) reach the same handler."""

from __future__ import annotations

import asyncio

import pytest

from market_replay.service.embedded import EmbeddedServer
from market_replay.service.runs import RunManager


@pytest.fixture(scope="module")
def server(tmp_path_factory, dev_pack_dir):
    mgr = RunManager(data_dir=tmp_path_factory.mktemp("mcpdata"))
    srv = EmbeddedServer(mgr, "adm_mcp").start()
    admin = srv.admin()
    admin.post("/api/v1/packs/import", json={"path": str(dev_pack_dir), "name": "gen_dev_short"}).raise_for_status()
    aid = admin.post("/api/v1/agents", json={"name": "mcp-agent", "version": "1", "runtime": "external"}).json()["agent_id"]
    run = admin.post("/api/v1/runs", json={"agent_id": aid, "pack_id": "gen_dev_short"}).json()
    yield srv, run
    srv.stop()


def test_streamable_http_mcp_endpoint(server):
    srv, run = server
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    token = run["session_credential"]["token"]

    async def go():
        async with streamablehttp_client(srv.url + "/agent/mcp", headers={"Authorization": f"Bearer {token}"}) as (read, write, _):
            async with ClientSession(read, write) as s:
                await s.initialize()
                tools = await s.list_tools()
                names = {t.name for t in tools.tools}
                res = await s.call_tool("session_describe", {"arguments": {}})
                adv = await s.call_tool("clock_advance", {"arguments": {"to_ms": 60_000}})
                return names, res, adv

    names, res, adv = asyncio.run(go())
    assert {"session_describe", "broker_submit", "clock_advance", "session_finish"} <= names
    payload = res.structuredContent or {}
    payload = payload.get("result", payload)
    assert payload["status"] == "ok" and payload["data"]["tools"]
    adv_payload = (adv.structuredContent or {}).get("result", adv.structuredContent)
    assert adv_payload["data"]["clock_ms"] == 60_000
    # the run's clock moved: MCP and HTTP share one session
    assert srv.admin().get(f"/api/v1/runs/{run['run_id']}").json()["clock_ms"] == 60_000


def test_mcp_session_tools_require_a_token_but_the_endpoint_is_open(server):
    """Anyone can initialize and call `enroll`; session tools without a token get a typed error, never data."""
    srv, _run = server
    import httpx

    r = httpx.post(srv.url + "/agent/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}, headers={"Accept": "application/json, text/event-stream"})
    assert r.status_code == 200
    call = httpx.post(srv.url + "/agent/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "portfolio_get", "arguments": {"arguments": {}}}}, headers={"Accept": "application/json, text/event-stream"})
    assert call.status_code == 200
    body = call.json()["result"]["structuredContent"]
    body = body.get("result", body)
    assert body["status"] == "error" and body["error"]["code"] == "UNAUTHORIZED"
