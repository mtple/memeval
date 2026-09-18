"""Every launch inside the period, in bulk, against the fake chains."""

from __future__ import annotations

from pathlib import Path

import httpx
import yaml

from market_replay.collectors.launches import run_launch_collection
from market_replay.datasets.pack import Pack
from tests.integration.test_weeks_fakes import PERIOD_END, PERIOD_START, FakeAllVenues


def test_every_traded_launch_inside_the_period_is_recorded_and_discoverable_from_creation(tmp_path: Path):
    fake = FakeAllVenues()
    cfg = {"rpc_url_env": "X", "chain": "base", "period_start_utc": PERIOD_START, "period_end_utc": PERIOD_END, "prehistory_hours": 1, "venues": ["uniswap_v2", "uniswap_v4"], "log_chunk_blocks": 3000, "max_requests": 4000, "out_dir": "packs/launches"}
    p = tmp_path / "cfg.yaml"
    p.write_text(yaml.safe_dump(cfg))
    res = run_launch_collection(p, tmp_path, transport=httpx.MockTransport(fake.handle), rpc_url_override="http://fake-rpc.local", sleep=lambda s: None)
    assert res["status"] == "pack_built", res
    pack = Pack.load(tmp_path / "packs" / "launches")
    # the v4 fake initializes one WETH pool inside the period; the v2 fake's pair predates it (not a launch)
    assert res["launches_per_venue"]["uniswap_v4"] >= 1 and res["traded_launches_per_venue"]["uniswap_v4"] >= 1
    assert res["traded_launches_per_venue"]["uniswap_v2"] == 0
    assert pack.manifest.universe.selection_rule_version == "all_launches_in_window_v1"
    for pool in pack.pools.values():
        assert pool.initial_state_basis == "pool_initialized_inside_window"
        assert pool.discovery_available_utc_ms == pool.created_time_utc_ms + 4000  # discoverable from creation, not from a swap count
    assert any(r["kind"] == "cl_init" for r in pack.tape) and pack.validation["resulting_qualification"] == "research"
    # resumable: a second run over the same work directory reads no logs again
    calls = fake.calls
    res2 = run_launch_collection(p, tmp_path, transport=httpx.MockTransport(fake.handle), rpc_url_override="http://fake-rpc.local", sleep=lambda s: None)
    assert res2["status"] == "pack_built" and res2["tape_events"] == res["tape_events"] and res2["pools"] == res["pools"] and fake.calls - calls <= 3


def test_a_combined_week_can_carry_every_launch_next_to_the_established_pools(tmp_path: Path):
    from market_replay.collectors.historical import run_collection

    fake = FakeAllVenues()
    cfg = {"rpc_url_env": "X", "chain": "base", "protocol": "all", "period_start_utc": PERIOD_START, "period_end_utc": PERIOD_END, "discovery_window_start_utc": "2025-09-03T14:53:20Z", "prehistory_hours": 1, "venues": ["uniswap_v2", "uniswap_v4"], "venue_pairs": {"uniswap_v2": 2, "uniswap_v4": 2}, "selection_rule": "earliest_created_wrapped_native_pairs_v1", "selection_rule_cl": "earliest_created_wrapped_native_pairs_v1", "include_launches": True, "log_chunk_blocks": 3000, "max_requests": 6000, "out_dir": "packs/week_all", "authorization_note": "test"}
    p = tmp_path / "cfg.yaml"
    p.write_text(yaml.safe_dump(cfg))
    res = run_collection(p, tmp_path, transport=httpx.MockTransport(fake.handle), rpc_url_override="http://fake-rpc.local", sleep=lambda s: None)
    assert res["status"] == "pack_built", res
    assert "launches" in res["venues"] and res["venues"]["launches"]["pools"] >= 1
    pack = Pack.load(tmp_path / "packs" / "week_all")
    assert "all_launches_in_window_v1" in pack.manifest.universe.selection_rule_version
    assert pack.inventory["venues"]["all_launches_in_window_v1"]["pools"] >= 1
