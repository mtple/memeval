"""Real weeks on demand: requested from the app, collected in resumable slices across instances, ranked."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from market_replay.service.app import create_app
from market_replay.service.runs import RunManager
from market_replay.service.weeks import WeekJobs
from tests.integration.test_collector import FakeBase
from tests.integration.test_collector_cl import FakePoolManager

PERIOD_START = "2025-09-04T14:53:20Z"
PERIOD_END = "2025-09-04T15:53:20Z"


def make(tmp_path: Path, fake: FakeBase, store: str, slice_seconds: float = 240.0) -> RunManager:
    mgr = RunManager(data_dir=tmp_path / "data", store_url=store, hosted=True)
    # the fake chain's only pool starts trading inside the hour, so select by creation order (a real week uses the activity rule)
    mgr.weeks = WeekJobs(mgr, rpc_url="http://fake-rpc.local", slice_seconds=slice_seconds, max_requests=400, log_chunk_blocks=3000, max_pairs=2, selection_rule="earliest_created_wrapped_native_pairs_v1", transport=httpx.MockTransport(fake.handle), sleep=lambda s: None)
    return mgr


def test_a_period_is_collected_in_slices_across_fresh_instances_and_becomes_a_category(tmp_path: Path):
    fake = FakeBase()
    store = str(tmp_path / "store.sqlite")
    mgr = make(tmp_path, fake, store)
    job = mgr.weeks.request(PERIOD_START, PERIOD_END)
    assert job["status"] == "queued" and job["name"] == "base_period_01_1h" and job["label"] == "Base period 1 (1h)"
    assert "period_start_utc" not in job and job["dates_sealed"] is True  # public view: no calendar
    admin_view = mgr.weeks.job(job["job_id"], role="admin")
    assert admin_view["period_start_utc"] == PERIOD_START and admin_view["dates_sealed"] is False
    assert mgr.weeks.request(PERIOD_START, PERIOD_END)["job_id"] == job["job_id"]  # idempotent
    mgr.close()
    # every tick runs on a brand-new instance with an empty filesystem and a tiny slice
    ticks = 0
    for _ in range(60):
        fresh = make(tmp_path / f"i{ticks}", fake, store)
        out = fresh.weeks.tick(slice_seconds=0.001)
        fresh.close()
        ticks += 1
        if out["advanced"]["status"] in ("built", "failed"):
            break
    assert out["advanced"]["status"] == "built", out
    assert ticks > 2  # it really was sliced
    final = make(tmp_path / "final", fake, store)
    view = final.weeks.job(job["job_id"])
    assert view["pack_id"] and view["requests_used"] > 0 and "pack built" in view["note"]
    packs = {p["name"]: p for p in final.packs()}
    assert view["name"] in packs and packs[view["name"]]["origin"] == "historical_reconstruction"
    cats = final.leaderboard_categories()
    real = [c for c in cats if c["id"] == view["pack_id"]]
    assert real and real[0]["label"] == "Base period 1 (1h)" and "Real swaps recorded on Base" in real[0]["description"]
    assert "2025" not in real[0]["label"] + real[0]["description"] + view["name"]  # nothing public names the calendar
    pack_view = next(p for p in final.packs() if p["pack_id"] == view["pack_id"])
    assert "period_dev_mode" not in pack_view and "2025" not in json.dumps(pack_view)
    assert "period_dev_mode" in next(p for p in final.packs(reveal_dates=True) if p["pack_id"] == view["pack_id"])
    # the pack materializes on demand from its archive and can be run
    row, pack = final.load_pack(view["name"])
    assert pack.pack_id == view["pack_id"] and len(pack.tape) > 0
    assert final.weeks.tick()["advanced"] is None  # nothing left to do
    final.close()


def test_a_v4_period_is_collected_and_labelled_by_venue(tmp_path: Path):
    fake = FakePoolManager(protocol="uniswap_v4")
    store = str(tmp_path / "store.sqlite")
    mgr = RunManager(data_dir=tmp_path / "data", store_url=store, hosted=True)
    mgr.weeks = WeekJobs(mgr, rpc_url="http://fake-rpc.local", slice_seconds=240, max_requests=600, log_chunk_blocks=3000, max_pairs=2, selection_rule="earliest_created_wrapped_native_pairs_v1", transport=httpx.MockTransport(fake.handle), sleep=lambda s: None)
    v2 = mgr.weeks.request(PERIOD_START, PERIOD_END)  # same period on v2 pairs is a different job
    job = mgr.weeks.request(PERIOD_START, PERIOD_END, protocol="uniswap_v4")
    assert job["job_id"] != v2["job_id"] and job["protocol"] == "uniswap_v4" and job["label"] == "Base period 2 (1h), v4 pools"
    assert v2["label"] == "Base period 1 (1h)"
    assert mgr.weeks.request(PERIOD_START, PERIOD_END, protocol="uniswap_v4")["job_id"] == job["job_id"]
    try:
        mgr.weeks.request(PERIOD_START, PERIOD_END, protocol="sushi")
    except Exception as e:  # noqa: BLE001
        assert "unknown venue" in str(e)
    else:
        raise AssertionError("unknown venue accepted")
    # the v2 job goes first (older); the fake chain serves v4 logs, so v2 collection blocks and the v4 one builds
    mgr.store.update_week_job(v2["job_id"], status="failed", error="skipped in test")
    out = None
    for _ in range(80):
        out = mgr.weeks.tick(slice_seconds=0.001)
        if out["advanced"] is None or out["advanced"]["status"] in ("built", "failed"):
            break
    assert out and out["advanced"] and out["advanced"]["status"] == "built", out
    view = mgr.weeks.job(job["job_id"])
    row, pack = mgr.load_pack(view["name"])
    assert any(p.model == "uniswap_v4_cl" for p in pack.pools.values()) and any(r["kind"] == "cl_swap" for r in pack.tape)
    cats = mgr.leaderboard_categories()
    assert any(c["id"] == view["pack_id"] and c["label"] == "Base period 2 (1h), v4 pools" for c in cats)
    mgr.close()


def test_a_finished_week_can_be_rebuilt_under_the_same_sealed_name(tmp_path: Path):
    fake = FakeBase()
    store = str(tmp_path / "store.sqlite")
    mgr = make(tmp_path, fake, store)
    job = mgr.weeks.request(PERIOD_START, PERIOD_END)
    for _ in range(60):
        out = mgr.weeks.tick(slice_seconds=0.001)
        if out["advanced"]["status"] in ("built", "failed"):
            break
    first = mgr.weeks.job(job["job_id"])
    assert first["status"] == "built" and first["qualification"] == "research"
    c = TestClient(create_app(mgr, "adm_w"))
    r = c.post(f"/api/v1/weeks/{job['job_id']}/rebuild")
    assert r.status_code == 200 and r.json()["status"] == "queued" and r.json()["pack_id"] is None
    assert all(p["pack_id"] != first["pack_id"] for p in mgr.packs())  # the old pack is forgotten
    assert c.post(f"/api/v1/weeks/{job['job_id']}/rebuild").status_code == 409  # already collecting
    for _ in range(60):
        out = mgr.weeks.tick(slice_seconds=0.001)
        if out["advanced"]["status"] in ("built", "failed"):
            break
    again = mgr.weeks.job(job["job_id"])
    assert again["status"] == "built" and again["name"] == first["name"] and again["label"] == first["label"]
    assert sum(1 for p in mgr.packs() if p["name"] == first["name"]) == 1
    mgr.close()


def test_a_diagnostic_week_from_an_older_collector_is_rebuilt_once_on_an_idle_tick(tmp_path: Path, monkeypatch):
    import market_replay.service.weeks as weeks_mod

    fake = FakeBase(hidden_drift=True)  # this chain never reconciles: the pack comes out diagnostic_only
    mgr = make(tmp_path, fake, str(tmp_path / "s.sqlite"))
    job = mgr.weeks.request(PERIOD_START, PERIOD_END)
    for _ in range(60):
        out = mgr.weeks.tick(slice_seconds=0.001)
        if out["advanced"]["status"] in ("built", "failed"):
            break
    assert mgr.weeks.job(job["job_id"])["qualification"] == "diagnostic_only"
    assert mgr.weeks.tick()["advanced"] is None  # same collector version: nothing to redo
    monkeypatch.setattr(weeks_mod, "COLLECTOR_VERSION", "9999.1")
    out = mgr.weeks.tick(slice_seconds=0.001)
    assert out.get("rebuilt") is True and out["advanced"]["status"] == "queued"
    for _ in range(60):
        out = mgr.weeks.tick(slice_seconds=0.001)
        if out["advanced"]["status"] in ("built", "failed"):
            break
    assert out["advanced"]["status"] == "built"
    assert mgr.weeks.tick()["advanced"] is None  # rebuilt under this version: not again
    mgr.close()


def test_week_requests_are_validated_capped_and_public_over_http(tmp_path: Path):
    fake = FakeBase()
    mgr = make(tmp_path, fake, str(tmp_path / "s.sqlite"))
    mgr.weeks.max_jobs_per_day = 1
    c = TestClient(create_app(mgr, "adm_w"))
    assert c.get("/api/v1/meta").json()["weeks_enabled"] is True
    assert c.post("/api/v1/weeks", json={"week_start": "not a date"}).status_code == 400
    assert c.post("/api/v1/weeks", json={"week_start": "2999-01-01"}).status_code == 400  # not finished yet
    assert c.post("/api/v1/weeks", json={"week_start": PERIOD_START, "period_end": PERIOD_END, "protocol": "curve"}).status_code == 400
    r = c.post("/api/v1/weeks", json={"week_start": PERIOD_START, "period_end": PERIOD_END})
    assert r.status_code == 201 and r.json()["status"] == "queued"
    assert c.post("/api/v1/weeks", json={"week_start": "2025-08-04"}).status_code == 429  # daily cap
    listing = c.get("/api/v1/weeks").json()
    assert listing["enabled"] and len(listing["items"]) == 1 and "period_start_utc" not in listing["items"][0]
    assert "period_start_utc" in c.get("/api/v1/weeks", headers={"Authorization": "Bearer adm_w"}).json()["items"][0]
    t = c.post("/api/v1/weeks/tick?slice=5").json()
    assert t["advanced"]["status"] in ("collecting", "built")
    assert c.get(f"/api/v1/weeks/{r.json()['job_id']}").status_code == 200
    mgr.close()


def test_weeks_are_disabled_without_an_endpoint(tmp_path: Path):
    mgr = RunManager(data_dir=tmp_path / "data")
    mgr.weeks = WeekJobs(mgr, rpc_url=None)
    c = TestClient(create_app(mgr, "adm_w"))
    assert c.get("/api/v1/weeks").json() == {"enabled": False, "items": []}
    assert c.post("/api/v1/weeks", json={"week_start": "2025-09-01"}).status_code == 503
    assert c.get("/api/v1/meta").json()["weeks_enabled"] is False
    mgr.close()


def test_a_tick_during_a_running_slice_reports_busy_instead_of_waiting(tmp_path: Path):
    fake = FakeBase()
    mgr = make(tmp_path, fake, str(tmp_path / "s.sqlite"), slice_seconds=100)
    job = mgr.weeks.request(PERIOD_START, PERIOD_END)
    # simulate a slice that started moments ago on another instance (it holds a lease)
    mgr.store.update_week_job(job["job_id"], status="collecting", lease_until="2999-01-01T00:00:00Z")
    out = mgr.weeks.tick(slice_seconds=0.001)
    assert out["advanced"] is None and out["busy"] is True
    # a lease that expired while the holder is still inside its slice: the lock says busy, nobody waits
    mgr.store.update_week_job(job["job_id"], lease_until="2000-01-01T00:00:00Z")
    other = make(tmp_path / "other", fake, str(tmp_path / "s.sqlite"), slice_seconds=100)
    with other.store.run_lock("weekjob:" + job["job_id"]):
        out = mgr.weeks.tick(slice_seconds=0.001)
    assert out["advanced"] is None and out["busy"] is True
    other.close()
    mgr.close()


def test_work_state_syncs_per_file_and_only_changed_files_travel(tmp_path: Path):
    fake = FakeBase()
    store = str(tmp_path / "s.sqlite")
    mgr = make(tmp_path, fake, store)
    job = mgr.weeks.request(PERIOD_START, PERIOD_END)
    out = mgr.weeks.tick(slice_seconds=0.001)
    assert out["advanced"]["status"] == "collecting"
    index = mgr.store.week_job_file_index(job["job_id"])
    assert "checkpoints.json" in index and not any(p.startswith("receipts/") for p in index)
    assert mgr.store.week_job_archive(job["job_id"]) is None  # no monolithic archive any more
    # nothing changed on disk -> nothing re-uploaded
    work = next(p for p in (tmp_path / "data" / "packs" / "historical").iterdir() if p.name.endswith("_work"))
    assert mgr.weeks._save_work(job["job_id"], work)["uploaded"] == 0
    # a fresh instance restores the same bytes and continues
    fresh = make(tmp_path / "fresh", fake, store)
    for _ in range(60):
        out = fresh.weeks.tick(slice_seconds=0.001)
        if out["advanced"]["status"] in ("built", "failed"):
            break
    assert out["advanced"]["status"] == "built", out
    assert fresh.store.week_job_file_index(job["job_id"]) == {}  # cleaned up once the pack exists
    fresh.close()
    mgr.close()


def test_a_legacy_archive_is_restored_and_pool_logs_migrate_out_of_checkpoints(tmp_path: Path):
    from market_replay.service.weeks import _targz

    fake = FakeBase()
    store = str(tmp_path / "s.sqlite")
    mgr = make(tmp_path, fake, store)
    job = mgr.weeks.request(PERIOD_START, PERIOD_END)
    # run until the pool scan has started, then rewrite the work dir in the old layout (logs inside checkpoints.json)
    work = None
    for _ in range(60):
        out = mgr.weeks.tick(slice_seconds=0.001)
        work = next((p for p in (tmp_path / "data" / "packs" / "historical").iterdir() if p.name.endswith("_work")), None)
        if work and (work / "logs").exists():
            break
    assert work is not None and (work / "logs").exists()
    ck = json.loads((work / "checkpoints.json").read_text())
    for f in (work / "logs").glob("*.jsonl"):
        ck[f"pairlogs:{f.stem}:logs"] = [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
        f.unlink()
    (work / "checkpoints.json").write_text(json.dumps(ck))
    mgr.store.delete_week_job_files(job["job_id"])
    mgr.store.set_week_job_archive(job["job_id"], _targz(work))
    mgr.close()
    fresh = make(tmp_path / "fresh", fake, store)
    for _ in range(60):
        out = fresh.weeks.tick(slice_seconds=0.001)
        if out["advanced"]["status"] in ("built", "failed"):
            break
    assert out["advanced"]["status"] == "built", out
    fresh.close()


def test_rpc_urls_never_reach_the_logs(caplog, tmp_path: Path):
    import logging

    fake = FakeBase()
    mgr = make(tmp_path, fake, str(tmp_path / "s.sqlite"))
    mgr.weeks.rpc_url = "http://fake-rpc.local/rpc/v1/base/SECRETKEY123"
    mgr.weeks.request(PERIOD_START, PERIOD_END)
    with caplog.at_level(logging.DEBUG):
        mgr.weeks.tick(slice_seconds=0.001)
    assert "SECRETKEY123" not in caplog.text
    mgr.close()


def test_log_chunk_follows_the_provider_cap(tmp_path: Path):
    mgr = RunManager(data_dir=tmp_path / "data")
    assert WeekJobs(mgr, rpc_url="https://api.developer.coinbase.com/rpc/v1/base/KEY", log_chunk_blocks=10000).log_chunk_blocks == 1000
    assert WeekJobs(mgr, rpc_url="https://base-mainnet.g.alchemy.com/v2/KEY", log_chunk_blocks=10000).log_chunk_blocks == 2000
    assert WeekJobs(mgr, rpc_url="https://example-node.quiknode.pro/KEY/", log_chunk_blocks=10000).log_chunk_blocks == 10000
    mgr.close()
