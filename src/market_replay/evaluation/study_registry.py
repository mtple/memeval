"""Prospective study registry (Section 18C).

A study freezes: agent versions considered, evaluator/pack versions, the comparison
timestamp, the intended outcome and later outcome attachments. Predictive validity
stays ``not_established`` until an independently documented study exists.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def study_fingerprint(record: dict[str, Any]) -> str:
    frozen = {k: record[k] for k in ("candidates", "evaluator_version", "pack_ids", "comparison_id", "intended_outcome") if k in record}
    return hashlib.sha256(json.dumps(frozen, sort_keys=True).encode()).hexdigest()[:24]


def new_study(
    *,
    candidates: list[str],
    evaluator_version: str,
    pack_ids: list[str],
    comparison_id: str | None,
    intended_outcome: str,
    prediction: str,
    registered_at_utc: str,
) -> dict[str, Any]:
    rec = {
        "candidates": sorted(candidates),
        "evaluator_version": evaluator_version,
        "pack_ids": sorted(pack_ids),
        "comparison_id": comparison_id,
        "intended_outcome": intended_outcome,
        "prediction": prediction,
        "registered_at_utc": registered_at_utc,
        "outcomes": [],
        "predictive_validity": "not_established",
    }
    rec["study_id"] = "study_" + study_fingerprint(rec)
    return rec


def attach_outcome(study: dict[str, Any], *, observed_at_utc: str, description: str, data: dict[str, Any]) -> dict[str, Any]:
    study = dict(study)
    outcomes = list(study.get("outcomes", []))
    outcomes.append({"observed_at_utc": observed_at_utc, "description": description, "data": data})
    study["outcomes"] = outcomes
    # Attaching an outcome never flips predictive validity automatically; that requires a documented analysis.
    study["predictive_validity"] = "not_established"
    return study
