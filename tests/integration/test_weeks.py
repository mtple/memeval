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


def test_week_requests_are_validated_capped_and_public_over_http(tmp_path: Path):
    fake = FakeBase()
    mgr = make(tmp_path, fake, str(tmp_path / "s.sqlite"))
    mgr.weeks.max_jobs_per_day = 1
    c = TestClient(create_app(mgr, "adm_w"))
    assert c.get("/api/v1/meta").json()["weeks_enabled"] is True
    assert c.post("/api/v1/weeks", json={"week_start": "not a date"}).status_code == 400
    assert c.post("/api/v1/weeks", json={"week_start": "2999-01-01"}).status_code == 400  # not finished yet
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
    mgr.close()


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
