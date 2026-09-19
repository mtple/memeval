"""Suite definitions: a frozen list of packs plus bankroll, mode and mask schedule."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field


class SuiteDef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    suite_id: str
    description: str
    packs: list[str]  # pack names (as imported) or pack ids
    bankroll_raw: str = "1000000"
    mode: str = "practice"  # practice | sealed
    mask_seed: str
    engine_seed: str = "engine-v1"
    isolation: str = "trusted_external_client"
    sealed: bool = False
    notes: list[str] = Field(default_factory=list)

    def fingerprint(self, pack_ids: list[str]) -> str:
        payload = {"suite_id": self.suite_id, "packs": sorted(pack_ids), "bankroll_raw": self.bankroll_raw, "mode": self.mode, "mask_seed": self.mask_seed, "engine_seed": self.engine_seed}
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24]


def load_suites(path: Path) -> dict[str, SuiteDef]:
    data = yaml.safe_load(path.read_text()) or {}
    out: dict[str, SuiteDef] = {}
    for sid, body in (data.get("suites") or {}).items():
        out[sid] = SuiteDef.model_validate({"suite_id": sid, **body})
    return out


def default_suites_text() -> str:
    return yaml.safe_dump(
        {
            "suites": {
                "generated-practice-v1": {
                    "description": "Four full generated weeks with different artificial conditions. Dates and trajectories may be revealed after completion.",
                    "packs": ["gen_week_trending", "gen_week_reversal", "gen_week_sparse_missing", "gen_week_liquidity_shift"],
                    "bankroll_raw": "1000000",
                    "mode": "practice",
                    "mask_seed": "suite-generated-practice-v1-mask",
                    "engine_seed": "suite-generated-practice-v1-engine",
                    "notes": ["Generated fixtures only. Bankroll 1.0 CASH is a software default, not a recommendation."],
                },
                "generated-sealed-v1": {
                    "description": "Same four generated weeks in sealed mode: no real dates, no post-run trajectory disclosure to the participant.",
                    "packs": ["gen_week_trending", "gen_week_reversal", "gen_week_sparse_missing", "gen_week_liquidity_shift"],
                    "bankroll_raw": "1000000",
                    "mode": "sealed",
                    "sealed": True,
                    "mask_seed": "suite-generated-sealed-v1-mask",
                    "engine_seed": "suite-generated-sealed-v1-engine",
                },
                "generated-dev-v1": {
                    "description": "Two-hour development fixture for quick integration checks.",
                    "packs": ["gen_dev_short"],
                    "bankroll_raw": "1000000",
                    "mode": "practice",
                    "mask_seed": "suite-generated-dev-v1-mask",
                    "engine_seed": "suite-generated-dev-v1-engine",
                },
            }
        },
        sort_keys=False,
    )


def suite_public(s: SuiteDef, pack_ids: list[str]) -> dict[str, Any]:
    return {
        "suite_id": s.suite_id,
        "description": s.description,
        "packs": s.packs,
        "pack_count": len(s.packs),
        "resolved_pack_ids": pack_ids,
        "bankroll_raw": s.bankroll_raw,
        "mode": s.mode,
        "sealed": s.sealed,
        "isolation": s.isolation,
        "fingerprint": s.fingerprint(pack_ids),
        "notes": s.notes,
    }
