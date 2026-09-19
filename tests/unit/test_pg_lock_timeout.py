"""Exercise PostgreSQL lock cleanup without connecting to a user's database."""

from types import SimpleNamespace

import pytest

from market_replay.service.db import PgStore, RunBusy


@pytest.mark.parametrize("timeout", [0.025, 120])
def test_postgres_lock_wait_is_bounded_and_failed_transaction_is_closed(timeout):
    class LockNotAvailable(Exception):
        pass

    class QueryCanceled(Exception):
        pass

    calls = []

    class Connection:
        def execute(self, sql, params):
            calls.append((sql, params))
            if "pg_advisory_xact_lock" in sql:
                raise LockNotAvailable()

        def rollback(self):
            calls.append("rollback")

        def close(self):
            calls.append("close")

    def connect(url, **options):
        assert options["connect_timeout"] == 5
        assert options["prepare_threshold"] is None
        assert "statement_timeout" in options["options"]
        assert "idle_in_transaction" not in options["options"]
        return Connection()

    store = PgStore.__new__(PgStore)
    store.url = "unused"
    store._psycopg = SimpleNamespace(connect=connect, errors=SimpleNamespace(LockNotAvailable=LockNotAvailable, QueryCanceled=QueryCanceled))
    with pytest.raises(RunBusy):
        with store.run_lock("run_x", timeout=timeout):
            pytest.fail("Must not enter without lock ownership")
    assert calls[0] == ("SELECT set_config('lock_timeout', %s, true)", (f"{int(timeout * 1000)}ms",))
    if timeout == 120:
        assert calls[1] == ("SELECT set_config('statement_timeout', %s, true)", ("121000ms",))
    assert calls[-2:] == ["rollback", "close"]

