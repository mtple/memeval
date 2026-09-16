"""Execution/timing profile parameters. Stored as a YAML file inside each pack and hashed into the manifest."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from ..domain.status import ExecutionModel


class CapacityProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str = "capacity_v1"
    # Per-swap input relative to the current input reserve of the private state.
    max_input_bps_of_reserve: int = 10
    # Absolute accumulated displacement of private reserves from the no-agent reference state.
    max_cumulative_displacement_bps: int = 50
    note: str = "Unvalidated modeling guardrails, not trading rules or fidelity guarantees."


class RateLimit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    simulated_requests_per_minute: int = 600


class Budgets(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_requests: int = 200_000
    max_decisions: int = 20_000
    max_page_size: int = 500
    max_response_items: int = 5_000
    wall_clock_deadline_s: int | None = None
    inference_spend_limit_usd: str = "0"


class ExecutionParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: ExecutionModel = ExecutionModel.CPMM_FIXED_FLOW_V1
    profile_name: str
    label: str
    block_interval_ms: int | None = None  # generated packs; historical packs carry a block table
    data_latency_ms: int = 100
    quote_latency_ms: int = 100
    submit_latency_ms: int = 500
    confirm_blocks: int = 1
    quote_ttl_ms: int = 5000
    availability_delay_ms: int = 1500
    gas_cost_raw: str = "0"  # in numeraire raw units per included transaction
    gas_basis: str = "artificial_sensitivity_profile"
    capacity: CapacityProfile = Field(default_factory=CapacityProfile)
    settlement_tail_blocks: int = 2
    reporting_grid_ms: int = 3_600_000
    candle_intervals_ms: list[int] = Field(default_factory=lambda: [60_000, 300_000, 3_600_000])
    rate_limit: RateLimit = Field(default_factory=RateLimit)
    budgets: Budgets = Field(default_factory=Budgets)
    valuation_policy: str = "liquidate_all_holdings_via_direct_pool_v1"
    is_measured: bool = False
    notes: list[str] = Field(default_factory=list)

    @property
    def gas_cost(self) -> int:
        return int(self.gas_cost_raw)

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.model_dump(mode="json"), sort_keys=True)

    @classmethod
    def from_yaml(cls, text: str) -> ExecutionParams:
        return cls.model_validate(yaml.safe_load(text))

    @classmethod
    def load(cls, path: Path) -> ExecutionParams:
        return cls.from_yaml(path.read_text())

    def content_hash(self) -> str:
        return hashlib.sha256(self.to_yaml().encode()).hexdigest()

    def public_latency_assumptions(self) -> dict[str, Any]:
        return {
            "profile_name": self.profile_name,
            "label": self.label,
            "block_interval_ms": self.block_interval_ms,
            "data_latency_ms": self.data_latency_ms,
            "quote_latency_ms": self.quote_latency_ms,
            "submit_latency_ms": self.submit_latency_ms,
            "confirm_blocks": self.confirm_blocks,
            "quote_ttl_ms": self.quote_ttl_ms,
            "availability_delay_ms": self.availability_delay_ms,
            "gas_cost_raw": self.gas_cost_raw,
            "gas_basis": self.gas_basis,
            "capacity_profile": self.capacity.model_dump(),
            "settlement_tail_blocks": self.settlement_tail_blocks,
            "is_measured": self.is_measured,
        }


def fixture_default_params(**overrides: Any) -> ExecutionParams:
    base: dict[str, Any] = {
        "profile_name": "fixture_default_v1",
        "label": "Artificial test settings for generated fixtures; not measured Base latencies.",
        "block_interval_ms": 2000,
        "data_latency_ms": 100,
        "quote_latency_ms": 100,
        "submit_latency_ms": 500,
        "confirm_blocks": 1,
        "quote_ttl_ms": 5000,
        "availability_delay_ms": 1500,
        "gas_cost_raw": "50",
        "capacity": {"max_input_bps_of_reserve": 100, "max_cumulative_displacement_bps": 500},
        "settlement_tail_blocks": 2,
        "reporting_grid_ms": 3_600_000,
        "notes": ["All values are fixture test settings."],
    }
    base.update(overrides)
    return ExecutionParams.model_validate(base)


def historical_research_params(**overrides: Any) -> ExecutionParams:
    base: dict[str, Any] = {
        "profile_name": "base_research_v1",
        "label": "Documented assumptions for Base research packs; block times come from recorded block headers.",
        "block_interval_ms": None,
        "data_latency_ms": 250,
        "quote_latency_ms": 250,
        "submit_latency_ms": 1000,
        "confirm_blocks": 1,
        "quote_ttl_ms": 10_000,
        "availability_delay_ms": 4000,
        "gas_cost_raw": "0",
        "gas_basis": "assumed_zero_until_recorded_fee_series_imported",
        "capacity": {"max_input_bps_of_reserve": 10, "max_cumulative_displacement_bps": 50},
        "settlement_tail_blocks": 2,
        "reporting_grid_ms": 3_600_000,
        "notes": [
            "Latency and availability values are assumptions, not measurements.",
            "Gas defaults to zero and is reported as assumed until a time-qualified fee series is imported.",
        ],
    }
    base.update(overrides)
    return ExecutionParams.model_validate(base)
