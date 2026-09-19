"""A contending request returns promptly and never starts another cold replay."""

from functools import partial

from fastapi.testclient import TestClient

from market_replay.service.app import create_app
from market_replay.service.runs import RunManager


def test_busy_http_and_mcp_keep_the_run_resumable(tmp_path, dev_pack_dir, monkeypatch):
    owner = RunManager(data_dir=tmp_path / "data")
    owner.import_pack(dev_pack_dir, "episode")
    agent = owner.register_agent(name="contended", version="1", runtime="external", capabilities=[], config={})
    run = owner.create_run(agent_id=agent["agent_id"], pack_ref="episode")
    rid, token = run["run_id"], run["session_credential"]["token"]
    other = RunManager(data_dir=tmp_path / "data")
    monkeypatch.setattr(other.store, "run_lock", partial(other.store.run_lock, timeout=0.03))
    try:
        with TestClient(create_app(other, "adm_contention")) as client:
            with owner.store.run_lock(rid):
                response = client.post("/agent/v1/commands", headers={"Authorization": f"Bearer {token}"}, json={"request_id": "r", "tool": "session.describe", "arguments": {}})
                assert response.status_code == 503 and response.json()["code"] == "RUN_BUSY"
                assert response.headers["Retry-After"] == "1"
                mcp = client.post("/agent/mcp", headers={"Accept": "application/json, text/event-stream"}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "session_describe", "arguments": {"token": token}}})
                payload = mcp.json()["result"]["structuredContent"]
                payload = payload.get("result", payload)
                assert payload["error"]["code"] == "RUN_BUSY"
                assert client.get(f"/api/v1/runs/{rid}").json()["clock_ms"] == 0
                assert other._contexts == {}
                assert other.store.trace_len(rid) == 0
            resumed = other.handle_command(token, "resume", "session.snapshot", {"format": "compact"})
            assert resumed.status == "ok"
            assert other.store.trace_len(rid) == 1
            assert other._contexts[rid].session.budget.requests == 1
    finally:
        owner.close()
        other.close()
