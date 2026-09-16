from __future__ import annotations

import os

import pytest

PG_URL = os.environ.get("TEST_DATABASE_URL", "postgresql://memeval:memeval@127.0.0.1:5432/memeval_test")


def pg_available() -> bool:
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
        pytest.skip("no test Postgres at TEST_DATABASE_URL")
    from market_replay.service.db import open_store

    s = open_store(PG_URL)
    s.reset_for_tests()
    s.close()
    return PG_URL
