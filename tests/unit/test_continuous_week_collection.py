from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml

from market_replay.collectors.base import BudgetExhausted
from market_replay.datasets.generator import GeneratorConfig, generate_pack
from market_replay.datasets.pack import Pack, compute_pack_id, sha256_file, write_jsonl


@pytest.fixture
def helper():
    path = Path(__file__).resolve().parents[2] / "scripts" / "collect_continuous_week.py"
    spec = importlib.util.spec_from_file_location("continuous_week_collection", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_budget_survives_transport_failure_and_restart(helper, tmp_path):
    calls = []

    def send(request):
        calls.append(request)
        if len(calls) == 2:
            raise httpx.ReadTimeout("response lost")
        return httpx.Response(200, content=b"{}")

    path = tmp_path / "rpc-budget.json"
    first = helper.CumulativeTransport(path, max_requests=2, max_response_bytes=100, inner=httpx.MockTransport(send))
    with httpx.Client(transport=first) as client:
        assert client.post("https://rpc.invalid").status_code == 200
        with pytest.raises(httpx.ReadTimeout):
            client.post("https://rpc.invalid")
    second = helper.CumulativeTransport(path, max_requests=2, max_response_bytes=100, inner=httpx.MockTransport(send))
    with httpx.Client(transport=second) as client:
        with pytest.raises(BudgetExhausted, match="cumulative RPC"):
            client.post("https://rpc.invalid")
    assert len(calls) == 2
    assert json.loads(path.read_text())["requests"] == 2
    with pytest.raises(helper.CollectionError, match="saved collection budget differs"):
        helper.CumulativeTransport(path, max_requests=3, max_response_bytes=100, inner=httpx.MockTransport(send))


def test_response_bytes_enforce_cumulative_cap(helper, tmp_path):
    calls = []

    def send(request):
        calls.append(request)
        return httpx.Response(200, content=b"12345")

    transport = helper.CumulativeTransport(tmp_path / "budget.json", max_requests=10, max_response_bytes=4, inner=httpx.MockTransport(send))
    with httpx.Client(transport=transport) as client:
        with pytest.raises(BudgetExhausted, match="crossed"):
            client.post("https://rpc.invalid")
        with pytest.raises(BudgetExhausted, match="byte cap reached"):
            client.post("https://rpc.invalid")
    assert len(calls) == 1
    assert transport.state["response_bytes"] == 5


def test_week_config_preserves_the_full_universe(helper, tmp_path):
    config = helper.collection_config("2026-09-07", "2026-09-14", tmp_path, max_requests=40000, max_response_bytes=16 * 1024**3)
    assert config["min_swaps"] == 1
    assert config["include_launches"] is True
    assert config["venues"] == ["uniswap_v2", "uniswap_v3", "uniswap_v4"]
    assert config["venue_pairs"] == {"uniswap_v2": 4, "uniswap_v3": 4, "uniswap_v4": 8}
    assert config["prehistory_hours"] == 24
    with pytest.raises(helper.CollectionError, match="seven UTC days"):
        helper.collection_config("2026-09-07", "2026-09-13", tmp_path, max_requests=40000, max_response_bytes=100)


def test_utc_boundaries_require_adjacent_headers(helper):
    blocks = {"period_start": 100, "period_end": 200, "period_start_ts": 1001000}
    headers = {"start_before": 999000, "start_at_or_after": 1001000, "end_before": 1199000, "end_at_or_after": 1201000}
    helper.verify_boundary_headers(1000000, 1200000, blocks, headers)
    with pytest.raises(helper.CollectionError, match="bracket"):
        helper.verify_boundary_headers(1000000, 1200000, blocks, {**headers, "start_before": 1000000})
    with pytest.raises(helper.CollectionError, match="recorded interval"):
        helper.verify_boundary_headers(1000000, 1200000, {**blocks, "period_end": 201}, headers)


def test_exact_week_has_distinct_identity_and_preserves_source_bytes(helper, tmp_path):
    cfg = GeneratorConfig(name="boundaries", seed="boundaries", n_pools=2, duration_ms=60000, prehistory_ms=2000, scaled_activity=100)
    cfg.params.block_interval_ms = 2000
    original = generate_pack(cfg, tmp_path / "source")
    start_ms = original.manifest.period.start_utc_ms - 1000
    end_ms = original.manifest.period.end_utc_ms - 1000
    rows = [row for row in original.iter_tape() if row["time_utc_ms"] < end_ms]
    # Make a tiny collector-style fixture whose tape stops at the exclusive end.
    write_jsonl(original.tape_file, rows)
    for obj in original.manifest.data.objects:
        if obj.filename == original.tape_file.name:
            obj.sha256 = sha256_file(original.tape_file)
            obj.size_bytes = original.tape_file.stat().st_size
    original.manifest.pack_id = compute_pack_id({obj.filename: obj.sha256 for obj in original.manifest.data.objects}, original.manifest.execution.parameters_hash)
    (original.path / "manifest.yaml").write_text(yaml.safe_dump(original.manifest.model_dump(mode="json")))
    original = Pack.load(original.path)
    start_block = rows[0]["block"] + (original.manifest.period.start_utc_ms - rows[0]["time_utc_ms"]) // 2000
    proof = {
        "start_utc_ms": start_ms, "end_utc_ms": end_ms,
        "blocks": {"period_start": start_block, "period_start_ts": start_ms + 1000, "period_end": start_block + 30},
        "header_times_utc_ms": {"start_before": start_ms - 1000, "start_at_or_after": start_ms + 1000, "end_before": end_ms - 1000, "end_at_or_after": end_ms + 1000},
    }
    before = {path.name: sha256_file(path) for path in original.path.iterdir() if path.is_file()}
    result = helper.prepare_exact_week(original.path, tmp_path / "ready", proof)
    assert result.manifest.period.start_utc_ms == start_ms
    assert result.manifest.period.end_utc_ms == end_ms
    assert result.pack_id != original.pack_id
    assert sha256_file(result.tape_file) == sha256_file(original.tape_file)
    assert any(obj.filename == "utc-boundaries.json" for obj in result.manifest.data.objects)
    assert before == {path.name: sha256_file(path) for path in original.path.iterdir() if path.is_file()}
    assert helper.profile_pack(result)["events"] == len(rows)
    profile = {"dataset": helper.profile_pack(result)}
    helper.index_and_publish(result, tmp_path, profile, SimpleNamespace(publish=False, storage_budget_bytes=750_000_000))
    index = json.loads((tmp_path / "indexed" / result.pack_id / "index.json").read_text())
    assert index["total"] == len(rows)
    assert "utc-boundaries.json" in index["metadata"]
    assert profile["status"] == "indexed"
    assert not (tmp_path / "catalog-candidate.json").exists()
    with pytest.raises(helper.CollectionError, match="storage budget"):
        helper.index_and_publish(result, tmp_path, profile, SimpleNamespace(publish=True, storage_budget_bytes=1))
