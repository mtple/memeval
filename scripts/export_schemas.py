"""Export OpenAPI (control + agent plane), JSON Schemas for canonical objects, and command examples."""

from __future__ import annotations

import json
from pathlib import Path

from market_replay.datasets.execution_params import ExecutionParams
from market_replay.domain.envelope import Envelope
from market_replay.domain.models import (
    Asset,
    CoverageInterval,
    EpisodeManifest,
    Pool,
    PublicDescriptor,
    RawReceipt,
    RestrictionObservation,
    TapeEvent,
)
from market_replay.engine.session import TOOLS, UNSUPPORTED_CAPABILITIES
from market_replay.service.app import create_app
from market_replay.service.runs import RunManager

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "schemas"


def main() -> None:
    OUT.mkdir(exist_ok=True)
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        mgr = RunManager(data_dir=Path(td))
        app = create_app(mgr, "adm_schema_export")
        (OUT / "openapi.json").write_text(json.dumps(app.openapi(), indent=2, sort_keys=True))
        mgr.close()
    for model in (Envelope, Asset, Pool, TapeEvent, CoverageInterval, RestrictionObservation, RawReceipt, EpisodeManifest, PublicDescriptor, ExecutionParams):
        (OUT / f"{model.__name__}.schema.json").write_text(json.dumps(model.model_json_schema(), indent=2, sort_keys=True))
    tools = {
        "tools": TOOLS,
        "unsupported_capabilities": UNSUPPORTED_CAPABILITIES,
        "mcp_tool_names": {name.replace(".", "_"): name for name in TOOLS},
        "command_request_example": {
            "request_id": "client_unique_001",
            "tool": "broker.submit",
            "arguments": {
                "pool_id": "pool_n7q2abcd",
                "asset_in": "CASH",
                "asset_out": "asset_p2dxyz12",
                "amount_in_raw": "1000000",
                "min_amount_out_raw": "950000000",
                "deadline_ms": 180000,
                "idempotency_key": "agent_intent_42",
            },
            "note": "Illustrative protocol values, not recommended sizes; decimals come from session.describe.",
        },
        "envelope_example": {
            "request_id": "req_opaque",
            "session_id": "ses_opaque",
            "clock_ms": 123400,
            "status": "ok",
            "data": {},
            "quality": {"completeness": "partial", "availability_basis": "fixture_delay_model", "observed_through_ms": 123000, "stale": False, "warnings": ["GENERATED_FIXTURE"]},
            "error": None,
        },
        "error_codes": [
            "NOT_YET_DISCOVERED", "OUTSIDE_COVERAGE", "UNSUPPORTED_CAPABILITY", "MISSING_DATA", "STALE_DATA", "NO_ROUTE",
            "INSUFFICIENT_FUNDS", "QUOTE_EXPIRED", "SLIPPAGE_LIMIT", "RATE_LIMITED", "INVALID_ORDER", "INVALID_REQUEST",
            "EPISODE_ENDED", "ENVIRONMENT_FIDELITY_LIMIT", "MODEL_CAPACITY_LIMIT", "IDEMPOTENCY_CONFLICT", "BUDGET_EXHAUSTED",
            "RUN_PAUSED", "UNAUTHORIZED", "SESSION_FINISHED",
        ],
    }
    (OUT / "agent_tools.json").write_text(json.dumps(tools, indent=2, sort_keys=True))
    print(f"wrote {len(list(OUT.iterdir()))} files to {OUT}")


if __name__ == "__main__":
    main()
