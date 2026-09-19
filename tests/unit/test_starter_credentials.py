"""Starter restarts reuse identity and run credentials, including after a lost response."""

import argparse
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "skills/market-replay/scripts/market_replay_agent.py"


@pytest.fixture
def starter():
    spec = importlib.util.spec_from_file_location("starter", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_interruption_keeps_all_tokens_and_resume_never_enrolls_or_creates(starter, tmp_path, monkeypatch, capsys):
    path = tmp_path / "credentials.json"
    server = "http://replay.test"
    calls, attempts = [], []

    def http(method, url, body=None, token=None):
        calls.append((method, url))
        if url.endswith("/enroll"):
            return {"agent_name": "bot", "agent_version": "1", "agent_token": "agn_private", "results_url": server}
        if url.endswith("/play"):
            return {"suite_id": "test-suite", "runs": [{"run_id": f"run_{i}", "pack_id": f"pack_{i}", "pack_name": f"episode_{i}", "session_credential": {"commands_url": server + "/agent/v1/commands", "token": f"agt_private_{i}"}} for i in (1, 2)]}
        if url.endswith("/runs/run_1"):
            return {"state": "running"}
        pytest.fail(f"Unexpected request {method} {url}")

    def play(session):
        # Both tokens must be on disk even if the very first command is interrupted.
        assert len(json.loads(path.read_text())["runs"]) == 2
        attempts.append(session.token)
        if attempts == ["agt_private_1"]:
            raise TimeoutError("lost response")
        return {"clock_ms": 100, "final_cash_raw": "1000000000000000000", "model_equity_raw": "1000000000000000000", "valuation_complete": True}

    monkeypatch.setattr(starter, "http", http)
    monkeypatch.setattr(starter, "play", play)
    args = argparse.Namespace(server=server, agent="bot", version="1", suite="test-suite", pack=None, all=False, resume=None)
    with starter.Credentials(path, server, "bot", "1") as credentials:
        assert starter.run_saved(args, credentials) == 1
    assert path.stat().st_mode & 0o777 == 0o600
    args.suite, args.resume = None, "all"
    with starter.Credentials(path, server, "bot", "1") as credentials:
        assert starter.run_saved(args, credentials) == 0
    assert attempts == ["agt_private_1", "agt_private_2", "agt_private_1"]
    assert calls.count(("POST", server + "/api/v1/enroll")) == 1
    assert calls.count(("POST", server + "/api/v1/play")) == 1
    output = capsys.readouterr().out
    assert "agn_private" not in output and "agt_private" not in output


def test_credential_scope_lock_and_atomic_save(starter, tmp_path, monkeypatch):
    path = tmp_path / "credentials.json"
    with starter.Credentials(path, "http://one.test", "bot", "1") as credentials:
        credentials.data["identity"] = {"agent_token": "saved"}
        credentials.save()
        before = path.read_bytes()
        with pytest.raises(BlockingIOError):
            with starter.Credentials(path, "http://one.test", "bot", "1"):
                pass
        def fail_replace(*_):
            raise OSError("disk failure")
        monkeypatch.setattr(starter.os, "replace", fail_replace)
        credentials.data["identity"]["agent_token"] = "new"
        with pytest.raises(OSError):
            credentials.save()
        assert path.read_bytes() == before
        assert sorted(p.name for p in tmp_path.iterdir()) == ["credentials.json", "credentials.json.lock"]
    with pytest.raises(ValueError, match="different server"):
        with starter.Credentials(path, "http://other.test", "bot", "1"):
            pass
