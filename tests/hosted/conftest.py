from __future__ import annotations

import os

import pytest

# Postgres tests reset their database. Never probe or select a user's local database implicitly.
PG_URL = os.environ.get("TEST_DATABASE_URL")


def pg_available() -> bool:
    if not PG_URL:
        return False
    try:
        import psycopg

        with psycopg.connect(PG_URL, connect_timeout=2) as c:
            c.execute("SELECT 1")
        return True
    except Exception:
        return False


@pytest.fixture(params=["sqlite", "postgres"])
def store_url(request, tmp_path):
    if request.param == "sqlite":
        return str(tmp_path / "cp.sqlite")
    if not pg_available():
        pytest.skip("set TEST_DATABASE_URL to an available disposable Postgres test database")
    from market_replay.service.db import open_store

    s = open_store(PG_URL)
    s.reset_for_tests()
    s.close()
    return PG_URL
