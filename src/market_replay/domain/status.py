"""Status vocabularies. Each dimension is reported independently; there is no single 'verified' badge."""

from __future__ import annotations

from enum import StrEnum


class DataOrigin(StrEnum):
    GENERATED_FIXTURE = "generated_fixture"
    CAPTURED_OBSERVATIONS = "captured_observations"
    HISTORICAL_RECONSTRUCTION = "historical_reconstruction"
    REPORT_EXCERPT = "report_excerpt"
    MIXED = "mixed"


class AvailabilityBasis(StrEnum):
    RECORDED_RECEIPT = "recorded_receipt"
    RECONSTRUCTED_WITH_DELAY_MODEL = "reconstructed_with_delay_model"
    FIXTURE_DELAY_MODEL = "fixture_delay_model"
    UNKNOWN = "unknown"


class ExecutionModel(StrEnum):
    CPMM_FIXED_FLOW_V1 = "cpmm_fixed_flow_v1"
    DIAGNOSTIC_NO_EXECUTION = "diagnostic_no_execution"


class TokenBehavior(StrEnum):
    KNOWN_FIXTURE_RULES = "known_fixture_rules"
    HISTORICALLY_SUPPORTED = "historically_supported"
    ASSUMED_STANDARD_TRANSFER = "assumed_standard_transfer"
    UNKNOWN = "unknown"


class Isolation(StrEnum):
    TRUSTED_EXTERNAL_CLIENT = "trusted_external_client"
    RESTRICTED_LOCAL_RUNNER = "restricted_local_runner"


class UseStatus(StrEnum):
    DEMO = "demo"
    RESEARCH = "research"
    QUALIFIED_FOR_NAMED_SUITE = "qualified_for_named_suite"
    DIAGNOSTIC_ONLY = "diagnostic_only"
    REJECTED = "rejected"


class PredictiveValidity(StrEnum):
    NOT_ESTABLISHED = "not_established"


class PoolModel(StrEnum):
    UNISWAP_V2_PLAIN = "uniswap_v2_plain"
    FIXTURE_CPMM = "fixture_cpmm"
    # Recognised but unsupported by the CPMM adapter. They exist so imports can keep the data.
    UNISWAP_V3 = "uniswap_v3"
    UNISWAP_V4_HOOKED = "uniswap_v4_hooked"
    BONDING_CURVE = "bonding_curve"
    UNKNOWN = "unknown"


SUPPORTED_CPMM_MODELS = frozenset({PoolModel.UNISWAP_V2_PLAIN, PoolModel.FIXTURE_CPMM})


class RunState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    AGENT_FAILED = "agent_failed"
    ENVIRONMENT_FAILED = "environment_failed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    ABORTED = "aborted"


TERMINAL_RUN_STATES = frozenset(
    {RunState.COMPLETED, RunState.AGENT_FAILED, RunState.ENVIRONMENT_FAILED, RunState.BUDGET_EXHAUSTED, RunState.ABORTED}
)


class OrderState(StrEnum):
    RECEIVED = "received"
    REJECTED = "rejected"
    ACCEPTED_AND_RESERVED = "accepted_and_reserved"
    PENDING_INCLUSION = "pending_inclusion"
    FILLED_PENDING_CONFIRMATION = "filled_pending_confirmation"
    CONFIRMED = "confirmed"
    REVERTED = "reverted"
    EXPIRED = "expired"
    MODEL_CAPACITY_REJECTED = "model_capacity_rejected"


TERMINAL_ORDER_STATES = frozenset(
    {
        OrderState.REJECTED,
        OrderState.CONFIRMED,
        OrderState.REVERTED,
        OrderState.EXPIRED,
        OrderState.MODEL_CAPACITY_REJECTED,
    }
)


class ErrorCode(StrEnum):
    NOT_YET_DISCOVERED = "NOT_YET_DISCOVERED"
    OUTSIDE_COVERAGE = "OUTSIDE_COVERAGE"
    UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"
    MISSING_DATA = "MISSING_DATA"
    STALE_DATA = "STALE_DATA"
    NO_ROUTE = "NO_ROUTE"
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    QUOTE_EXPIRED = "QUOTE_EXPIRED"
    SLIPPAGE_LIMIT = "SLIPPAGE_LIMIT"
    RATE_LIMITED = "RATE_LIMITED"
    INVALID_ORDER = "INVALID_ORDER"
    INVALID_REQUEST = "INVALID_REQUEST"
    EPISODE_ENDED = "EPISODE_ENDED"
    ENVIRONMENT_FIDELITY_LIMIT = "ENVIRONMENT_FIDELITY_LIMIT"
    MODEL_CAPACITY_LIMIT = "MODEL_CAPACITY_LIMIT"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    RUN_PAUSED = "RUN_PAUSED"
    UNAUTHORIZED = "UNAUTHORIZED"
    SESSION_FINISHED = "SESSION_FINISHED"


class Completeness(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNKNOWN = "unknown"
    EMPTY_VERIFIED = "empty_verified"


class CoverageState(StrEnum):
    PENDING = "pending"
    COMPLETED_AND_CHECKED = "completed_and_checked"
    PARTIAL = "partial"
    MISSING = "missing"
    FAILED = "failed"


class ValuationClass(StrEnum):
    CASH = "cash"
    PRICED_LIQUIDATABLE = "priced_liquidatable"
    NO_ROUTE = "no_route"
    UNPRICED_MISSING_DATA = "unpriced_missing_data"


class GateStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    NOT_APPLICABLE = "not_applicable"
    WARNING = "warning"


class ValidationStatus(StrEnum):
    TESTED = "tested"
    NOT_TESTED = "not_tested"
    FAILED = "failed"
    ASSUMED = "assumed"
