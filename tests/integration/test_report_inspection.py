"""Report panels must not replay or contend with an agent just to inspect its evidence."""
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from threading import Event

from fastapi.testclient import TestClient

from market_replay.evaluation.debrief import timeline
from market_replay.service.app import create_app
from market_replay.service.inspection import observed_view
from market_replay.service.runs import RunManager


def finished_run(manager, dev_pack_dir):
    manager.import_pack(dev_pack_dir, "episode")
    agent = manager.register_agent(name="inspection", version="1", runtime="external", capabilities=[], config={})
    run = manager.create_run(agent_id=agent["agent_id"], pack_ref="episode")
    rid, token = run["run_id"], run["session_credential"]["token"]
    s = manager._contexts[rid].session
    key = s.sim.discovered_pools(0)[0]
    pool = s.pack.pools[key]
    cash = s.pack.numeraire
    asset = pool.asset1 if pool.asset0 == cash else pool.asset0
    pid = s.alias.pool(key)
    order = {"pool_id": pid, "asset_in": s.alias.asset(cash), "asset_out": s.alias.asset(asset),
             "amount_in_raw": "1000", "min_amount_out_raw": "0", "deadline_ms": 300_000,
             "idempotency_key": "buy", "reason": "Test visible activity"}
    assert manager.handle_command(token, "buy", "broker.submit", order).status == "ok"
    assert manager.handle_command(token, "wait", "clock.advance", {"to_ms": 60_000}).status == "ok"
    assert manager.handle_command(token, "done", "session.finish", {"confirm": True}).status == "ok"
    return rid, token, pid, s


def forbid_replay(*args, **kwargs):
    raise AssertionError("A saved report must not rebuild a session")


def test_completed_panels_survive_worker_loss_and_a_busy_command_lock(tmp_path, dev_pack_dir, monkeypatch):
    owner = RunManager(data_dir=tmp_path / "data")
    rid, token, pid, session = finished_run(owner, dev_pack_dir)
    before = dict(owner.store.run(rid)), owner.store.trace(rid), owner.store.usage_today()
    expected = observed_view(session, pid, 60_000)
    expected_timeline = timeline(session)
    other = RunManager(data_dir=tmp_path / "data")
    monkeypatch.setattr(other, "_ctx", forbid_replay)
    try:
        with TestClient(create_app(other, "adm_test")) as client, owner.store.run_lock(rid):
            result = client.get(f"/api/v1/runs/{rid}/observed", params={"pool_id": pid})
            assert result.status_code == 200, result.text
            assert result.json() == expected
            result = client.get(f"/api/v1/runs/{rid}/timeline", params={"cursor": 0, "limit": 1})
            assert result.status_code == 200, result.text
            page = result.json()
            assert page["total"] == len(session.trace) and page["next_cursor"] == 1
            assert page["items"] == expected_timeline["items"][:1]
            assert page["items"][0]["order"]["state"] == "confirmed"
            assert client.get(f"/api/v1/runs/{rid}/timeline", headers={"Authorization": f"Bearer {token}"}).status_code == 403
        assert other._contexts == {}
        assert before == (dict(owner.store.run(rid)), owner.store.trace(rid), owner.store.usage_today())
    finally:
        other.close()
        owner.close()


