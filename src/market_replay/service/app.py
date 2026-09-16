"""FastAPI application: control plane (/api/v1, admin token) and agent plane (/agent/v1, session token)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..domain.envelope import Envelope
from ..engine.session import TOOLS, UNSUPPORTED_CAPABILITIES
from .auth import constant_time_equal, resolve_admin_token
from .runs import ApiError, RunManager

REPO_ROOT = Path(__file__).resolve().parents[3]
WEB_DIST = REPO_ROOT / "apps" / "web" / "dist"


class ImportPackBody(BaseModel):
    path: str
    name: str | None = None


class AgentBody(BaseModel):
    name: str
    version: str
    runtime: str = "external"
    capabilities: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)


class LaunchBody(BaseModel):
    kind: str = "example"
    name: str
    runtime: str = "python"
    args: list[str] = Field(default_factory=list)


class RunBody(BaseModel):
    agent_id: str
    pack_id: str
    mode: str = "practice"
    bankroll_raw: str = "1000000"
    mask_seed: str | None = None
    engine_seed: str | None = None
    agent_seed: str | None = None
    isolation: str = "trusted_external_client"
    launch: LaunchBody | None = None


class SuiteRunBody(BaseModel):
    agent_id: str
    launch: LaunchBody
    isolation: str | None = None
    wait: bool = True
    agent_seed: str | None = None


class ComparisonBody(BaseModel):
    suite_id: str | None = None
    agent_a: str
    agent_b: str
    run_ids_a: list[str] | None = None
    run_ids_b: list[str] | None = None


class StudyBody(BaseModel):
    candidates: list[str]
    pack_ids: list[str]
    comparison_id: str | None = None
    intended_outcome: str
    prediction: str


class OutcomeBody(BaseModel):
    description: str
    data: dict[str, Any] = Field(default_factory=dict)


class CommandBody(BaseModel):
    request_id: str = Field(min_length=1, max_length=128)
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    session_id: str | None = None


def cors_origins_from_env() -> list[str]:
    """Browser origins allowed to call this server (a statically hosted UI, for example). Empty by default."""
    raw = os.environ.get("MARKET_REPLAY_CORS_ORIGINS", "")
    return [o.strip().rstrip("/") for o in raw.split(",") if o.strip()]


def create_app(manager: RunManager, admin_token: str | None = None, cors_origins: list[str] | None = None) -> FastAPI:
    token = resolve_admin_token(admin_token)
    app = FastAPI(title="Market Replay", version="0.1.0", description="Strategy-agnostic trading-agent evaluator: control plane and agent plane.")
    app.state.manager = manager
    app.state.admin_token = token
    if cors_origins:
        # Explicit allowlist only; never "*". Credentials are bearer headers, so allow the Authorization header.
        app.add_middleware(CORSMiddleware, allow_origins=list(cors_origins), allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["Authorization", "Content-Type"], max_age=600)

    def require_admin(authorization: str | None = Header(default=None)) -> None:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, {"code": "UNAUTHORIZED", "message": "admin bearer token required"})
        supplied = authorization[7:]
        if supplied.startswith("agt_") or not constant_time_equal(supplied, token):
            raise HTTPException(403, {"code": "FORBIDDEN", "message": "agent credentials cannot access the control plane"})

    def agent_token(authorization: str | None = Header(default=None)) -> str:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, {"code": "UNAUTHORIZED", "message": "session bearer token required"})
        supplied = authorization[7:]
        if not supplied.startswith("agt_"):
            raise HTTPException(403, {"code": "FORBIDDEN", "message": "only session credentials may call the agent plane"})
        return supplied

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"code": exc.code, "message": exc.message})

    # ------------------------------------------------------------------ health / meta
    @app.get("/api/v1/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "service": "market-replay", "dev_mode": manager.dev_mode}

    @app.get("/api/v1/meta", dependencies=[Depends(require_admin)])
    def meta() -> dict[str, Any]:
        return {"tools": TOOLS, "unsupported_capabilities": UNSUPPORTED_CAPABILITIES, "gateway_url": manager.gateway_url, "dev_mode": manager.dev_mode}

    # ------------------------------------------------------------------ packs
    @app.post("/api/v1/packs/import", dependencies=[Depends(require_admin)])
    def import_pack(body: ImportPackBody) -> dict[str, Any]:
        return manager.import_pack(body.path, body.name)

    @app.get("/api/v1/packs", dependencies=[Depends(require_admin)])
    def list_packs() -> dict[str, Any]:
        return {"items": manager.packs()}

    @app.get("/api/v1/packs/{pack_id}", dependencies=[Depends(require_admin)])
    def get_pack(pack_id: str) -> dict[str, Any]:
        return manager.pack_row(pack_id)

    @app.get("/api/v1/packs/{pack_id}/validation", dependencies=[Depends(require_admin)])
    def pack_validation(pack_id: str) -> dict[str, Any]:
        _row, pack = manager.load_pack(pack_id)
        return pack.validation

    @app.get("/api/v1/packs/{pack_id}/health", dependencies=[Depends(require_admin)])
    def pack_health(pack_id: str) -> dict[str, Any]:
        return manager.pack_health(pack_id)

    @app.get("/api/v1/packs/{pack_id}/descriptor", dependencies=[Depends(require_admin)])
    def pack_descriptor(pack_id: str) -> dict[str, Any]:
        row, pack = manager.load_pack(pack_id)
        return manager.public_descriptor(pack, row, "trusted_external_client").model_dump(mode="json")

    # ------------------------------------------------------------------ agents
    @app.post("/api/v1/agents", dependencies=[Depends(require_admin)], status_code=201)
    def create_agent(body: AgentBody) -> dict[str, Any]:
        return manager.register_agent(name=body.name, version=body.version, runtime=body.runtime, capabilities=body.capabilities, config=body.config)

    @app.get("/api/v1/agents", dependencies=[Depends(require_admin)])
    def list_agents() -> dict[str, Any]:
        return {"items": manager.agents()}

    @app.get("/api/v1/agents/{agent_id}", dependencies=[Depends(require_admin)])
    def get_agent(agent_id: str) -> dict[str, Any]:
        return manager.agent_view(agent_id)

    # ------------------------------------------------------------------ suites
    @app.get("/api/v1/suites", dependencies=[Depends(require_admin)])
    def list_suites() -> dict[str, Any]:
        return {"items": manager.suites_view()}

    @app.post("/api/v1/suites/{suite_id}/runs", dependencies=[Depends(require_admin)], status_code=201)
    def run_suite(suite_id: str, body: SuiteRunBody) -> dict[str, Any]:
        return manager.run_suite(suite_id, agent_id=body.agent_id, launch_spec=body.launch.model_dump(), isolation=body.isolation, wait=body.wait, agent_seed=body.agent_seed)

    @app.get("/api/v1/suite-runs", dependencies=[Depends(require_admin)])
    def list_suite_runs() -> dict[str, Any]:
        return {"items": manager.store.suite_runs()}

    # ------------------------------------------------------------------ runs
    @app.post("/api/v1/runs", dependencies=[Depends(require_admin)], status_code=201)
    def create_run(body: RunBody) -> dict[str, Any]:
        return manager.create_run(
            agent_id=body.agent_id,
            pack_ref=body.pack_id,
            mode=body.mode,
            bankroll_raw=body.bankroll_raw,
            mask_seed=body.mask_seed,
            engine_seed=body.engine_seed,
            isolation=body.isolation,
            launch_spec=body.launch.model_dump() if body.launch else None,
            agent_seed=body.agent_seed,
        )

    @app.get("/api/v1/runs", dependencies=[Depends(require_admin)])
    def list_runs(agent_id: str | None = None, pack_id: str | None = None, suite_id: str | None = None) -> dict[str, Any]:
        where = {k: v for k, v in {"agent_id": agent_id, "pack_id": pack_id, "suite_id": suite_id}.items() if v}
        return {"items": manager.runs(**where)}

    @app.get("/api/v1/runs/{run_id}", dependencies=[Depends(require_admin)])
    def get_run(run_id: str) -> dict[str, Any]:
        return manager.run_view(run_id)

    @app.post("/api/v1/runs/{run_id}/pause", dependencies=[Depends(require_admin)])
    def pause_run(run_id: str) -> dict[str, Any]:
        return manager.pause(run_id)

    @app.post("/api/v1/runs/{run_id}/resume", dependencies=[Depends(require_admin)])
    def resume_run(run_id: str) -> dict[str, Any]:
        return manager.resume(run_id)

    @app.post("/api/v1/runs/{run_id}/abort", dependencies=[Depends(require_admin)])
    def abort_run(run_id: str) -> dict[str, Any]:
        return manager.abort(run_id)

    @app.get("/api/v1/runs/{run_id}/report", dependencies=[Depends(require_admin)])
    def run_report(run_id: str, role: str = Query(default="admin", pattern="^(admin|participant)$")) -> dict[str, Any]:
        return manager.report(run_id, role)

    @app.get("/api/v1/runs/{run_id}/observed", dependencies=[Depends(require_admin)])
    def run_observed(run_id: str, pool_id: str | None = None, interval_ms: int = 60_000) -> dict[str, Any]:
        return manager.observed(run_id, pool_id, interval_ms)

    @app.get("/api/v1/runs/{run_id}/export", dependencies=[Depends(require_admin)])
    def run_export(run_id: str, role: str = Query(default="admin", pattern="^(admin|participant)$"), include_mappings: bool = False) -> dict[str, Any]:
        return manager.export(run_id, role, include_mappings)

    @app.post("/api/v1/runs/{run_id}/replay", dependencies=[Depends(require_admin)])
    def run_replay(run_id: str) -> dict[str, Any]:
        return manager.replay(run_id)

    # ------------------------------------------------------------------ comparisons / studies
    @app.post("/api/v1/comparisons", dependencies=[Depends(require_admin)], status_code=201)
    def create_comparison(body: ComparisonBody) -> dict[str, Any]:
        return manager.compare(suite_id=body.suite_id, agent_a=body.agent_a, agent_b=body.agent_b, run_ids_a=body.run_ids_a, run_ids_b=body.run_ids_b)

    @app.get("/api/v1/comparisons", dependencies=[Depends(require_admin)])
    def list_comparisons() -> dict[str, Any]:
        return {"items": manager.comparisons()}

    @app.get("/api/v1/comparisons/{comparison_id}", dependencies=[Depends(require_admin)])
    def get_comparison(comparison_id: str) -> dict[str, Any]:
        return manager.comparison(comparison_id)

    @app.post("/api/v1/studies", dependencies=[Depends(require_admin)], status_code=201)
    def create_study(body: StudyBody) -> dict[str, Any]:
        return manager.register_study(candidates=body.candidates, pack_ids=body.pack_ids, comparison_id=body.comparison_id, intended_outcome=body.intended_outcome, prediction=body.prediction)

    @app.get("/api/v1/studies", dependencies=[Depends(require_admin)])
    def list_studies() -> dict[str, Any]:
        return {"items": manager.studies()}

    @app.post("/api/v1/studies/{study_id}/outcomes", dependencies=[Depends(require_admin)])
    def add_outcome(study_id: str, body: OutcomeBody) -> dict[str, Any]:
        return manager.attach_study_outcome(study_id, description=body.description, data=body.data)

    # ------------------------------------------------------------------ agent plane
    @app.get("/agent/v1/capabilities")
    def capabilities(tok: str = Depends(agent_token)) -> dict[str, Any]:
        env = manager.handle_command(tok, "capabilities", "session.describe", {})
        return env.model_dump(mode="json")

    @app.post("/agent/v1/commands", response_model=Envelope)
    def commands(body: CommandBody, tok: str = Depends(agent_token)) -> Envelope:
        return manager.handle_command(tok, body.request_id, body.tool, body.arguments, body.session_id)

    # Any attempt by an agent credential to reach control-plane paths is rejected by require_admin above.

    # ------------------------------------------------------------------ web UI
    if WEB_DIST.exists():
        app.mount("/assets", StaticFiles(directory=str(WEB_DIST / "assets")), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str) -> FileResponse:
            candidate = WEB_DIST / full_path
            if full_path and candidate.exists() and candidate.is_file():
                return FileResponse(str(candidate))
            return FileResponse(str(WEB_DIST / "index.html"))

    return app


def default_manager() -> RunManager:
    data_dir = Path(os.environ.get("MARKET_REPLAY_DATA_DIR", str(REPO_ROOT / "data")))
    dev_mode = os.environ.get("MARKET_REPLAY_DEV_MODE", "1") != "0"
    return RunManager(data_dir=data_dir, dev_mode=dev_mode)
