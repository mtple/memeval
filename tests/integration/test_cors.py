"""A statically hosted UI (e.g. Vercel) must be able to call a self-hosted backend only when the operator allows its origin."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from market_replay.service.app import create_app
from market_replay.service.runs import RunManager


@pytest.fixture()
def manager(tmp_path: Path) -> RunManager:
    return RunManager(data_dir=tmp_path / "data")


def test_no_cors_headers_by_default(manager: RunManager):
    app = create_app(manager, "adm_t")
    c = TestClient(app)
    r = c.get("/api/v1/health", headers={"Origin": "https://memeval-web.vercel.app"})
    assert r.status_code == 200
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}


def test_allowed_origin_gets_cors_and_preflight(manager: RunManager):
    app = create_app(manager, "adm_t", cors_origins=["https://memeval-web.vercel.app"])
    c = TestClient(app)
    pre = c.options("/api/v1/packs", headers={"Origin": "https://memeval-web.vercel.app", "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "authorization"})
    assert pre.status_code == 200
    assert pre.headers["access-control-allow-origin"] == "https://memeval-web.vercel.app"
    assert "authorization" in pre.headers["access-control-allow-headers"].lower()
    r = c.get("/api/v1/health", headers={"Origin": "https://memeval-web.vercel.app"})
    assert r.headers["access-control-allow-origin"] == "https://memeval-web.vercel.app"
    other = c.get("/api/v1/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in {k.lower() for k in other.headers}


def test_cors_origins_from_environment(manager: RunManager, monkeypatch: pytest.MonkeyPatch):
    from market_replay.service.app import cors_origins_from_env

    monkeypatch.setenv("MARKET_REPLAY_CORS_ORIGINS", "https://a.example, https://b.example/")
    assert cors_origins_from_env() == ["https://a.example", "https://b.example"]
    monkeypatch.delenv("MARKET_REPLAY_CORS_ORIGINS")
    assert cors_origins_from_env() == []
