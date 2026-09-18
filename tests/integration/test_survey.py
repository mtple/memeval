"""The week survey counts launches and swaps per venue against the fake chains and records nothing."""

from __future__ import annotations

from pathlib import Path

import httpx

from market_replay.collectors.survey import survey_week
from tests.integration.test_weeks_fakes import PERIOD_END, PERIOD_START, FakeAllVenues


def test_survey_counts_launches_and_swaps_without_writing_a_pack(tmp_path: Path):
    fake = FakeAllVenues()
    out = survey_week(rpc_url="http://fake-rpc.local", chain="base", period_start_utc=PERIOD_START, period_end_utc=PERIOD_END, work=tmp_path / "s", transport=httpx.MockTransport(fake.handle), log_chunk_blocks=3000, swap_chunk_blocks=3000)
    assert set(out["venues"]) == {"uniswap_v2", "uniswap_v3", "uniswap_v4"}
    v4 = out["venues"]["uniswap_v4"]
    assert v4["launches"] >= 1 and v4["launches_with_eth_leg"] >= 1 and "launched_pools_by_swap_count" in v4
    assert out["requests"] > 0 and (tmp_path / "s" / "survey.json").exists()
    assert not list((tmp_path / "s").glob("**/manifest.yaml"))
