"""Write an immutable pack directory: data objects, execution parameters, coverage, validation and manifest."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from ..domain.models import (
    DataObject,
    EpisodeManifest,
    ExecutionSection,
    PackData,
    Period,
    Rights,
    Universe,
    ValidationSection,
)
from ..domain.status import DataOrigin, PredictiveValidity, TokenBehavior, UseStatus
from .execution_params import ExecutionParams
from .pack import Pack, compute_pack_id, sha256_file, write_jsonl

WEEK_MS = 604_800_000
VALIDATOR_VERSION = "pack_validator_v1"


def utc_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_period(start_utc_ms: int, end_utc_ms: int, prehistory_start_utc_ms: int | None) -> Period:
    return Period(
        start_utc=utc_iso(start_utc_ms),
        end_utc=utc_iso(end_utc_ms),
        prehistory_start_utc=None if prehistory_start_utc_ms is None else utc_iso(prehistory_start_utc_ms),
        start_utc_ms=start_utc_ms,
        end_utc_ms=end_utc_ms,
        prehistory_start_utc_ms=prehistory_start_utc_ms,
        duration_ms=end_utc_ms - start_utc_ms,
        is_full_week=(end_utc_ms - start_utc_ms) == WEEK_MS,
    )


def build_pack(
    out_dir: Path,
    *,
    origin: DataOrigin,
    chain: str,
    chain_id: int,
    scope_label: str,
    title_private: str,
    period: Period,
    universe: Universe,
    assets: list[dict[str, Any]],
    pools: list[dict[str, Any]],
    tape: list[dict[str, Any]],
    params: ExecutionParams,
    coverage: dict[str, Any],
    numeraire: str,
    numeraire_alias: str,
    numeraire_decimals: int,
    token_behavior: TokenBehavior,
    availability_model: dict[str, Any],
    rights: Rights,
    qualification: UseStatus,
    blocks: list[dict[str, Any]] | None = None,
    restrictions: list[dict[str, Any]] | None = None,
    inventory: dict[str, Any] | None = None,
    generator: dict[str, Any] | None = None,
    provenance_notes: list[str] | None = None,
    decision_log: list[str] | None = None,
    validate: bool = True,
) -> Pack:
    out_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(out_dir / "assets.jsonl", assets)
    write_jsonl(out_dir / "pools.jsonl", pools)
    write_jsonl(out_dir / "tape.jsonl", tape)
    names = ["assets.jsonl", "pools.jsonl", "tape.jsonl"]
    if blocks:
        write_jsonl(out_dir / "blocks.jsonl", blocks)
        names.append("blocks.jsonl")
    if restrictions:
        write_jsonl(out_dir / "restrictions.jsonl", restrictions)
        names.append("restrictions.jsonl")
    if inventory is not None:
        (out_dir / "inventory.json").write_text(json.dumps(inventory, indent=2, sort_keys=True))
        names.append("inventory.json")
    (out_dir / "coverage.json").write_text(json.dumps(coverage, indent=2, sort_keys=True))
    names.append("coverage.json")
    (out_dir / "execution_params.yaml").write_text(params.to_yaml())
    objects = []
    hashes: dict[str, str] = {}
    for n in names:
        fp = out_dir / n
        h = sha256_file(fp)
        hashes[n] = h
        objects.append(DataObject(filename=n, schema_id=n.split(".")[0] + "_v1", size_bytes=fp.stat().st_size, sha256=h))
    params_hash = params.content_hash()
    pack_id = compute_pack_id(hashes, params_hash)
    manifest = EpisodeManifest(
        pack_id=pack_id,
        origin=origin,
        chain=chain,
        chain_id=chain_id,
        scope_label=scope_label,
        title_private=title_private,
        period=period,
        universe=universe,
        data=PackData(objects=objects, coverage_report="coverage.json", availability_model=availability_model, token_behavior_basis=token_behavior),
        execution=ExecutionSection(model=params.model, parameters_file="execution_params.yaml", parameters_hash=params_hash),
        rights=rights,
        validation=ValidationSection(
            report_path="validation.json",
            qualification=qualification,
            predictive_validity=PredictiveValidity.NOT_ESTABLISHED,
            validator_version=VALIDATOR_VERSION,
        ),
        numeraire=numeraire,
        numeraire_alias=numeraire_alias,
        numeraire_decimals=numeraire_decimals,
        generator=generator or {},
        provenance_notes=provenance_notes or [],
        decision_log=decision_log or [],
    )
    (out_dir / "manifest.yaml").write_text(yaml.safe_dump(manifest.model_dump(mode="json"), sort_keys=False))
    if not (out_dir / "validation.json").exists():
        (out_dir / "validation.json").write_text(json.dumps({"status": "not_run"}, indent=2))
    pack = Pack.load(out_dir)
    if validate:
        from .validator import validate_pack

        report = validate_pack(pack)
        (out_dir / "validation.json").write_text(json.dumps(report, indent=2, sort_keys=True))
        # Qualification may be downgraded by the validator; rewrite manifest if so.
        final_q = UseStatus(report["resulting_qualification"])
        if final_q != qualification:
            manifest.validation.qualification = final_q
            (out_dir / "manifest.yaml").write_text(yaml.safe_dump(manifest.model_dump(mode="json"), sort_keys=False))
        pack = Pack.load(out_dir)
    return pack
