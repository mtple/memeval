"""Importers and provider normalizers.

* Report-excerpt fixtures become a *diagnostic-only* pack: the sample carries no
  executable state, so no execution model is attached and no fill is ever invented.
* Trade pages are never imported with a fabricated completeness watermark.
* Candle responses keep the still-open bucket marked open and leave missing buckets
  missing (never zero-filled).
* Security responses keep empty fields unknown and preserve conflicts.
* HTTP success with an application-level error stays a failure.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..domain.models import RestrictionObservation, Rights, Universe
from ..domain.status import CoverageState, DataOrigin, ExecutionModel, PoolModel, TokenBehavior, UseStatus
from .builder import make_period
from .execution_params import ExecutionParams
from .pack import Pack


class ImportError_(ValueError):
    pass


def iso_ms(s: str) -> int:
    s = s.replace("Z", "+00:00")
    return int(datetime.fromisoformat(s).timestamp() * 1000)


# ---------------------------------------------------------------------- normalizers
def normalize_trade_page(rows: list[dict[str, Any]], *, requested_limit: int | None, claimed_complete: bool = False, received_utc_ms: int | None = None) -> dict[str, Any]:
    """A trade page is an input sample. Its interval completeness is partial unless independently checked."""
    if claimed_complete:
        raise ImportError_("a selected trade page cannot be imported with a fabricated completeness watermark")
    times = [iso_ms(r["block_timestamp"]) for r in rows if r.get("block_timestamp")]
    state = CoverageState.PARTIAL if rows else CoverageState.MISSING
    return {
        "rows": len(rows),
        "requested_limit": requested_limit,
        "span_utc_ms": [min(times), max(times)] if times else None,
        "coverage_state": str(state),
        "evidence": "single provider page; pagination exhaustion and provider indexing completeness not established",
        "received_utc_ms": received_utc_ms,
        "completeness": "not_established",
    }


def normalize_candles(rows: list[list[float]], *, interval_s: int, received_utc_ms: int, expected_buckets: list[int] | None = None) -> dict[str, Any]:
    """Rows: [bucket_start_s, o, h, l, c, v]. Marks the open bucket; reports missing buckets as gaps."""
    out = []
    starts = set()
    for r in rows:
        start_ms = int(r[0]) * 1000
        end_ms = start_ms + interval_s * 1000
        is_open = end_ms > received_utc_ms
        starts.add(start_ms)
        out.append({"start_utc_ms": start_ms, "end_utc_ms": end_ms, "open": is_open, "closed": not is_open, "completeness": "partial" if is_open else "unknown", "values_as_reported": [str(x) for x in r[1:]]})
    gaps = []
    if expected_buckets:
        for b in expected_buckets:
            if b * 1000 not in starts:
                gaps.append({"start_utc_ms": b * 1000, "reason": "bucket_absent_in_response", "zero_filled": False, "interpretation": "unknown: provider may skip no-swap intervals but this is not established for this gap"})
    return {"bars": out, "gaps": gaps, "received_utc_ms": received_utc_ms, "note": "open bar is never a finalized earlier candle; absent buckets are not zero volume"}


def normalize_security(fields: dict[str, Any], *, observed_utc_ms: int, asset_key: str, market_evidence_pool_exists: bool | None = None) -> RestrictionObservation:
    def flag(v: Any) -> bool | None:
        if v in ("", None):
            return None
        return str(v) == "1" or v is True

    def tax(v: Any) -> int | None:
        if v in ("", None):
            return None
        try:
            return int(round(float(v) * 10_000))
        except ValueError:
            return None

    conflicts = []
    in_dex = flag(fields.get("is_in_dex"))
    if in_dex is False and market_evidence_pool_exists:
        conflicts.append("provider reports is_in_dex=0 while a market provider observed a pool for this token")
    return RestrictionObservation(
        asset=asset_key,
        observed_utc_ms=observed_utc_ms,
        available_utc_ms=None,  # no historical as-of service: unknown availability for replay
        source="goplus_excerpt",
        is_honeypot=flag(fields.get("is_honeypot")),
        buy_tax_bps=tax(fields.get("buy_tax")),
        sell_tax_bps=tax(fields.get("sell_tax")),
        is_in_dex=in_dex,
        sell_blocked=None,
        raw_fields={k: v for k, v in fields.items()},
        conflicts=conflicts,
    )


def classify_receipt(http_status: int | None, body: dict[str, Any] | None) -> dict[str, Any]:
    """HTTP success plus provider application failure stays a failure."""
    app_error = None
    if isinstance(body, dict):
        st = body.get("status")
        if isinstance(st, dict) and st.get("error_code"):
            app_error = f"{st.get('error_code')}: {st.get('error_message')}"
        if body.get("error"):
            app_error = str(body["error"])
        if body.get("code") not in (None, 1, 0, "1", "0") and body.get("message") not in (None, "OK", "ok"):
            app_error = f"{body.get('code')}: {body.get('message')}"
    ok = http_status is not None and 200 <= http_status < 300 and app_error is None
    return {"http_status": http_status, "application_error": app_error, "ok": ok}


def normalize_quote_only(excerpt: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "quote_evidence",
        "execution_evidence": excerpt.get("execution_evidence", "quote_only"),
        "is_fill": False,
        "rounded": True,
        "lines": excerpt.get("output_lines_as_reported", []),
        "invocation_utc_ms": iso_ms(excerpt["reported_invocation_at"]) if excerpt.get("reported_invocation_at") else None,
        "calibrates_fill_model": False,
        "note": "A rounded quote-only text is not a calibrated fill model and does not establish costs, routing, execution or sellability.",
    }


# ---------------------------------------------------------------------- diagnostic pack from excerpts
def import_report_excerpts(fixture_path: Path, out_dir: Path) -> Pack:
    raw = fixture_path.read_bytes()
    data = json.loads(raw)
    if data.get("origin") != "report_excerpt":
        raise ImportError_("fixture origin must be report_excerpt")
    excerpt_hash = hashlib.sha256(raw).hexdigest()
    examples = {e["kind"]: e for e in data["examples"]}
    trade = examples["recent_trade_excerpt"]
    candle = examples["open_candle_excerpt"]
    security = examples["security_excerpt"]
    quote = examples["quote_only_cli_excerpt"]
    chain_id = 8453
    pool_addr = trade["pool_address"]
    # Token identity is not supplied by the excerpt: keep a placeholder canonical key derived from the pool, flagged unknown.
    base_asset_key = f"{chain_id}:unknown_token_of_{pool_addr}"
    weth_key = f"{chain_id}:0x4200000000000000000000000000000000000006"
    t_first = iso_ms(trade["reported_span"][0])
    t_last = iso_ms(trade["reported_span"][1])
    received = iso_ms(trade["reported_received_at"])
    start = (t_first // 60_000) * 60_000
    end = ((t_last // 60_000) + 1) * 60_000
    assets = [
        {"key": weth_key, "chain_id": chain_id, "address": "0x4200000000000000000000000000000000000006", "decimals": 18, "symbol": "WETH", "name": "Wrapped Ether (Base) - decimals assumed from public knowledge, not from the excerpt", "is_numeraire": True, "created_block": None, "created_time_utc_ms": None, "discovery_available_utc_ms": start, "fixture_rules": {}},
        {"key": base_asset_key, "chain_id": chain_id, "address": f"unknown_token_of_{pool_addr}", "decimals": 18, "symbol": None, "name": None, "is_numeraire": False, "created_block": None, "created_time_utc_ms": None, "discovery_available_utc_ms": start, "fixture_rules": {"decimals_basis": "unknown; 18 assumed only for schema completeness"}},
    ]
    pools = [
        {
            "key": f"{chain_id}:unknown_protocol:{pool_addr}",
            "chain_id": chain_id,
            "address": pool_addr,
            "protocol": "unknown_protocol",
            "model": str(PoolModel.UNKNOWN),
            "asset0": weth_key,
            "asset1": base_asset_key,
            "fee_numerator": 997,
            "fee_denominator": 1000,
            "created_block": None,
            "created_time_utc_ms": None,
            "discovery_available_utc_ms": start,
            "initial_reserve0": None,
            "initial_reserve1": None,
            "initial_state_block": None,
            "initial_state_basis": None,
            "factory": None,
            "supported_by_cpmm": False,
            "unsupported_reason": "snapshot-only excerpt: protocol, reserves and token amounts are not supplied",
        }
    ]
    # One observation row exists (a single trade with USD-denominated fields); raw amounts are unknown, so no swap event is created.
    tape: list[dict[str, Any]] = []
    trade_page = normalize_trade_page([trade["trade_fields_as_reported"]], requested_limit=None, received_utc_ms=received)
    candles = normalize_candles([candle["first_row_as_reported"]], interval_s=60 * candle["aggregate_minutes"], received_utc_ms=iso_ms(candle["reported_received_at"]), expected_buckets=[iso_ms(candle["reported_missing_bucket"]) // 1000])
    sec = normalize_security(security["response_fields_as_reported"], observed_utc_ms=iso_ms(security["reported_received_at"]), asset_key=base_asset_key, market_evidence_pool_exists=True)
    coverage = {
        "schema": "coverage_v1",
        "basis": "report_excerpt",
        "intervals": [
            {"object_ref": pools[0]["key"], "field": "swaps", "start_utc_ms": start, "end_utc_ms": end, "state": trade_page["coverage_state"], "evidence": trade_page["evidence"], "gaps": []},
            {"object_ref": pools[0]["key"], "field": "candles", "start_utc_ms": iso_ms(candle["reported_missing_bucket"]), "end_utc_ms": iso_ms(candle["reported_missing_bucket"]) + 60_000, "state": "missing", "evidence": "bucket absent in response; cause not established", "gaps": candles["gaps"]},
        ],
        "diagnostics": {"trade_page": trade_page, "candles": candles, "quote_only": normalize_quote_only(quote), "receipt_rule_example": classify_receipt(200, examples.get("http_success_application_error", {}).get("response_fields_as_reported"))},
        "excerpt_fixture_sha256": excerpt_hash,
        "excerpt_hash_basis": "hash of the excerpt fixture file, not of any provider receipt",
    }
    params = ExecutionParams(
        model=ExecutionModel.DIAGNOSTIC_NO_EXECUTION,
        profile_name="diagnostic_no_execution",
        label="No execution model: snapshot-only sample.",
        block_interval_ms=2000,
        gas_cost_raw="0",
        gas_basis="not_applicable",
        reporting_grid_ms=60_000,
        notes=["Diagnostic import; no fills can be simulated."],
    )
    from .builder import build_pack

    return build_pack(
        out_dir,
        origin=DataOrigin.REPORT_EXCERPT,
        chain="base",
        chain_id=chain_id,
        scope_label="report_excerpt_diagnostic_v1",
        title_private="Report excerpt diagnostic sample (2026-09-16 evidence report)",
        period=make_period(start, end, None),
        universe=Universe(factories=[], pool_models=[PoolModel.UNKNOWN], quote_asset=weth_key, selection_rule_version="single_reported_pool_excerpt", candidate_count=1, selected_count=0, unsupported_count=1, missing_count=0, description="One pool reported in the evidence excerpt; not a universe."),
        assets=assets,
        pools=pools,
        tape=tape,
        params=params,
        coverage=coverage,
        numeraire=weth_key,
        numeraire_alias="NATIVE",
        numeraire_decimals=18,
        token_behavior=TokenBehavior.UNKNOWN,
        availability_model={"kind": "unknown", "note": "acquisition time recorded; original availability not established"},
        rights=Rights(storage_basis="report_excerpt_supplied_by_owner", local_processing_basis="report_excerpt_supplied_by_owner", redistribution="not_cleared", simulator_serving="diagnostic_only"),
        qualification=UseStatus.DIAGNOSTIC_ONLY,
        restrictions=[sec.model_dump(mode="json")],
        inventory={"unsupported": [{"pool": pools[0]["key"], "reason": pools[0]["unsupported_reason"]}], "missing": [], "candidate_count": 1, "selected_count": 0},
        provenance_notes=["REPORT EXCERPT. Not a raw receipt. Completeness not established. No execution model attached."],
        decision_log=["Snapshot-only sample imported as diagnostic data; fills are not invented."],
    )


def utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
