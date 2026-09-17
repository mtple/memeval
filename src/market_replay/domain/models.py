"""Canonical normalized objects (private side). Quantities are decimal strings on the wire."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .quantities import parse_raw
from .status import (
    AvailabilityBasis,
    Completeness,
    CoverageState,
    DataOrigin,
    ExecutionModel,
    Isolation,
    PoolModel,
    PredictiveValidity,
    TokenBehavior,
    UseStatus,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _validate_raw_str(v: Any) -> str:
    if isinstance(v, int) and not isinstance(v, bool):
        return str(v)
    return str(parse_raw(v))


class Asset(StrictModel):
    key: str  # canonical "{chain_id}:{address}"
    chain_id: int
    address: str
    decimals: int
    symbol: str | None = None  # metadata only
    name: str | None = None
    created_block: int | None = None
    created_time_utc_ms: int | None = None
    discovery_available_utc_ms: int | None = None
    is_numeraire: bool = False
    fixture_rules: dict[str, Any] = Field(default_factory=dict)


class Pool(StrictModel):
    key: str  # canonical "{chain_id}:{protocol}:{address}"
    chain_id: int
    protocol: str
    address: str
    model: PoolModel
    asset0: str
    asset1: str
    fee_numerator: int = 997
    fee_denominator: int = 1000
    created_block: int | None = None
    created_time_utc_ms: int | None = None
    discovery_available_utc_ms: int | None = None
    initial_reserve0: str | None = None
    initial_reserve1: str | None = None
    initial_state_block: int | None = None
    initial_state_basis: str | None = None  # e.g. "last_sync_before_period", "fixture_construction"
    factory: str | None = None
    supported_by_cpmm: bool = True
    unsupported_reason: str | None = None
    # concentrated liquidity (uniswap_v3_cl / uniswap_v4_cl)
    fee_pips: int | None = None
    fee_basis: str | None = None  # e.g. "pool_fee_from_initialize", "dynamic_hook_fee_last_observed_before_window"
    tick_spacing: int | None = None
    hooks: str | None = None
    initial_sqrt_price_x96: str | None = None
    initial_tick: int | None = None
    initial_liquidity: str | None = None
    initial_ticks: list[list[str]] = Field(default_factory=list)  # [tick, liquidity_net, liquidity_gross]
    supported_by_clmm: bool = False

    @field_validator("initial_reserve0", "initial_reserve1", "initial_sqrt_price_x96", "initial_liquidity", mode="before")
    @classmethod
    def _raw(cls, v: Any) -> Any:
        return None if v is None else _validate_raw_str(v)


TapeKind = Literal["swap", "mint", "burn", "sync", "adjust", "restriction", "halt", "unhalt", "discovery", "cl_init", "cl_modify", "cl_swap"]


class TapeEvent(StrictModel):
    """One ordered external primitive action on the tape (private side, absolute times)."""

    seq: int
    block: int
    log_index: int
    time_utc_ms: int
    kind: TapeKind
    pool: str
    tx: str | None = None
    wallet: str | None = None
    # swap
    asset_in: str | None = None
    amount_in: str | None = None
    amount_out_recorded: str | None = None
    # mint/burn (non-negative); adjust (signed net reserve deltas of an event the model has no primitive for)
    amount0: str | None = None
    amount1: str | None = None
    # sync checkpoint
    reserve0: str | None = None
    reserve1: str | None = None
    # concentrated liquidity: cl_init / cl_modify / cl_swap (signed integers as decimal strings)
    sqrt_price_x96: str | None = None
    tick: int | None = None
    tick_lower: int | None = None
    tick_upper: int | None = None
    liquidity_delta: str | None = None
    sqrt_price_x96_after: str | None = None
    liquidity_after: str | None = None
    tick_after: int | None = None
    fee_pips: int | None = None
    # restriction / halt
    payload: dict[str, Any] = Field(default_factory=dict)
    publication_utc_ms: int | None = None
    received_utc_ms: int | None = None
    available_utc_ms: int | None = None  # None -> never published to agents (observation dropout)
    availability_basis: AvailabilityBasis = AvailabilityBasis.UNKNOWN

    @field_validator("amount_in", "amount_out_recorded", "amount0", "amount1", "reserve0", "reserve1", "sqrt_price_x96", "liquidity_delta", "sqrt_price_x96_after", "liquidity_after", mode="before")
    @classmethod
    def _raw(cls, v: Any) -> Any:
        return None if v is None else _validate_raw_str(v)


class CoverageInterval(StrictModel):
    object_ref: str  # pool key or "universe"
    field: str  # e.g. "swaps", "liquidity", "discovery", "restrictions"
    start_utc_ms: int
    end_utc_ms: int
    state: CoverageState
    evidence: str
    checked_scope: str | None = None
    gaps: list[dict[str, Any]] = Field(default_factory=list)


class RestrictionObservation(StrictModel):
    asset: str
    observed_utc_ms: int
    available_utc_ms: int | None
    source: str
    is_honeypot: bool | None = None
    buy_tax_bps: int | None = None
    sell_tax_bps: int | None = None
    is_in_dex: bool | None = None
    sell_blocked: bool | None = None
    raw_fields: dict[str, Any] = Field(default_factory=dict)
    conflicts: list[str] = Field(default_factory=list)


class RawReceipt(StrictModel):
    receipt_id: str
    provider: str
    request: dict[str, Any]  # credentials removed
    http_status: int | None
    application_error: str | None
    body_sha256: str | None
    requested_utc_ms: int | None
    received_utc_ms: int | None
    body_path: str | None = None
    origin: DataOrigin = DataOrigin.CAPTURED_OBSERVATIONS
    integrity_hash_basis: str = "sha256_of_stored_bytes"


class Period(StrictModel):
    start_utc: str
    end_utc: str
    prehistory_start_utc: str | None
    start_utc_ms: int
    end_utc_ms: int
    prehistory_start_utc_ms: int | None
    duration_ms: int
    is_full_week: bool


class Universe(StrictModel):
    factories: list[str] = Field(default_factory=list)
    pool_models: list[PoolModel] = Field(default_factory=list)
    quote_asset: str
    selection_rule_version: str
    indexed_block_ranges: list[list[int]] = Field(default_factory=list)
    excluded_or_unsupported_counts: dict[str, int] = Field(default_factory=dict)
    candidate_count: int = 0
    selected_count: int = 0
    unsupported_count: int = 0
    missing_count: int = 0
    description: str = ""


class DataObject(StrictModel):
    filename: str
    schema_id: str
    size_bytes: int
    sha256: str


class PackData(StrictModel):
    objects: list[DataObject]
    coverage_report: str
    availability_model: dict[str, Any]
    token_behavior_basis: TokenBehavior


class ExecutionSection(StrictModel):
    model: ExecutionModel
    parameters_file: str
    parameters_hash: str


class Rights(StrictModel):
    storage_basis: str = "unverified"
    local_processing_basis: str = "unverified"
    redistribution: str = "not_cleared"
    simulator_serving: str = "local_only"
    notes: str = ""


class ValidationSection(StrictModel):
    report_path: str
    qualification: UseStatus
    predictive_validity: PredictiveValidity = PredictiveValidity.NOT_ESTABLISHED
    validator_version: str


class EpisodeManifest(StrictModel):
    schema_version: int = 1
    pack_id: str
    origin: DataOrigin
    chain: str
    chain_id: int
    scope_label: str
    title_private: str
    period: Period
    universe: Universe
    data: PackData
    execution: ExecutionSection
    rights: Rights
    validation: ValidationSection
    numeraire: str  # canonical asset key
    numeraire_alias: str
    numeraire_decimals: int
    generator: dict[str, Any] = Field(default_factory=dict)
    provenance_notes: list[str] = Field(default_factory=list)
    decision_log: list[str] = Field(default_factory=list)


class PublicDescriptor(StrictModel):
    """Allowlisted view of a pack for agents and sealed-suite participants."""

    episode_id: str  # public, non content-addressed id
    origin: DataOrigin
    chain: str
    duration_ms: int
    is_full_week: bool
    prehistory_ms: int
    numeraire_alias: str
    numeraire_decimals: int
    execution_model: ExecutionModel
    token_behavior_basis: TokenBehavior
    availability_basis: AvailabilityBasis
    latency_assumptions: dict[str, Any]
    capabilities: list[str]
    unsupported_capabilities: list[str]
    limitations: list[str]
    use_status: UseStatus
    predictive_validity: PredictiveValidity = PredictiveValidity.NOT_ESTABLISHED
    isolation: Isolation | None = None


class ObservationQuality(StrictModel):
    completeness: Completeness
    availability_basis: AvailabilityBasis
    observed_through_ms: int
    warnings: list[str] = Field(default_factory=list)
