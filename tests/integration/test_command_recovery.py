"""Durable command retries, complete transfer bodies, and deliberate finalization."""
import gzip
import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from http.client import IncompleteRead
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from market_replay.datasets.generator import dev_short_config, generate_pack
from market_replay.engine.session import Session, replay_trace
from market_replay.service.app import create_app
from market_replay.service.embedded import EmbeddedServer
from market_replay.service.runs import RunManager


@pytest.fixture
def setup_run(tmp_path, dev_pack_dir):
    manager = RunManager(data_dir=tmp_path / "data")
    manager.import_pack(dev_pack_dir, "episode")
    agent = manager.register_agent(name="retry", version="1", runtime="external", capabilities=[], config={})
    run = manager.create_run(agent_id=agent["agent_id"], pack_ref="episode")
    yield manager, run
    manager.close()


def load_starter():
    path = Path(__file__).parents[2] / "skills/market-replay/scripts/market_replay_agent.py"
    spec = importlib.util.spec_from_file_location("recovery_starter", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_retry_survives_cold_cache_conflicts_and_finish(setup_run):
    manager, run = setup_run
    rid, token = run["run_id"], run["session_credential"]["token"]
    first = manager.handle_command(token, "advance", "clock.advance", {"advance_ms": 300_000})
    assert first.status == "ok"
    manager._contexts.clear()
    retry = manager.handle_command(token, "advance", "clock.advance", {"advance_ms": 300_000})
    assert retry == first
    assert manager.store.trace_len(rid) == 1
    assert manager._contexts == {}  # No cold replay is needed to serve the receipt.
    conflict = manager.handle_command(token, "advance", "clock.advance", {"advance_ms": 1})
    assert conflict.error.code == "IDEMPOTENCY_CONFLICT"
    assert conflict.clock_ms == first.clock_ms
    warning = manager.handle_command(token, "warn", "session.finish", {})
    assert warning.status == "error" and warning.error.details["confirmation_required"]
    assert warning.error.details["orders_total"] == 0
    assert warning.clock_ms == first.clock_ms
    assert manager.store.run(rid)["state"] == "running"
    done = manager.handle_command(token, "done", "session.finish", {"confirm": True})
    assert done.status == "ok"
    count = manager.store.trace_len(rid)
    manager._contexts.clear()
    assert manager.handle_command(token, "done", "session.finish", {"confirm": True}) == done
    assert manager.store.trace_len(rid) == count
    assert manager.handle_command(token, "advance", "clock.advance", {"advance_ms": 300_000}).clock_ms == done.clock_ms


def test_legacy_successful_finish_replays_but_new_refusal_stays_refused(fresh_pack):
    s = Session.create(session_id="s", pack=fresh_pack, bankroll_raw=1_000_000, mask_seed="m", engine_seed="e")
    s.handle("warning", "session.finish", {})
    trace = [asdict(r) for r in s.trace]
    replay = replay_trace(fresh_pack, trace, bankroll_raw=1_000_000, mask_seed="m", engine_seed="e", session_id="s")
    assert not replay.finished and replay.trace_hash() == s.trace_hash()
    s.handle("legacy-finish", "session.finish", {}, replaying=True)
    replay = replay_trace(fresh_pack, [asdict(r) for r in s.trace], bankroll_raw=1_000_000, mask_seed="m", engine_seed="e", session_id="s")
    assert replay.finished and replay.result_hash() == s.result_hash()


def test_starter_recovers_lost_execution_clock_skew_and_confirmed_exit(setup_run, monkeypatch):
    manager, run = setup_run
    client = TestClient(create_app(manager, "adm_retry"))
    starter = load_starter()
    monkeypatch.setattr(starter.time, "sleep", lambda _: None)
    lost, ids = set(), []
    def transport(method, url, body=None, token=None):
        response = client.post("/agent/v1/commands", json=body, headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200, response.text
        ids.append(body["request_id"])
        # Drop a response AFTER server execution, including an order submission.
        if body["tool"] in ("session.snapshot", "broker.submit") and body["request_id"] not in lost:
            lost.add(body["request_id"])
            raise IncompleteRead(response.content[:100], len(response.content) - 100)
        return response.json()
    monkeypatch.setattr(starter, "http", transport)
    s = starter.Session("http://test/agent/v1/commands", run["session_credential"]["token"])
    info = s.ok("session.describe")
    s.ok("clock.advance", advance_ms=300_000)
    s.ok("session.snapshot", format="compact", limit=25)
    s.clock_ms = 0  # Deliberately stale client clock.
    refused = s.call("clock.advance", to_ms=0)
    assert refused["status"] == "error" and s.clock_ms == refused["clock_ms"] > 0
    s.ok("clock.advance", advance_ms=1)
    market = s.ok("markets.list", limit=25)["items"][0]
    pid = market["pool_id"]
    base = s.ok("markets.get", pool_id=pid)["base_asset"]
    cash = info["numeraire"]["asset_id"]
    s.ok("broker.quote", pool_id=pid, asset_in=cash, amount_in_raw="1000")
    s.ok("broker.submit", pool_id=pid, asset_in=cash, asset_out=base, amount_in_raw="1000",
         min_amount_out_raw="0", deadline_ms=s.clock_ms + 60_000, idempotency_key="buy")
    s.ok("clock.advance", advance_ms=20_000)
    balances = s.ok("portfolio.get")["balances"]
    quantity = next(b["available_raw"] for b in balances if b["asset_id"] == base)
    s.ok("broker.quote", pool_id=pid, asset_in=base, amount_in_raw=quantity)
    s.ok("broker.submit", pool_id=pid, asset_in=base, asset_out=cash, amount_in_raw=quantity,
         min_amount_out_raw="0", deadline_ms=s.clock_ms + 60_000, idempotency_key="sell")
    s.ok("clock.advance", advance_ms=20_000)
    portfolio = s.ok("portfolio.get")
    assert not portfolio["pending_orders"]
    assert all(b["asset_id"] == cash or int(b["available_raw"]) == 0 for b in portfolio["balances"])
    s.ok("clock.advance", to_ms=info["episode"]["duration_ms"])
    s.ok("session.finish", confirm=True)
    report = manager.report(run["run_id"])
    assert report["activity"]["confirmed_fills"] == 2
    assert report["activity"]["orders_total"] == 2
    assert report["activity"]["requests_total"] == len(set(ids))


def test_starter_exhaustion_preserves_pending_request_without_finishing(setup_run, monkeypatch):
    manager, run = setup_run
    starter = load_starter()
    monkeypatch.setattr(starter.time, "sleep", lambda _: None)
    record, ids = {}, []
    def failing(method, url, body, token):
        ids.append(body["request_id"])
        manager.handle_command(token, body["request_id"], body["tool"], body["arguments"])
        raise IncompleteRead(b'{"', 100)
    monkeypatch.setattr(starter, "http", failing)
    session = starter.Session("http://test", run["session_credential"]["token"], run_record=record)
    with pytest.raises(RuntimeError, match="Pending request"):
        starter.play(session)
    assert len(ids) == 5 and len(set(ids)) == 1
    assert manager.store.trace_len(run["run_id"]) == 1
    assert manager.store.run(run["run_id"])["state"] == "running"
    assert record["pending_command"]["request_id"] == ids[0]
    monkeypatch.setattr(starter, "http", lambda method, url, body, token: manager.handle_command(token, body["request_id"], body["tool"], body["arguments"]).model_dump(mode="json"))
    restarted = starter.Session("http://test", run["session_credential"]["token"], run_record=json.loads(json.dumps(record)))
    assert restarted.recover_pending()["status"] == "ok"
    assert manager.store.trace_len(run["run_id"]) == 1


def test_large_market_responses_have_complete_length_and_gzip_under_load(tmp_path):
    pack = tmp_path / "large"
    generate_pack(dev_short_config(n_pools=80), pack)
    manager = RunManager(data_dir=tmp_path / "data")
    server = EmbeddedServer(manager, "adm_large")
    server.start()
    try:
        manager.import_pack(pack, "large")
        agent = manager.register_agent(name="large", version="1", runtime="external", capabilities=[], config={})
        run = manager.create_run(agent_id=agent["agent_id"], pack_ref="large", mask_seed="large-transfer-199")
        token = run["session_credential"]["token"]
        manager.handle_command(token, "advance", "clock.advance", {"to_ms": 7_000_000})
        markets = manager.handle_command(token, "markets", "markets.list", {"limit": 500})
        pid = markets.data["items"][0]["pool_id"]
        def read(i):
            tool, args = ("markets.list", {"limit": 500}) if i % 2 == 0 else ("market.trades", {"pool_id": pid, "limit": 500})
            encoding = "gzip" if i < 8 else "identity"
            with httpx.stream("POST", server.url + "/agent/v1/commands", headers={"Authorization": f"Bearer {token}", "Accept-Encoding": encoding},
                              json={"request_id": f"large-{i}", "tool": tool, "arguments": args}, timeout=30) as response:
                raw = b"".join(response.iter_raw())
                assert response.status_code == 200
                assert len(raw) == int(response.headers["content-length"])
                assert response.headers.get("content-encoding") == ("gzip" if encoding == "gzip" else None)
                decoded = gzip.decompress(raw) if encoding == "gzip" else raw
                assert len(decoded) > 15_000
                env = json.loads(decoded)
                assert env["status"] == "ok" and env["data"]["items"]
                return len(decoded)
        with ThreadPoolExecutor(max_workers=4) as executor:
            assert len(list(executor.map(read, range(12)))) == 12
    finally:
        server.stop()


@pytest.mark.parametrize("committed", [False, True])
def test_uncertain_trace_write_rebuilds_without_duplicate_execution(setup_run, monkeypatch, committed):
    manager, run = setup_run
    token, rid = run["session_credential"]["token"], run["run_id"]
    original = manager.store.append_trace
    def fail(*args, **kwargs):
        if committed:
            original(*args, **kwargs)
        raise OSError("connection lost during commit")
    monkeypatch.setattr(manager.store, "append_trace", fail)
    with pytest.raises(OSError):
        manager.handle_command(token, "uncertain", "clock.advance", {"advance_ms": 10_000})
    assert rid not in manager._contexts
    monkeypatch.setattr(manager.store, "append_trace", original)
    recovered = manager.handle_command(token, "uncertain", "clock.advance", {"advance_ms": 10_000})
    assert recovered.status == "ok" and recovered.clock_ms == 10_000
    assert manager.store.trace_len(rid) == 1


def test_starter_rejects_incomplete_gzip_before_accepting_response(monkeypatch):
    starter = load_starter()
    class Response:
        headers = {"Content-Encoding": "gzip"}
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def read(self):
            return gzip.compress(b'{"status":"ok"}')[:-5]
    monkeypatch.setattr(starter.urllib.request, "urlopen", lambda *a, **kw: Response())
    with pytest.raises(EOFError):
        starter.http("POST", "http://localhost/agent/v1/commands", {})
