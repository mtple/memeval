"""Seam: the hosted entrypoint builds the app from environment variables and routes are wired for Vercel."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[2]


def test_app_from_env_uses_sqlite_when_no_database_url(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    monkeypatch.setenv("MARKET_REPLAY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MARKET_REPLAY_ADMIN_TOKEN", "adm_env")
    monkeypatch.setenv("MARKET_REPLAY_MAX_RUNS_PER_DAY", "7")
    from market_replay.service.hosted import build_hosted_app

    app, mgr = build_hosted_app()
    c = TestClient(app)
    assert c.get("/api/v1/health").json()["status"] == "ok"
    assert c.get("/api/v1/usage", headers={"Authorization": "Bearer adm_env"}).json()["caps"]["max_runs_per_day"] == 7
    assert mgr.store.backend == "sqlite"
    mgr.close()


def test_hosted_bootstrap_registers_fixture_packs(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("MARKET_REPLAY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MARKET_REPLAY_ADMIN_TOKEN", "adm_env")
    monkeypatch.setenv("MARKET_REPLAY_BOOTSTRAP", "dev")
    from market_replay.service.hosted import build_hosted_app

    app, mgr = build_hosted_app()
    c = TestClient(app)
    names = {p["name"] for p in c.get("/api/v1/packs", headers={"Authorization": "Bearer adm_env"}).json()["items"]}
    assert "gen_dev_short" in names
    mgr.close()


def test_vercel_config_routes_api_and_agent_to_the_function():
    cfg = json.loads((REPO / "vercel.json").read_text())
    rewrites = cfg["rewrites"]
    def dest(path: str) -> str | None:
        import re
        for r in rewrites:
            pat = "^" + re.sub(r":(\w+)\*", r"(?P<\1>.*)", r["source"]) + "$"
            try:
                if re.match(pat, path):
                    return r["destination"]
            except re.error:
                continue
        return None
    assert dest("/api/v1/health") == "/api/index"
    assert dest("/agent/v1/commands") == "/api/index"
    assert dest("/agent/mcp") == "/api/index"
    assert dest("/agents") == "/index.html"
    fn = cfg["functions"]["api/index.py"]
    assert fn["maxDuration"] <= 300 and fn["memory"] <= 1024
    assert "excludeFiles" in fn
    assert (REPO / "api" / "index.py").exists()
    assert "duckdb" not in (REPO / "requirements.txt").read_text()


def test_hosted_default_bootstrap_is_all_when_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("MARKET_REPLAY_BOOTSTRAP", raising=False)
    monkeypatch.setenv("MARKET_REPLAY_HOSTED", "1")
    monkeypatch.setenv("MARKET_REPLAY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MARKET_REPLAY_ADMIN_TOKEN", "adm_env")
    from market_replay.service.hosted import build_hosted_app

    app, mgr = build_hosted_app()
    c = TestClient(app)
    names = {p["name"] for p in c.get("/api/v1/packs", headers={"Authorization": "Bearer adm_env"}).json()["items"]}
    assert {"gen_dev_short", "gen_week_trending", "gen_week_reversal", "gen_week_sparse_missing", "gen_week_liquidity_shift"} <= names
    mgr.close()


def test_hosted_bootstrap_none_registers_nothing(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("MARKET_REPLAY_BOOTSTRAP", "none")
    monkeypatch.setenv("MARKET_REPLAY_HOSTED", "1")
    monkeypatch.setenv("MARKET_REPLAY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MARKET_REPLAY_ADMIN_TOKEN", "adm_env")
    from market_replay.service.hosted import build_hosted_app

    app, mgr = build_hosted_app()
    c = TestClient(app)
    assert c.get("/api/v1/packs", headers={"Authorization": "Bearer adm_env"}).json()["items"] == []
    mgr.close()


def test_vercel_entrypoint_binds_app_with_a_plain_assignment():
    """Vercel's Python detector only registers api/*.py files that assign `app` (or `handler`) at top level."""
    import ast

    tree = ast.parse((REPO / "api" / "index.py").read_text())
    plain = [n for n in tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "app" for t in n.targets)]
    assert plain, "api/index.py must contain a top-level `app = ...` assignment (no tuple unpacking)"
