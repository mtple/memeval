"""Seam: the control-plane store. Same behaviour on SQLite (local) and Postgres (hosted)."""

from __future__ import annotations

import threading

import pytest

from market_replay.service.db import open_store


def test_open_store_picks_backend(store_url):
    s = open_store(store_url)
    assert s.backend in ("sqlite", "postgres")
    assert (s.backend == "postgres") == store_url.startswith("postgres")
    s.close()


def test_run_rows_and_docs_roundtrip(store_url):
    s = open_store(store_url)
    s.insert_run({"run_id": "run_a", "agent_id": "ag", "pack_id": "pk", "mode": "practice", "isolation": "trusted_external_client", "state": "queued", "bankroll_raw": "1", "mask_seed": "m", "engine_seed": "e", "profile_hash": "h", "token_hash": "t", "created_at": "now", "clock_ms": 0})
    s.update_run("run_a", state="running", clock_ms=5)
    assert s.run("run_a")["state"] == "running" and s.run("run_a")["clock_ms"] == 5
    assert s.run_by_token_hash("t")["run_id"] == "run_a"
    s.put_doc("run_a", "run_manifest", {"x": 1})
    s.put_doc("run_a", "run_manifest", {"x": 2})
    assert s.get_doc("run_a", "run_manifest") == {"x": 2}
    assert s.get_doc("run_a", "missing") is None
    s.close()


def test_trace_is_append_only_and_ordered(store_url):
    s = open_store(store_url)
    s.insert_run({"run_id": "run_b", "agent_id": "ag", "pack_id": "pk", "mode": "practice", "isolation": "trusted_external_client", "state": "queued", "bankroll_raw": "1", "mask_seed": "m", "engine_seed": "e", "profile_hash": "h", "token_hash": "t2", "created_at": "now", "clock_ms": 0})
    s.append_trace("run_b", [{"index": 0, "tool": "a"}, {"index": 1, "tool": "b"}])
    s.append_trace("run_b", [{"index": 2, "tool": "c"}])
    assert s.trace_len("run_b") == 3
    assert [r["tool"] for r in s.trace("run_b")] == ["a", "b", "c"]
    assert [r["tool"] for r in s.trace("run_b", after=1)] == ["c"]
    s.close()


def test_run_lock_serializes_across_connections(store_url):
    s1 = open_store(store_url)
    s2 = open_store(store_url)
    order: list[str] = []
    entered = threading.Event()
    release = threading.Event()

    def holder():
        with s1.run_lock("run_x"):
            order.append("s1-in")
            entered.set()
            release.wait(5)
            order.append("s1-out")

    def waiter():
        entered.wait(5)
        with s2.run_lock("run_x"):
            order.append("s2-in")

    t1 = threading.Thread(target=holder)
    t2 = threading.Thread(target=waiter)
    t1.start()
    t2.start()
    entered.wait(5)
    release.set()
    t1.join(10)
    t2.join(10)
    assert order == ["s1-in", "s1-out", "s2-in"]
    s1.close()
    s2.close()


def test_run_lock_times_out_without_taking_ownership(store_url):
    from market_replay.service.db import RunBusy

    owner, other = open_store(store_url), open_store(store_url)
    try:
        with owner.run_lock("busy"):
            with pytest.raises(RunBusy):
                with other.run_lock("busy", timeout=0.03):
                    pytest.fail("A second owner entered the critical section")
            # A timed-out waiter must not release the owner's lock.
            with other.try_run_lock("busy") as acquired:
                assert not acquired
        with other.run_lock("busy", timeout=0.03):
            pass
    finally:
        owner.close()
        other.close()


def test_usage_accounting(store_url):
    s = open_store(store_url)
    assert s.usage_today() == {"runs": 0, "cpu_seconds": 0.0}
    s.add_usage(runs=1, cpu_seconds=12.5)
    s.add_usage(runs=1, cpu_seconds=2.5)
    assert s.usage_today() == {"runs": 2, "cpu_seconds": 15.0}
    assert s.usage_month()["cpu_seconds"] == 15.0
    s.close()


def test_postgres_store_survives_a_dropped_connection(store_url, tmp_path):
    """Neon closes idle connections; a warm serverless instance must reconnect instead of failing every request."""
    if not store_url.startswith("postgres"):
        pytest.skip("postgres only")
    from market_replay.service.db import open_store

    st = open_store(store_url)
    st.add_usage(runs=1)
    st._conn.close()  # simulate the server dropping the session
    assert st.usage_today()["runs"] >= 1
    st.add_usage(runs=1)
    assert st.usage_today()["runs"] >= 2
    st.close()