def test_legacy_chart_build_is_shared_and_does_not_block_the_timeline(tmp_path, dev_pack_dir, monkeypatch):
    owner = RunManager(data_dir=tmp_path / "data")
    rid, _token, pid, session = finished_run(owner, dev_pack_dir)
    expected = observed_view(session, pid, 60_000)
    before = dict(owner.store.run(rid)), owner.store.trace(rid)
    owner.store.execute("DELETE FROM docs WHERE run_id=? AND (kind LIKE 'observed_v1:%' OR kind='review_orders_v1')", (rid,))
    first = RunManager(data_dir=tmp_path / "data")
    second = RunManager(data_dir=tmp_path / "data")
    started, release = Event(), Event()
    rebuild = first._ctx

    def slow_rebuild(run_id):
        started.set()
        assert release.wait(5), "test did not release reconstruction"
        return rebuild(run_id)

    monkeypatch.setattr(first, "_ctx", slow_rebuild)
    monkeypatch.setattr(second, "_ctx", forbid_replay)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(first.observed, rid, pid)
            assert started.wait(2)
            b = pool.submit(second.observed, rid, pid)
            try:
                # The chart owns inspection:<run>, so evidence and command ownership stay available.
                with owner.store.run_lock(rid, timeout=0.02):
                    page = second.decision_timeline(rid, 0, 1)
                    assert page["items"][0]["delivered"] == before[1][0]["delivered"]
            finally:
                release.set()
            assert a.result(timeout=5) == expected
            assert b.result(timeout=5) == expected
        assert second._contexts == {}
        assert before == (dict(owner.store.run(rid)), owner.store.trace(rid))
    finally:
        release.set()
        second.close()
        first.close()
        owner.close()


def test_active_timeline_is_readable_but_active_chart_still_obeys_command_lock(tmp_path, dev_pack_dir, monkeypatch):
    owner = RunManager(data_dir=tmp_path / "data")
    owner.import_pack(dev_pack_dir, "episode")
    agent = owner.register_agent(name="active", version="1", runtime="external", capabilities=[], config={})
    run = owner.create_run(agent_id=agent["agent_id"], pack_ref="episode")
    rid, token = run["run_id"], run["session_credential"]["token"]
    owner.handle_command(token, "first", "session.describe", {})
    other = RunManager(data_dir=tmp_path / "data")
    monkeypatch.setattr(other, "_ctx", forbid_replay)
    monkeypatch.setattr(other.store, "run_lock", partial(other.store.run_lock, timeout=0.02))
    try:
        with TestClient(create_app(other, "adm_test")) as client, owner.store.run_lock(rid):
            assert client.get(f"/api/v1/runs/{rid}/timeline").status_code == 200
            busy = client.get(f"/api/v1/runs/{rid}/observed")
            assert busy.status_code == 503 and busy.json()["code"] == "RUN_BUSY"
            assert owner.store.trace_len(rid) == 1
        assert other._contexts == {}
    finally:
        other.close()
        owner.close()


def test_legacy_missing_deliveries_are_explicit_and_requests_remain_redacted(tmp_path, dev_pack_dir, monkeypatch):
    owner = RunManager(data_dir=tmp_path / "data")
    rid, _token, _pid, session = finished_run(owner, dev_pack_dir)
    trace = owner.store.trace(rid)
    trace[0].pop("delivered")
    trace[0]["arguments"]["reason"] = session.pack.manifest.title_private
    monkeypatch.setattr(owner.store, "trace", lambda _: trace)
    monkeypatch.setattr(owner, "_ctx", forbid_replay)
    try:
        page = owner.decision_timeline(rid, 0, 1)
        item = page["items"][0]
        assert item["delivered"]["evidence_basis"] == "not_recorded"
        assert item["delivered"]["payload"] is None
        assert session.pack.manifest.title_private not in str(page)
        assert item["request"]["reason"] == "[redacted]"
    finally:
        owner.close()


def test_derived_view_failure_does_not_fail_the_run(tmp_path, dev_pack_dir, monkeypatch):
    owner = RunManager(data_dir=tmp_path / "data")
    put_doc = owner.store.put_doc

    def fail_cache(run_id, kind, doc):
        if kind.startswith("observed_v1:"):
            raise RuntimeError("cache unavailable")
        return put_doc(run_id, kind, doc)

    monkeypatch.setattr(owner.store, "put_doc", fail_cache)
    try:
        rid, _token, _pid, _session = finished_run(owner, dev_pack_dir)
        assert owner.store.run(rid)["state"] == "completed"
        assert owner.report(rid)["outcome"]["primary_metric"] == "final_cash_return_v1"
    finally:
        owner.close()
