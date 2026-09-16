"""Acceptance 10, 11, 12, 13 and the report-excerpt fixtures; validator behaviour."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from market_replay.datasets.generator import dev_short_config, generate_pack
from market_replay.datasets.importer import (
    ImportError_,
    classify_receipt,
    import_report_excerpts,
    iso_ms,
    normalize_candles,
    normalize_quote_only,
    normalize_security,
    normalize_trade_page,
)
from market_replay.datasets.pack import Pack, PackError
from market_replay.datasets.validator import validate_pack

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "reported_evidence" / "report_excerpts.json"


@pytest.fixture(scope="module")
def excerpts() -> dict:
    return json.loads(FIXTURE.read_text())


def test_fixture_is_labeled_report_excerpt(excerpts):
    assert excerpts["origin"] == "report_excerpt"
    assert excerpts["complete_raw_response"] is False
    assert "hash of the excerpt fixture" in excerpts["integrity_note"]


def test_http_success_with_application_error_is_failure(excerpts):
    ex = next(e for e in excerpts["examples"] if e["kind"] == "http_success_application_error")
    r = classify_receipt(ex["reported_http_status"], ex["response_fields_as_reported"])
    assert r["http_status"] == 200 and r["ok"] is False and "Rate Limit" in r["application_error"]
    assert classify_receipt(200, {"data": []})["ok"] is True
    assert classify_receipt(500, {"data": []})["ok"] is False


def test_empty_tax_fields_unknown_and_conflict_preserved(excerpts):
    ex = next(e for e in excerpts["examples"] if e["kind"] == "security_excerpt")
    obs = normalize_security(ex["response_fields_as_reported"], observed_utc_ms=iso_ms(ex["reported_received_at"]), asset_key="8453:x", market_evidence_pool_exists=True)
    assert obs.buy_tax_bps is None and obs.sell_tax_bps is None
    assert obs.is_honeypot is False and obs.is_in_dex is False
    assert obs.conflicts and "is_in_dex=0" in obs.conflicts[0]
    assert obs.available_utc_ms is None  # no historical as-of availability
    assert obs.raw_fields["buy_tax"] == ""


def test_trade_page_cannot_carry_fabricated_completeness(excerpts):
    ex = next(e for e in excerpts["examples"] if e["kind"] == "recent_trade_excerpt")
    rows = [ex["trade_fields_as_reported"]]
    with pytest.raises(ImportError_):
        normalize_trade_page(rows, requested_limit=None, claimed_complete=True)
    norm = normalize_trade_page(rows, requested_limit=None)
    assert norm["coverage_state"] == "partial" and norm["completeness"] == "not_established"


def test_open_candle_and_missing_minute(excerpts):
    ex = next(e for e in excerpts["examples"] if e["kind"] == "open_candle_excerpt")
    received = iso_ms(ex["reported_received_at"])
    norm = normalize_candles([ex["first_row_as_reported"]], interval_s=60, received_utc_ms=received, expected_buckets=[iso_ms(ex["reported_missing_bucket"]) // 1000])
    bar = norm["bars"][0]
    assert bar["start_utc_ms"] == 1789541100 * 1000 and bar["open"] is True and bar["closed"] is False
    assert norm["gaps"][0]["zero_filled"] is False


def test_quote_only_is_not_a_fill(excerpts):
    ex = next(e for e in excerpts["examples"] if e["kind"] == "quote_only_cli_excerpt")
    q = normalize_quote_only(ex)
    assert q["is_fill"] is False and q["calibrates_fill_model"] is False and q["rounded"] is True


def test_snapshot_only_sample_cannot_claim_executable(tmp_path: Path):
    pack = import_report_excerpts(FIXTURE, tmp_path / "diag")
    assert pack.manifest.origin == "report_excerpt"
    assert pack.validation["resulting_qualification"] == "diagnostic_only"
    assert str(pack.manifest.execution.model) == "diagnostic_no_execution"
    assert all(not p.supported_by_cpmm for p in pack.pools.values())
    assert pack.tape == []
    # attempting to upgrade the manifest to an executable qualification is caught by the validator
    m = yaml.safe_load((tmp_path / "diag" / "manifest.yaml").read_text())
    m["validation"]["qualification"] = "research"
    (tmp_path / "diag" / "manifest.yaml").write_text(yaml.safe_dump(m))
    rep = validate_pack(Pack.load(tmp_path / "diag"))
    assert rep["resulting_qualification"] == "diagnostic_only"


def test_generated_pack_passes_all_gates(dev_pack: Pack):
    rep = validate_pack(dev_pack)
    assert not rep["executable_failure"]
    assert {g["gate"] for g in rep["gates"]} >= {"identity", "universe", "temporal_ordering", "trade_coverage", "execution_state", "mechanics", "valuation", "anonymization", "provenance_rights", "reproducibility"}
    assert rep["predictive_validity"] == "not_established"
    ident = next(g for g in rep["gates"] if g["gate"] == "identity")
    assert "MOON" in ident["symbol_collisions"]  # collisions allowed, never merged


def test_pack_immutability_hash_mismatch(tmp_path: Path):
    generate_pack(dev_short_config(), tmp_path / "p")
    with (tmp_path / "p" / "tape.jsonl").open("a") as f:
        f.write("\n")
    with pytest.raises(PackError):
        Pack.load(tmp_path / "p")


def test_tampered_tape_fails_reconciliation(tmp_path: Path):
    generate_pack(dev_short_config(), tmp_path / "p")
    rows = [json.loads(l) for l in (tmp_path / "p" / "tape.jsonl").read_text().splitlines()]
    swap = next(r for r in rows if r["kind"] == "swap")
    swap["amount_out_recorded"] = str(int(swap["amount_out_recorded"]) - 1)
    (tmp_path / "p" / "tape.jsonl").write_text("\n".join(json.dumps(r, sort_keys=True, separators=(",", ":")) for r in rows) + "\n")
    pack = Pack.load(tmp_path / "p", verify_hashes=False)
    rep = validate_pack(pack)
    ex = next(g for g in rep["gates"] if g["gate"] == "execution_state")
    assert ex["status"] == "failed" and ex["reconciliation"]["mismatch_count"] > 0
    assert rep["resulting_qualification"] == "rejected"
