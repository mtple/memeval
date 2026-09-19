"""FastAPI application: control plane (/api/v1, admin token) and agent plane (/agent/v1, session token)."""

from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.gzip import GZipMiddleware

from ..domain.envelope import Envelope
from ..domain.profiles import PROFILES
from ..engine.session import TOOLS, UNSUPPORTED_CAPABILITIES
from .auth import constant_time_equal, resolve_admin_token
from .db import RunBusy
from .mcp_server import build_remote_mcp
from .runs import AGENT_VERSION, ApiError, RunManager

REPO_ROOT = Path(__file__).resolve().parents[3]
WEB_DIST = REPO_ROOT / "apps" / "web" / "dist"
SKILL_DIR = REPO_ROOT / "skills" / "market-replay"
SKILL_CANONICAL_URL = "https://memeval-web.vercel.app"


def skill_text(gateway_url: str, name: str = "SKILL.md") -> str:
    """The agent skill (or one of its scripts) with this server's URL in place of the canonical one."""
    path = SKILL_DIR / name if name == "SKILL.md" else SKILL_DIR / "scripts" / name
    text = path.read_text()
    return text.replace(SKILL_CANONICAL_URL, gateway_url.rstrip("/"))


class ImportPackBody(BaseModel):
    # Reject retired visibility options instead of silently publishing a private import.
    model_config = ConfigDict(extra="forbid")
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


class InlineAgentBody(BaseModel):
    """Self-serve agent identity: the name is the agent. A `version` sent by an older client is
    accepted and ignored so those clients keep working; every run lands under the one name."""

    name: str = Field(min_length=1, max_length=64)
    version: str = Field(default="1", max_length=32)  # legacy: accepted, ignored
    runtime: str = "external"
    capabilities: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)


class RunBody(BaseModel):
    agent_id: str | None = None
    agent: InlineAgentBody | None = None
    pack_id: str
    mode: str = "practice"
    bankroll_raw: str | None = None  # default: one whole unit of the pack's cash asset
    mask_seed: str | None = None
    engine_seed: str | None = None
    agent_seed: str | None = None
    isolation: str = "trusted_external_client"
    launch: LaunchBody | None = None
    resource_profile_id: str = "pack_defaults_v1"
    execute: str | None = None


class EnrollBody(BaseModel):
    agent: InlineAgentBody


class RenameBody(BaseModel):
    name: str
    agent_token: str | None = None


class PlayBody(BaseModel):
    agent_token: str | None = None  # or Authorization: Bearer agn_...
    suite_id: str | None = None
    pack_id: str | None = None
    pack_ids: list[str] | None = None  # the episodes the agent's owner chose; empty body = every unfinished one


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


def public_runs_from_env() -> bool:
    """Anyone may register an agent and start a run unless MARKET_REPLAY_PUBLIC_RUNS=0."""
    return os.environ.get("MARKET_REPLAY_PUBLIC_RUNS", "1") != "0"


