"""Versioned resource and stress assumptions, independent of historical evidence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ResourceProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    profile_id: str = "pack_defaults_v1"
    timing: Literal["controlled", "runner_measured"] = "controlled"
    decision_latency_ms: int = Field(default=0, ge=0, le=60_000)
    max_requests: int = Field(default=200_000, ge=1, le=200_000)
    max_decisions: int = Field(default=20_000, ge=1, le=20_000)
    requests_per_minute: int = Field(default=600, ge=1, le=600)
    stress: Literal["none", "adverse_execution_v1"] = "none"

    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(self.model_dump(), sort_keys=True).encode()).hexdigest()

    def public(self) -> dict:
        return self.model_dump() | {
            "fingerprint": self.fingerprint(),
            "decision_latency_basis": "before each budgeted command"
            if self.timing == "controlled"
            else "runner client wall time between calls, including local scheduling; server command processing and module loading excluded",
            "stress_basis": "assumed scenario; twice the pack's submit delay, confirmation blocks and gas"
            if self.stress != "none"
            else "pack assumptions",
        }


PROFILES = {
    "pack_defaults_v1": ResourceProfile(),
    "controlled_v1": ResourceProfile(
        profile_id="controlled_v1",
        decision_latency_ms=500,
        max_requests=20_000,
        max_decisions=2_000,
        requests_per_minute=120,
    ),
    "deployment_v1": ResourceProfile(
        profile_id="deployment_v1",
        timing="runner_measured",
        max_requests=20_000,
        max_decisions=2_000,
        requests_per_minute=120,
    ),
    "adverse_execution_v1": ResourceProfile(
        profile_id="adverse_execution_v1",
        decision_latency_ms=500,
        max_requests=20_000,
        max_decisions=2_000,
        requests_per_minute=120,
        stress="adverse_execution_v1",
    ),
}


def effective_pack(pack, profile: ResourceProfile):
    """Apply a declared scenario to a private copy; source pack and its hashes stay unchanged."""
    if profile.stress == "none":
        return pack
    params = pack.params.model_copy(
        update={
            "submit_latency_ms": 2 * pack.params.submit_latency_ms,
            "confirm_blocks": 2 * pack.params.confirm_blocks,
            "gas_cost_raw": str(2 * pack.params.gas_cost),
            "gas_basis": "adverse_execution_scenario_twice_pack_gas",
        }
    )
    return replace(pack, params=params)