def client_ip(request: Request) -> str:
    """Best available client address: platform headers first (Vercel sets them; clients cannot), then the socket."""
    h = request.headers
    for name in ("x-vercel-forwarded-for", "x-real-ip", "x-forwarded-for"):
        v = h.get(name)
        if v:
            return v.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def create_app(manager: RunManager, admin_token: str | None = None, cors_origins: list[str] | None = None, public_runs: bool | None = None) -> FastAPI:
    token = resolve_admin_token(admin_token)
    public_runs = public_runs_from_env() if public_runs is None else bool(public_runs)
    remote_mcp = build_remote_mcp(manager, public_runs=lambda: public_runs, client_ip=client_ip)
    mcp_asgi = remote_mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async with remote_mcp.session_manager.run():
            yield

    app = FastAPI(title="Market Replay", version="0.1.0", description="Strategy-agnostic trading-agent evaluator: control plane and agent plane.", lifespan=lifespan)
    app.state.manager = manager
    app.state.admin_token = token
    app.state.public_runs = public_runs
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)
    if cors_origins:
        # Explicit allowlist only; never "*". Credentials are bearer headers, so allow the Authorization header.
        app.add_middleware(CORSMiddleware, allow_origins=list(cors_origins), allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["Authorization", "Content-Type"], max_age=600)

    def role_of(authorization: str | None) -> str:
        """"public" without a credential, "admin" with the admin token; anything else is refused.

        Agent (session) tokens are rejected outright so a participant can never read the control plane,
        not even the parts anyone else can read anonymously.
        """
        if not authorization:
            return "public"
        if not authorization.startswith("Bearer "):
            raise HTTPException(401, {"code": "UNAUTHORIZED", "message": "malformed Authorization header"})
        supplied = authorization[7:]
        if supplied.startswith("agt_"):
            raise HTTPException(403, {"code": "FORBIDDEN", "message": "agent credentials cannot access the control plane"})
        if supplied.startswith("agn_"):
            return "public"  # an agent's identity token: a public caller that the play endpoints resolve themselves
        if not constant_time_equal(supplied, token):
            raise HTTPException(403, {"code": "FORBIDDEN", "message": "invalid admin token"})
        return "admin"

    def require_admin(authorization: str | None = Header(default=None)) -> str:
        if role_of(authorization) != "admin":
            raise HTTPException(401, {"code": "UNAUTHORIZED", "message": "admin bearer token required"})
        return "admin"

    def public_read(request: Request, authorization: str | None = Header(default=None)) -> str:
        role = role_of(authorization)
        if role != "admin":
            parts = request.url.path.split("/")
            if len(parts) >= 5 and parts[3] in ("packs", "runs"):
                manager.require_public(parts[3], parts[4])
        return role

    def public_write(kind: str):
        """Self-serve writes: anyone when public runs are on (rate-limited per address); the operator always."""

        def dep(request: Request, authorization: str | None = Header(default=None)) -> str:
            role = role_of(authorization)
            if role == "admin":
                return role
            parts = request.url.path.split("/")
            if len(parts) >= 5 and parts[3] in ("packs", "runs"):
                manager.require_public(parts[3], parts[4])
            if not public_runs:
                raise HTTPException(401, {"code": "UNAUTHORIZED", "message": "admin bearer token required (public runs are switched off on this server)"})
            manager.rate_limit(kind, client_ip(request))
            return role

        return dep

    def agent_token(authorization: str | None = Header(default=None)) -> str:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, {"code": "UNAUTHORIZED", "message": "session bearer token required"})
        supplied = authorization[7:]
        if not supplied.startswith("agt_"):
            raise HTTPException(403, {"code": "FORBIDDEN", "message": "only session credentials may call the agent plane"})
        return supplied

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"code": exc.code, "message": exc.message, "clock_ms": getattr(exc, "clock_ms", None)})

    @app.exception_handler(HTTPException)
    async def _http_error(_: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail, "clock_ms": None}, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _invalid_request(request: Request, exc: RequestValidationError):
        response = await request_validation_exception_handler(request, exc)
        import json
        return JSONResponse(status_code=response.status_code, content={**json.loads(response.body), "clock_ms": None})

    @app.exception_handler(RunBusy)
    async def _run_busy(_: Request, exc: RunBusy) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"code": exc.code, "message": exc.message, "clock_ms": getattr(exc, "clock_ms", None)}, headers={"Retry-After": "1"})

    # ------------------------------------------------------------------ health / meta
    @app.get("/api/v1/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "service": "market-replay", "dev_mode": manager.dev_mode}

    @app.get("/api/v1/meta")
    def meta(role: str = Depends(public_read)) -> dict[str, Any]:
        return {
            "role": role,
            "public_runs": public_runs,
            "rate_limit_per_hour_per_ip": manager.max_runs_per_hour_per_ip,
            "tools": TOOLS,
            "unsupported_capabilities": UNSUPPORTED_CAPABILITIES,
            "gateway_url": manager.gateway_url,
            "mcp_url": manager.gateway_url + "/agent/mcp",
            "dev_mode": manager.dev_mode,
            "hosted": manager.hosted,
            "runtimes_available": list(manager.runtimes_available),
            "store_backend": manager.store.backend,
        }

    @app.get("/api/v1/usage", dependencies=[Depends(public_read)])
    def usage() -> dict[str, Any]:
        return manager.usage_view()

    # ------------------------------------------------------------------ packs
    @app.post("/api/v1/packs/import", dependencies=[Depends(require_admin)])
    def import_pack(body: ImportPackBody) -> dict[str, Any]:
        return manager.import_pack(body.path, body.name)

    @app.get("/api/v1/packs")
    def list_packs(role: str = Depends(public_read)) -> dict[str, Any]:
        return {"items": manager.packs(reveal_dates=role == "admin")}

    @app.get("/api/v1/packs/{pack_id}")
    def get_pack(pack_id: str, role: str = Depends(public_read)) -> dict[str, Any]:
        return manager.pack_row(pack_id, reveal_dates=role == "admin")

    @app.get("/api/v1/packs/{pack_id}/validation", dependencies=[Depends(public_read)])
    def pack_validation(pack_id: str) -> dict[str, Any]:
        _row, pack = manager.load_pack(pack_id)
        return pack.validation

    @app.get("/api/v1/packs/{pack_id}/health", dependencies=[Depends(public_read)])
    def pack_health(pack_id: str) -> dict[str, Any]:
        return manager.pack_health(pack_id)

    @app.get("/api/v1/packs/{pack_id}/descriptor", dependencies=[Depends(public_read)])
    def pack_descriptor(pack_id: str) -> dict[str, Any]:
        row, pack = manager.load_pack(pack_id)
        return manager.public_descriptor(pack, row, "trusted_external_client").model_dump(mode="json")

    # ------------------------------------------------------------------ agents
    @app.post("/api/v1/enroll", dependencies=[Depends(public_write("runs"))], status_code=201)
    def enroll(body: EnrollBody, request: Request) -> dict[str, Any]:
        """Join: register by name (once) and receive the agent's identity token. Playing is a separate call."""
        return manager.enroll(agent=body.agent.model_dump(), client_key=client_ip(request))

    def identity_token(body_token: str | None, authorization: str | None) -> str:
        supplied = authorization[7:] if authorization and authorization.startswith("Bearer ") else (body_token or "")
        if not supplied.startswith("agn_"):
            raise HTTPException(401, {"code": "UNAUTHORIZED", "message": "agent identity token (agn_...) required: the agent_token from joining, as Authorization: Bearer or in the body"})
        return supplied

    @app.get("/api/v1/resource-profiles", dependencies=[Depends(public_read)])
    def resource_profiles() -> dict:
        return {"items": [p.public() for p in PROFILES.values()]}

    @app.post("/api/v1/play", dependencies=[Depends(public_write("runs"))], status_code=201)
    def play(body: PlayBody, request: Request, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        """Trade: one run with a session credential per episode this agent has not finished. Repeatable."""
        return manager.play(agent_token=identity_token(body.agent_token, authorization), suite_id=body.suite_id, pack_id=body.pack_id, pack_ids=body.pack_ids, client_key=client_ip(request))

    @app.patch("/api/v1/agents/me", dependencies=[Depends(public_write("agents"))])
    def rename_me(body: RenameBody, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        """Rename the agent this identity token belongs to; id, token, runs and rankings stay."""
        return manager.rename_agent(agent_token=identity_token(body.agent_token, authorization), name=body.name)

    @app.get("/api/v1/play", dependencies=[Depends(public_read)])
    def episodes(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        """What this agent could play: every real episode with its standing on it (new, running, finished)."""
        row = manager.agent_by_token(identity_token(None, authorization))
        return {"agent_id": row["agent_id"], "episodes": manager.episodes_for(row["agent_id"])}

    @app.get("/api/v1/leaderboard", dependencies=[Depends(public_read)])
    def leaderboard(suite_id: str | None = None, pack_id: str | None = None, all: bool = False, include_artificial: bool = True) -> dict[str, Any]:
        """Default: the first category with results, real weeks (newest first) before generated data."""
        if suite_id or pack_id or all:
            board = manager.leaderboard(suite_id=suite_id, pack_id=pack_id)
        else:
            cats = manager.leaderboard_categories(include_artificial=include_artificial)
            board = None
            for cat in cats:
                b = manager.leaderboard(suite_id=cat["id"]) if cat["kind"] == "suite" else manager.leaderboard(pack_id=cat["id"])
                if b["rows"]:
                    board = b
                    break
            if board is None:
                board = (manager.leaderboard(pack_id=cats[0]["id"]) if cats[0]["kind"] == "pack" else manager.leaderboard(suite_id=cats[0]["id"])) if cats else manager.leaderboard()
        board["categories"] = manager.leaderboard_categories(include_artificial=include_artificial)
        return board

    @app.get("/join", include_in_schema=False)
    @app.get("/api/v1/skill", include_in_schema=False)
    @app.get("/skill.md", include_in_schema=False)
    def skill() -> PlainTextResponse:
        return PlainTextResponse(skill_text(manager.gateway_url), media_type="text/markdown; charset=utf-8")

    @app.get("/skill/{script}", include_in_schema=False)
    def skill_script(script: str) -> PlainTextResponse:
        if script != "market_replay_agent.py":
            raise HTTPException(404, {"code": "NOT_FOUND", "message": "unknown skill script"})
        return PlainTextResponse(skill_text(manager.gateway_url, script), media_type="text/x-python; charset=utf-8")

    @app.post("/api/v1/agents", dependencies=[Depends(public_write("agents"))], status_code=201)
    def create_agent(body: AgentBody) -> dict[str, Any]:
        return manager.register_agent(name=body.name, version=body.version, runtime=body.runtime, capabilities=body.capabilities, config=body.config)

    @app.get("/api/v1/agents")
    def list_agents(role: str = Depends(public_read)) -> dict[str, Any]:
        return {"items": manager.agents(include_internal=role == "admin")}

    @app.get("/api/v1/agents/{agent_id}", dependencies=[Depends(public_read)])
    def get_agent(agent_id: str) -> dict[str, Any]:
        return manager.agent_view(agent_id)

    @app.get("/api/v1/agents/{agent_id}/history", dependencies=[Depends(public_read)])
    def get_agent_history(agent_id: str) -> dict[str, Any]:
        """The agent's runs day by day, each described in plain words with the number that counts."""
        return manager.agent_history(agent_id)

    # ------------------------------------------------------------------ suites
    @app.get("/api/v1/suites", dependencies=[Depends(public_read)])
    def list_suites() -> dict[str, Any]:
        return {"items": manager.suites_view()}

    @app.post("/api/v1/suites/{suite_id}/runs", dependencies=[Depends(require_admin)], status_code=201)
    def run_suite(suite_id: str, body: SuiteRunBody) -> dict[str, Any]:
        return manager.run_suite(suite_id, agent_id=body.agent_id, launch_spec=body.launch.model_dump(), isolation=body.isolation, wait=body.wait, agent_seed=body.agent_seed)

    @app.get("/api/v1/suite-runs", dependencies=[Depends(public_read)])
    def list_suite_runs() -> dict[str, Any]:
        return {"items": manager.store.suite_runs()}

    # ------------------------------------------------------------------ runs
    @app.post("/api/v1/runs", dependencies=[Depends(public_write("runs"))], status_code=201)
    def create_run(body: RunBody) -> dict[str, Any]:
        if body.agent is not None:
            agent_id = manager.register_or_reuse_agent(name=body.agent.name, version=AGENT_VERSION, runtime=body.agent.runtime, capabilities=body.agent.capabilities, config=body.agent.config)["agent_id"]
        elif body.agent_id:
            agent_id = body.agent_id
        else:
            raise ApiError(400, "agent_id or agent {name} is required", "INVALID")
        return manager.create_run(
            agent_id=agent_id,
            pack_ref=body.pack_id,
            mode=body.mode,
            bankroll_raw=body.bankroll_raw,
            mask_seed=body.mask_seed,
            engine_seed=body.engine_seed,
            isolation=body.isolation,
            launch_spec=body.launch.model_dump() if body.launch else None,
            agent_seed=body.agent_seed,
            resource_profile_id=body.resource_profile_id,
            execute=body.execute,
        )

    @app.get("/api/v1/runs")
    def list_runs(agent_id: str | None = None, pack_id: str | None = None, suite_id: str | None = None, role: str = Depends(public_read)) -> dict[str, Any]:
        where = {k: v for k, v in {"agent_id": agent_id, "pack_id": pack_id, "suite_id": suite_id}.items() if v}
        return {"items": manager.runs(include_internal=role == "admin", **where)}

    @app.get("/api/v1/runs/{run_id}")
    def get_run(run_id: str, caller: str = Depends(public_read)) -> dict[str, Any]:
        return manager.run_view(run_id, allow_private=caller == "admin")

    @app.get("/api/v1/runs/{run_id}/timeline", dependencies=[Depends(public_read)])
    def decision_timeline(run_id: str, cursor: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=500)) -> dict:
        from ..evaluation.debrief import timeline
        from ..observations.masking import redact_for_role
        with manager.store.run_lock(run_id):
            ctx = manager._ctx(run_id)
            manager._catch_up(ctx)
            return redact_for_role(timeline(ctx.session, cursor, limit), "participant", ctx.session.scanner)

    @app.post("/api/v1/runs/{run_id}/execute", dependencies=[Depends(public_write("runs"))])
    def execute_run(run_id: str) -> dict[str, Any]:
        """Run a launched Python reference participant to completion inside this request (hosted mode)."""
        return manager.execute_run(run_id)

    @app.post("/api/v1/runs/{run_id}/pause", dependencies=[Depends(require_admin)])
    def pause_run(run_id: str) -> dict[str, Any]:
        return manager.pause(run_id)

    @app.post("/api/v1/runs/{run_id}/resume", dependencies=[Depends(require_admin)])
    def resume_run(run_id: str) -> dict[str, Any]:
        return manager.resume(run_id)

    @app.post("/api/v1/runs/{run_id}/abort", dependencies=[Depends(require_admin)])
    def abort_run(run_id: str) -> dict[str, Any]:
        return manager.abort(run_id)

    @app.post("/api/v1/runs/{run_id}/report/repair")
    def repair_run_report(run_id: str, caller: str = Depends(public_write("runs"))) -> dict[str, Any]:
        manager.repair_report(run_id)
        return manager.report(run_id, "admin" if caller == "admin" else "participant")

    @app.get("/api/v1/runs/{run_id}/report")
    def run_report(run_id: str, role: str = Query(default="admin", pattern="^(admin|participant)$"), caller: str = Depends(public_read)) -> dict[str, Any]:
        # Results are public, but only the operator sees the unredacted (de-aliased) report.
        report = manager.report(run_id, role if caller == "admin" else "participant")
        if report.get("error"):
            raise ApiError(503, "Report generation failed. The recorded run is preserved.", "REPORT_GENERATION_FAILED")
        return report

    @app.get("/api/v1/runs/{run_id}/trade-review", dependencies=[Depends(public_read)])
    def trade_review(run_id: str) -> dict[str, Any]:
        return manager.trade_review(run_id)

    @app.get("/api/v1/runs/{run_id}/observed", dependencies=[Depends(public_read)])
    def run_observed(run_id: str, pool_id: str | None = None, interval_ms: int = 60_000) -> dict[str, Any]:
        return manager.observed(run_id, pool_id, interval_ms)

    @app.get("/api/v1/runs/{run_id}/export")
    def run_export(run_id: str, role: str = Query(default="admin", pattern="^(admin|participant)$"), include_mappings: bool = False, caller: str = Depends(public_read)) -> dict[str, Any]:
        if (role == "admin" or include_mappings) and caller != "admin":
            raise HTTPException(401, {"code": "UNAUTHORIZED", "message": "admin bearer token required for the admin export"})
        return manager.export(run_id, role, include_mappings)

    @app.post("/api/v1/runs/{run_id}/replay", dependencies=[Depends(require_admin)])
    def run_replay(run_id: str) -> dict[str, Any]:
        return manager.replay(run_id)

    # ------------------------------------------------------------------ comparisons / studies
    @app.post("/api/v1/comparisons", dependencies=[Depends(public_write("comparisons"))], status_code=201)
    def create_comparison(body: ComparisonBody) -> dict[str, Any]:
        return manager.compare(suite_id=body.suite_id, agent_a=body.agent_a, agent_b=body.agent_b, run_ids_a=body.run_ids_a, run_ids_b=body.run_ids_b)

    @app.get("/api/v1/comparisons", dependencies=[Depends(public_read)])
    def list_comparisons() -> dict[str, Any]:
        return {"items": manager.comparisons()}

    @app.get("/api/v1/comparisons/{comparison_id}", dependencies=[Depends(public_read)])
    def get_comparison(comparison_id: str) -> dict[str, Any]:
        return manager.comparison(comparison_id)

    @app.post("/api/v1/studies", dependencies=[Depends(require_admin)], status_code=201)
    def create_study(body: StudyBody) -> dict[str, Any]:
        return manager.register_study(candidates=body.candidates, pack_ids=body.pack_ids, comparison_id=body.comparison_id, intended_outcome=body.intended_outcome, prediction=body.prediction)

    @app.get("/api/v1/studies", dependencies=[Depends(public_read)])
    def list_studies() -> dict[str, Any]:
        return {"items": manager.studies()}

    @app.post("/api/v1/studies/{study_id}/outcomes", dependencies=[Depends(require_admin)])
    def add_outcome(study_id: str, body: OutcomeBody) -> dict[str, Any]:
        return manager.attach_study_outcome(study_id, description=body.description, data=body.data)

    # ------------------------------------------------------------------ agent plane
    @app.get("/agent/v1/capabilities")
    def capabilities(tok: str = Depends(agent_token)) -> dict[str, Any]:
        env = manager.handle_command(tok, uuid.uuid4().hex, "session.describe", {})
        return env.model_dump(mode="json")

    @app.post("/agent/v1/commands", response_model=Envelope)
    def commands(body: CommandBody, tok: str = Depends(agent_token)) -> Envelope:
        return manager.handle_command(tok, body.request_id, body.tool, body.arguments, body.session_id)

    # MCP over streamable HTTP lives at /agent/mcp (sub-app mounted after the explicit /agent routes).
    # No gate: `enroll` needs no credential; every other tool needs a session token (header or argument).
    app.mount("/agent", mcp_asgi, name="mcp")

    # ------------------------------------------------------------------ web UI
    if WEB_DIST.exists():
        app.mount("/assets", StaticFiles(directory=str(WEB_DIST / "assets")), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str) -> FileResponse:
            if full_path == "api" or full_path.startswith("api/"):
                raise HTTPException(404, {"code": "NOT_FOUND", "message": "unknown API route"})
            candidate = (WEB_DIST / full_path).resolve()
            if not candidate.is_relative_to(WEB_DIST.resolve()):
                raise HTTPException(404, {"code": "NOT_FOUND", "message": "unknown resource"})
            if full_path and candidate.exists() and candidate.is_file():
                return FileResponse(str(candidate))
            return FileResponse(str(WEB_DIST / "index.html"))

    return app


def default_manager() -> RunManager:
    data_dir = Path(os.environ.get("MARKET_REPLAY_DATA_DIR", str(REPO_ROOT / "data")))
    dev_mode = os.environ.get("MARKET_REPLAY_DEV_MODE", "1") != "0"
    return RunManager(data_dir=data_dir, dev_mode=dev_mode)
