"""Run manager: the single place where sessions, credentials, launchers and reports meet.

One serialized command queue (an RLock) per active run. Runs never share portfolio,
market state, alias maps or random state. Traces are appended to JSONL per run.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..datasets.pack import Pack, PackError
from ..datasets.validator import validate_pack
from ..domain.envelope import Envelope
from ..domain.models import PublicDescriptor
from ..domain.status import ErrorCode, Isolation, RunState
from ..engine.session import TOOLS, UNSUPPORTED_CAPABILITIES, Session, replay_trace
from ..evaluation.compare import pair_runs
from ..evaluation.report import ENGINE_VERSION, build_report
from ..evaluation.study_registry import attach_outcome, new_study
from ..observations.masking import redact_for_role
from ..observations.store import basis_for
from ..runners.restricted import launch_restricted
from ..runners.trusted import LaunchSpec, launch
from .auth import new_agent_token, token_hash
from .db import Store
from .suites import SuiteDef, default_suites_text, load_suites, suite_public


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class ApiError(Exception):
    def __init__(self, status: int, message: str, code: str = "error") -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


@dataclass
class RunContext:
    run_id: str
    session: Session
    lock: threading.RLock
    token: str
    trace_path: Path
    started_wall: float
    launch: dict[str, Any] | None = None
    proc: Any = None
    monitor: threading.Thread | None = None
    controls: dict[str, Any] | None = None
    finalized: bool = False
    trace_written: int = 0
    inference: dict[str, Any] = field(default_factory=lambda: {"recorded": False})


class RunManager:
    def __init__(self, *, data_dir: Path, suites_path: Path | None = None, dev_mode: bool = True) -> None:
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(data_dir / "control_plane.sqlite")
        self.runs_dir = data_dir / "runs"
        self.runs_dir.mkdir(exist_ok=True)
        self.dev_mode = dev_mode
        self._packs: dict[str, Pack] = {}
        self._contexts: dict[str, RunContext] = {}
        self._global = threading.RLock()
        self.gateway_url = "http://127.0.0.1:8000"
        if suites_path is None:
            suites_path = data_dir / "suites.yaml"
            if not suites_path.exists():
                suites_path.write_text(default_suites_text())
        self.suites_path = suites_path
        self.suites: dict[str, SuiteDef] = load_suites(suites_path)

    # ------------------------------------------------------------------ packs
    def import_pack(self, path: str | Path, name: str | None = None) -> dict[str, Any]:
        p = Path(path)
        try:
            pack = Pack.load(p)
        except (PackError, FileNotFoundError, ValueError) as e:
            raise ApiError(400, f"pack import failed: {e}", "PACK_INVALID") from e
        report = validate_pack(pack)
        (p / "validation.json").write_text(json.dumps(report, indent=2, sort_keys=True))
        pack = Pack.load(p)
        m = pack.manifest
        pack_name = name or p.name
        existing = self.store.pack(m.pack_id)
        episode_id = existing["episode_id"] if existing else "ep_" + secrets.token_hex(6)
        row = {
            "pack_id": m.pack_id,
            "episode_id": episode_id,
            "name": pack_name,
            "path": str(p.resolve()),
            "origin": str(m.origin),
            "chain": m.chain,
            "scope_label": m.scope_label,
            "use_status": report["resulting_qualification"],
            "duration_ms": m.period.duration_ms,
            "is_full_week": int(m.period.is_full_week),
            "start_utc": m.period.start_utc,
            "end_utc": m.period.end_utc,
            "execution_model": str(m.execution.model),
            "imported_at": now_iso(),
            "summary_json": json.dumps(self._pack_summary(pack, report), sort_keys=True),
        }
        self.store.upsert_pack(row)
        with self._global:
            self._packs[m.pack_id] = pack
        return self.pack_row(m.pack_id)

    def _pack_summary(self, pack: Pack, report: dict[str, Any]) -> dict[str, Any]:
        m = pack.manifest
        supported = sum(1 for p in pack.pools.values() if p.supported_by_cpmm and p.initial_reserve0 is not None)
        cov = pack.coverage or {}
        states: dict[str, int] = {}
        for i in cov.get("intervals", []):
            states[i.get("state", "unknown")] = states.get(i.get("state", "unknown"), 0) + 1
        return {
            "pools_total": len(pack.pools),
            "pools_executable": supported,
            "assets_total": len(pack.assets),
            "tape_events": len(pack.tape),
            "coverage_states": states,
            "gates": [{"gate": g["gate"], "status": g["status"]} for g in report.get("gates", [])],
            "executable_failure": report.get("executable_failure"),
            "numeraire_alias": m.numeraire_alias,
            "numeraire_decimals": m.numeraire_decimals,
            "token_behavior_basis": str(m.data.token_behavior_basis),
            "availability_basis": str(basis_for(str(m.origin))),
            "prehistory_ms": (m.period.start_utc_ms - m.period.prehistory_start_utc_ms) if m.period.prehistory_start_utc_ms else 0,
            "latency_assumptions": pack.params.public_latency_assumptions(),
            "universe": m.universe.model_dump(mode="json"),
            "rights": m.rights.model_dump(),
            "unsupported_inventory": pack.inventory.get("unsupported", []) if pack.inventory else [],
            "provenance_notes": m.provenance_notes,
            "scenario": (m.generator or {}).get("scenario_description"),
        }

    def load_pack(self, pack_ref: str) -> tuple[dict[str, Any], Pack]:
        row = self.store.pack(pack_ref)
        if row is None:
            raise ApiError(404, f"unknown pack {pack_ref}", "NOT_FOUND")
        with self._global:
            pack = self._packs.get(row["pack_id"])
            if pack is None:
                pack = Pack.load(Path(row["path"]))
                self._packs[row["pack_id"]] = pack
        return row, pack

    def pack_row(self, pack_ref: str) -> dict[str, Any]:
        row = self.store.pack(pack_ref)
        if row is None:
            raise ApiError(404, f"unknown pack {pack_ref}", "NOT_FOUND")
        return self._pack_view(row)

    def _pack_view(self, row: dict[str, Any]) -> dict[str, Any]:
        summary = json.loads(row["summary_json"])
        sealed_only = any(row["name"] in s.packs and s.sealed for s in self.suites.values()) and not any(row["name"] in s.packs and not s.sealed for s in self.suites.values())
        view = {
            "pack_id": row["pack_id"],
            "episode_id": row["episode_id"],
            "name": row["name"],
            "origin": row["origin"],
            "chain": row["chain"],
            "scope_label": row["scope_label"],
            "use_status": row["use_status"],
            "duration_ms": row["duration_ms"],
            "is_full_week": bool(row["is_full_week"]),
            "execution_model": row["execution_model"],
            "imported_at": row["imported_at"],
            "runnable": row["use_status"] in ("demo", "research", "qualified_for_named_suite") and summary.get("pools_executable", 0) > 0,
            "diagnostic_only": row["use_status"] in ("diagnostic_only",),
            "summary": summary,
            "supported_actions": sorted(TOOLS) if summary.get("pools_executable", 0) > 0 else [t for t in TOOLS if not t.startswith("broker.")],
            "unsupported_capabilities": UNSUPPORTED_CAPABILITIES,
            "predictive_validity": "not_established",
        }
        if self.dev_mode and not sealed_only:
            view["period_dev_mode"] = {"start_utc": row["start_utc"], "end_utc": row["end_utc"], "note": "actual dates shown in development mode only"}
        return view

    def packs(self) -> list[dict[str, Any]]:
        return [self._pack_view(r) for r in self.store.packs()]

    def public_descriptor(self, pack: Pack, row: dict[str, Any], isolation: str) -> PublicDescriptor:
        m = pack.manifest
        supported = any(p.supported_by_cpmm and p.initial_reserve0 is not None for p in pack.pools.values())
        return PublicDescriptor(
            episode_id=row["episode_id"],
            origin=m.origin,
            chain=m.chain,
            duration_ms=m.period.duration_ms,
            is_full_week=m.period.is_full_week,
            prehistory_ms=(m.period.start_utc_ms - m.period.prehistory_start_utc_ms) if m.period.prehistory_start_utc_ms else 0,
            numeraire_alias=m.numeraire_alias,
            numeraire_decimals=m.numeraire_decimals,
            execution_model=m.execution.model,
            token_behavior_basis=m.data.token_behavior_basis,
            availability_basis=basis_for(str(m.origin)),
            latency_assumptions=pack.params.public_latency_assumptions(),
            capabilities=sorted(TOOLS) if supported else [t for t in TOOLS if not t.startswith("broker.")],
            unsupported_capabilities=UNSUPPORTED_CAPABILITIES,
            limitations=["blinded interface, not contamination-proof", "fixed external flow counterfactual", *m.provenance_notes],
            use_status=m.validation.qualification,
            isolation=Isolation(isolation),
        )

    def pack_health(self, pack_ref: str) -> dict[str, Any]:
        row, pack = self.load_pack(pack_ref)
        m = pack.manifest
        cov = pack.coverage or {}
        intervals = cov.get("intervals", [])
        gaps = [i for i in intervals if i.get("state") != "completed_and_checked"]
        # duplicates & ordering diagnostics on the tape
        seen = set()
        dups = 0
        for r in pack.tape:
            k = (r["block"], r["log_index"], r.get("tx"))
            if k in seen and r["kind"] == "swap":
                dups += 1
            seen.add(k)
        unpublished = sum(1 for r in pack.tape if r["kind"] == "swap" and r.get("available_utc_ms") is None)
        conflicts = [r.model_dump() for r in pack.restrictions if r.conflicts]
        pools_missing_state = [p.key for p in pack.pools.values() if p.supported_by_cpmm and p.initial_reserve0 is None]
        attempts = self.store.query("SELECT * FROM attempts WHERE pack_id=?", (m.pack_id,))
        exposed = self.store.query("SELECT COUNT(*) AS n FROM runs WHERE pack_id=? AND exposed=1", (m.pack_id,))[0]["n"]
        return {
            "pack": self._pack_view(row),
            "universe": m.universe.model_dump(mode="json"),
            "ingestion": {"tape_events": len(pack.tape), "blocks_table_rows": len(pack.blocks), "indexed_block_ranges": m.universe.indexed_block_ranges},
            "coverage": {"intervals": len(intervals), "non_complete_intervals": gaps[:200], "non_complete_count": len(gaps)},
            "duplicates_suspected": dups,
            "unpublished_observations": unpublished,
            "corrections": cov.get("corrections", []),
            "missing_state_pools": pools_missing_state,
            "unsupported_inventory": pack.inventory.get("unsupported", []) if pack.inventory else [],
            "missing_inventory": pack.inventory.get("missing", []) if pack.inventory else [],
            "source_disagreements": conflicts,
            "restriction_observations": len(pack.restrictions),
            "token_behavior_basis": str(m.data.token_behavior_basis),
            "rights": m.rights.model_dump(),
            "validation": pack.validation,
            "attempts": attempts,
            "exposed_runs": exposed,
            "decision_log": m.decision_log,
        }

    # ------------------------------------------------------------------ agents
    def register_agent(self, *, name: str, version: str, runtime: str, capabilities: list[str], config: dict[str, Any]) -> dict[str, Any]:
        if not name or not version:
            raise ApiError(400, "name and version are required", "INVALID")
        if self.store.agent_by_name_version(name, version):
            raise ApiError(409, "agent name/version already registered (versions are immutable)", "CONFLICT")
        fp = hashlib.sha256(json.dumps({"name": name, "version": version, "runtime": runtime, "config": config}, sort_keys=True).encode()).hexdigest()
        agent_id = "agent_" + fp[:16]
        row = {
            "agent_id": agent_id,
            "name": name,
            "version": version,
            "runtime": runtime,
            "fingerprint": fp,
            "capabilities_json": json.dumps(sorted(capabilities)),
            "config_json": json.dumps(config, sort_keys=True),
            "created_at": now_iso(),
        }
        self.store.insert_agent(row)
        return self.agent_view(agent_id)

    def agent_view(self, agent_id: str) -> dict[str, Any]:
        row = self.store.agent(agent_id)
        if row is None:
            raise ApiError(404, f"unknown agent {agent_id}", "NOT_FOUND")
        caps = json.loads(row["capabilities_json"])
        required = set(caps)
        incompatible = sorted(c for c in required if c in UNSUPPORTED_CAPABILITIES)
        return {
            "agent_id": row["agent_id"],
            "name": row["name"],
            "version": row["version"],
            "runtime": row["runtime"],
            "fingerprint": row["fingerprint"],
            "capabilities": caps,
            "config": json.loads(row["config_json"]),
            "created_at": row["created_at"],
            "compatibility": {"compatible": not incompatible, "unsupported_requested": incompatible},
        }

    def agents(self) -> list[dict[str, Any]]:
        return [self.agent_view(r["agent_id"]) for r in self.store.agents()]

    # ------------------------------------------------------------------ runs
    def create_run(
        self,
        *,
        agent_id: str,
        pack_ref: str,
        mode: str = "practice",
        bankroll_raw: str | int = "1000000",
        mask_seed: str | None = None,
        engine_seed: str | None = None,
        isolation: str = "trusted_external_client",
        launch_spec: dict[str, Any] | None = None,
        suite_id: str | None = None,
        suite_run_id: str | None = None,
        agent_seed: str | None = None,
    ) -> dict[str, Any]:
        agent = self.agent_view(agent_id)
        if not agent["compatibility"]["compatible"]:
            raise ApiError(400, f"agent requests unsupported capabilities: {agent['compatibility']['unsupported_requested']}", "INCOMPATIBLE")
        row, pack = self.load_pack(pack_ref)
        if row["use_status"] not in ("demo", "research", "qualified_for_named_suite"):
            raise ApiError(400, f"pack use status '{row['use_status']}' is not runnable; see its validation report", "NOT_RUNNABLE")
        if mode not in ("practice", "sealed"):
            raise ApiError(400, "mode must be practice or sealed", "INVALID")
        if isolation not in ("trusted_external_client", "restricted_local_runner"):
            raise ApiError(400, "invalid isolation", "INVALID")
        try:
            bankroll = int(str(bankroll_raw))
        except ValueError as e:
            raise ApiError(400, "bankroll_raw must be an integer string", "INVALID") from e
        if bankroll <= 0:
            raise ApiError(400, "bankroll must be positive", "INVALID")
        run_id = "run_" + secrets.token_hex(8)
        mask_seed = mask_seed or ("mask-" + secrets.token_hex(8))
        engine_seed = engine_seed or ("engine-" + secrets.token_hex(8))
        session = Session.create(session_id="ses_" + run_id[4:], pack=pack, bankroll_raw=bankroll, mask_seed=mask_seed, engine_seed=engine_seed, mode=mode)
        token = new_agent_token()
        attempt = self.store.bump_attempt(row["pack_id"], agent_id)
        run_row = {
            "run_id": run_id,
            "agent_id": agent_id,
            "pack_id": row["pack_id"],
            "suite_id": suite_id,
            "suite_run_id": suite_run_id,
            "mode": mode,
            "isolation": isolation,
            "state": str(RunState.QUEUED),
            "bankroll_raw": str(bankroll),
            "mask_seed": mask_seed,
            "engine_seed": engine_seed,
            "profile_hash": pack.manifest.execution.parameters_hash,
            "token_hash": token_hash(token),
            "launch_json": json.dumps(launch_spec) if launch_spec else None,
            "created_at": now_iso(),
            "clock_ms": 0,
        }
        self.store.insert_run(run_row)
        run_dir = self.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "run_manifest.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "agent": agent,
                    "pack_id": row["pack_id"],
                    "episode_id": row["episode_id"],
                    "mode": mode,
                    "isolation": isolation,
                    "bankroll_raw": str(bankroll),
                    "mask_seed": mask_seed,
                    "engine_seed": engine_seed,
                    "agent_seed": agent_seed,
                    "profile_hash": pack.manifest.execution.parameters_hash,
                    "execution_profile": pack.params.profile_name,
                    "budgets": pack.params.budgets.model_dump(),
                    "engine_version": ENGINE_VERSION,
                    "attempt_number": attempt,
                    "created_at": run_row["created_at"],
                },
                indent=2,
                sort_keys=True,
            )
        )
        ctx = RunContext(run_id=run_id, session=session, lock=threading.RLock(), token=token, trace_path=run_dir / "trace.jsonl", started_wall=time.time(), launch=launch_spec)
        with self._global:
            self._contexts[run_id] = ctx
        result = self.run_view(run_id)
        if launch_spec:
            self._launch(ctx, launch_spec, isolation, agent_seed)
            result = self.run_view(run_id)
        else:
            # The credential is returned only to the caller who created the run (the participant or its runner).
            result["session_credential"] = {"token": token, "gateway_url": self.gateway_url, "commands_url": self.gateway_url + "/agent/v1/commands"}
        return result

    def _launch(self, ctx: RunContext, spec: dict[str, Any], isolation: str, agent_seed: str | None) -> None:
        name = spec.get("name")
        runtime = spec.get("runtime", "python")
        extra = list(spec.get("args", []))
        ls = LaunchSpec(name=str(name), runtime=str(runtime), extra_args=[str(a) for a in extra])
        log_path = self.runs_dir / ctx.run_id / "agent.log"
        try:
            if isolation == "restricted_local_runner":
                proc, controls, _wd = launch_restricted(ls, gateway_url=self.gateway_url, token=ctx.token, agent_seed=agent_seed, log_path=log_path)
                ctx.controls = controls.as_dict()
            else:
                proc = launch(ls, gateway_url=self.gateway_url, token=ctx.token, agent_seed=agent_seed, log_path=log_path)
                ctx.controls = {"isolation": "trusted_external_client", "enforced": [], "unenforced": ["network", "filesystem", "memory_across_runs"]}
        except (ValueError, OSError) as e:
            self.store.update_run(ctx.run_id, state=str(RunState.ENVIRONMENT_FAILED), error=f"launch failed: {e}", finished_at=now_iso())
            return
        ctx.proc = proc
        self.store.update_run(ctx.run_id, state=str(RunState.RUNNING), started_at=now_iso())

        def monitor() -> None:
            rc = proc.wait()
            with ctx.lock:
                row = self.store.run(ctx.run_id)
                if row and row["state"] in (str(RunState.RUNNING), str(RunState.QUEUED), str(RunState.PAUSED), str(RunState.BUDGET_EXHAUSTED)):
                    if ctx.session.finished:
                        self._finalize(ctx, RunState.COMPLETED if row["state"] != str(RunState.BUDGET_EXHAUSTED) else RunState.BUDGET_EXHAUSTED)
                    else:
                        self._finalize(ctx, RunState.AGENT_FAILED, error=f"agent process exited with code {rc} before session.finish")

        t = threading.Thread(target=monitor, name=f"monitor-{ctx.run_id}", daemon=True)
        ctx.monitor = t
        t.start()

    def wait_for_run(self, run_id: str, timeout: float | None = None) -> dict[str, Any]:
        ctx = self._contexts.get(run_id)
        if ctx and ctx.monitor:
            ctx.monitor.join(timeout)
        return self.run_view(run_id)

    def _ctx_for_token(self, token: str) -> RunContext:
        row = self.store.run_by_token_hash(token_hash(token))
        if row is None:
            raise ApiError(401, "invalid credential", "UNAUTHORIZED")
        ctx = self._contexts.get(row["run_id"])
        if ctx is None:
            raise ApiError(410, "run is no longer active", "GONE")
        return ctx

    def handle_command(self, token: str, request_id: str, tool: str, arguments: dict[str, Any] | None, session_id: str | None = None) -> Envelope:
        ctx = self._ctx_for_token(token)
        if session_id is not None and session_id != ctx.session.session_id:
            raise ApiError(403, "session_id does not match the credential", "FORBIDDEN")
        with ctx.lock:
            row = self.store.run(ctx.run_id)
            assert row is not None
            state = row["state"]
            if state in (str(RunState.ABORTED), str(RunState.COMPLETED), str(RunState.AGENT_FAILED), str(RunState.ENVIRONMENT_FAILED)):
                return Envelope.fail(request_id=request_id, session_id=ctx.session.session_id, clock_ms=ctx.session.now, code=ErrorCode.SESSION_FINISHED, message=f"run is {state}")
            if state == str(RunState.QUEUED):
                self.store.update_run(ctx.run_id, state=str(RunState.RUNNING), started_at=now_iso())
            try:
                env = ctx.session.handle(request_id, tool, arguments)
            except Exception as e:  # environment failure, never blamed on the agent
                tb = traceback.format_exc(limit=5)
                self._finalize(ctx, RunState.ENVIRONMENT_FAILED, error=f"{type(e).__name__}: {e}\n{tb}")
                return Envelope.fail(request_id=request_id, session_id=ctx.session.session_id, clock_ms=ctx.session.now, code=ErrorCode.ENVIRONMENT_FIDELITY_LIMIT, message="environment failure; run marked environment_failed")
            self._append_trace(ctx)
            updates: dict[str, Any] = {"clock_ms": ctx.session.now}
            if ctx.session.budget.exhausted and state != str(RunState.BUDGET_EXHAUSTED):
                updates["state"] = str(RunState.BUDGET_EXHAUSTED)
            self.store.update_run(ctx.run_id, **updates)
            if tool == "session.finish" and env.status == "ok" and not ctx.launch:
                final_state = RunState.BUDGET_EXHAUSTED if ctx.session.budget.exhausted else RunState.COMPLETED
                self._finalize(ctx, final_state)
            elif tool == "session.finish" and env.status == "ok" and ctx.launch:
                # Launched agents: the monitor thread finalizes when the process exits, but produce the report now.
                final_state = RunState.BUDGET_EXHAUSTED if ctx.session.budget.exhausted else RunState.COMPLETED
                self._finalize(ctx, final_state)
            return env

    def _append_trace(self, ctx: RunContext) -> None:
        recs = ctx.session.trace[ctx.trace_written :]
        if not recs:
            return
        with ctx.trace_path.open("a", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps({"index": r.index, "request_id": r.request_id, "tool": r.tool, "arguments": r.arguments, "clock_before_ms": r.clock_before_ms, "clock_after_ms": r.clock_after_ms, "status": r.status, "error_code": r.error_code}, sort_keys=True))
                f.write("\n")
        ctx.trace_written = len(ctx.session.trace)

    def _run_meta(self, ctx: RunContext, row: dict[str, Any]) -> dict[str, Any]:
        manifest = json.loads((self.runs_dir / ctx.run_id / "run_manifest.json").read_text())
        wall = {"started_at": row.get("started_at") or row.get("created_at"), "elapsed_s": round(time.time() - ctx.started_wall, 3)}
        return {
            "run_id": ctx.run_id,
            "agent": manifest["agent"],
            "episode_id": manifest["episode_id"],
            "mode": row["mode"],
            "isolation": row["isolation"],
            "isolation_controls": ctx.controls,
            "attempt_number": manifest["attempt_number"],
            "state": row["state"],
            "error": row.get("error"),
            "clock_ms": ctx.session.now,
            "pack_id": row["pack_id"],
            "mask_seed": row["mask_seed"],
            "engine_seed": row["engine_seed"],
            "agent_seed": manifest.get("agent_seed"),
            "profile_hash": row["profile_hash"],
            "wall_clock": wall,
            "inference": ctx.inference,
        }

    def _finalize(self, ctx: RunContext, state: RunState, error: str | None = None) -> None:
        if ctx.finalized and state not in (RunState.ABORTED,):
            return
        self._append_trace(ctx)
        row = self.store.run(ctx.run_id)
        assert row is not None
        row = dict(row)
        row["state"] = str(state)
        if error:
            row["error"] = error
        try:
            report = build_report(ctx.session, run_meta=self._run_meta(ctx, row), role="admin")
        except Exception as e:  # pragma: no cover - defensive
            report = {"error": f"report generation failed: {e}"}
            state = RunState.ENVIRONMENT_FAILED
            error = (error or "") + f" report failure: {e}"
        (self.runs_dir / ctx.run_id / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True))
        self.store.update_run(ctx.run_id, state=str(state), error=error, finished_at=now_iso(), clock_ms=ctx.session.now, report_json=json.dumps(report, sort_keys=True))
        ctx.finalized = True

    def pause(self, run_id: str) -> dict[str, Any]:
        ctx = self._active(run_id)
        with ctx.lock:
            ctx.session.paused = True
            self.store.update_run(run_id, state=str(RunState.PAUSED))
        return self.run_view(run_id)

    def resume(self, run_id: str) -> dict[str, Any]:
        ctx = self._active(run_id)
        with ctx.lock:
            ctx.session.paused = False
            self.store.update_run(run_id, state=str(RunState.RUNNING))
        return self.run_view(run_id)

    def abort(self, run_id: str) -> dict[str, Any]:
        ctx = self._active(run_id)
        with ctx.lock:
            if ctx.proc is not None and ctx.proc.poll() is None:
                ctx.proc.terminate()
            self._finalize(ctx, RunState.ABORTED, error="aborted by operator")
        return self.run_view(run_id)

    def _active(self, run_id: str) -> RunContext:
        ctx = self._contexts.get(run_id)
        if ctx is None:
            raise ApiError(404, "run not active in this process", "NOT_FOUND")
        return ctx

    def run_view(self, run_id: str) -> dict[str, Any]:
        row = self.store.run(run_id)
        if row is None:
            raise ApiError(404, f"unknown run {run_id}", "NOT_FOUND")
        ctx = self._contexts.get(run_id)
        pack_row = self.store.pack(row["pack_id"])
        view = {
            "run_id": row["run_id"],
            "agent_id": row["agent_id"],
            "pack_id": row["pack_id"],
            "episode_id": pack_row["episode_id"] if pack_row else None,
            "pack_name": pack_row["name"] if pack_row else None,
            "suite_id": row["suite_id"],
            "suite_run_id": row["suite_run_id"],
            "mode": row["mode"],
            "isolation": row["isolation"],
            "state": row["state"],
            "bankroll_raw": row["bankroll_raw"],
            "profile_hash": row["profile_hash"],
            "launch": json.loads(row["launch_json"]) if row["launch_json"] else None,
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "error": row["error"],
            "clock_ms": row["clock_ms"],
            "exposed": bool(row["exposed"]),
            "has_report": row["report_json"] is not None,
        }
        if ctx is not None:
            s = ctx.session
            v = s.sim.value_portfolio()
            view["live"] = {
                "clock_ms": s.now,
                "remaining_ms": max(0, s.sim.end_ms - s.now),
                "duration_ms": s.sim.duration_ms,
                "requests": s.budget.requests,
                "decisions": s.budget.decisions,
                "orders_total": len(s.sim.orders),
                "pending_orders": len(s.sim.unresolved_orders()),
                "cash_available_raw": str(v.cash_available),
                "cash_reserved_raw": str(v.cash_reserved),
                "model_equity_raw": None if v.equity is None else str(v.equity),
                "valuation_complete": v.complete,
                "holdings": [{"asset_id": s.alias.asset(h["asset"]), "class": h["class"], "quantity_raw": str(h["quantity"]), "model_value_raw": str(h["value"])} for h in v.holdings],
                "recent_actions": [{"tool": r.tool, "status": r.status, "error_code": r.error_code, "clock_ms": r.clock_after_ms} for r in s.trace[-15:]],
                "fidelity_flags": len(s.sim.fidelity_flags),
                "data_health": {"rate_limited": s.budget.rate_limited, "invalid_calls": s.budget.invalid_calls, "budget_exhausted": s.budget.exhausted},
                "isolation_controls": ctx.controls,
            }
        return view

    def runs(self, **where: Any) -> list[dict[str, Any]]:
        return [self.run_view(r["run_id"]) for r in self.store.runs(**where)]

    def report(self, run_id: str, role: str = "admin") -> dict[str, Any]:
        row = self.store.run(run_id)
        if row is None:
            raise ApiError(404, f"unknown run {run_id}", "NOT_FOUND")
        ctx = self._contexts.get(run_id)
        if row["report_json"]:
            report = json.loads(row["report_json"])
        elif ctx is not None:
            report = build_report(ctx.session, run_meta=self._run_meta(ctx, dict(row)), role="admin")
            report["provisional"] = True
        else:
            raise ApiError(404, "no report available", "NOT_FOUND")
        if role != "admin":
            scanner = ctx.session.scanner if ctx else None
            report = redact_for_role(report, role, scanner)
            if "versions" in report:
                report["versions"]["pack_id"] = "hidden"
            report.get("run", {}).pop("mask_seed", None)
            report.get("run", {}).pop("engine_seed", None)
            report.get("run", {}).pop("pack_id", None)
        return report

    def observed(self, run_id: str, pool_id: str | None = None, interval_ms: int = 60_000) -> dict[str, Any]:
        """Agent-visible view for the Run screen: only observations available at the current clock."""
        ctx = self._active(run_id)
        s = ctx.session
        with ctx.lock:
            discovered = s.sim.discovered_pools(s.now)
            pools = [{"pool_id": s.alias.pool(k)} for k in discovered]
            series: dict[str, Any] | None = None
            if pool_id or pools:
                pid = pool_id or pools[0]["pool_id"]
                r = s.alias.resolve(pid)
                if r and r[0] == "pool" and r[1] in discovered:
                    key = r[1]
                    from ..observations.store import aggregate_candles

                    base, quote = s._base_quote(key)
                    end = s.now
                    start = max(end - interval_ms * 400, -(10**12))
                    candles, gaps = aggregate_candles(s.sim.obs[key], base_asset=base, interval_ms=interval_ms, start_ms=start, end_ms=end, as_of=s.now, availability_delay_ms=s.params.availability_delay_ms, include_partial=False)
                    from fractions import Fraction

                    from ..domain.quantities import fraction_to_decimal_str

                    scale = Fraction(10 ** s._decimals(base), 10 ** s._decimals(quote))
                    items = []
                    for c in candles:
                        d = c.to_public()
                        for f in ("open", "high", "low", "close"):
                            v = getattr(c, f)
                            d[f] = None if v is None else fraction_to_decimal_str(v * scale, 18)
                        items.append(d)
                    series = {"pool_id": pid, "interval_ms": interval_ms, "items": items, "gaps": gaps, "as_of_ms": s.now}
            equity = [{"time_ms": p.time_ms, "equity_raw": None if p.equity is None else str(p.equity), "complete": p.complete, "source": p.source} for p in s.sim.equity_points]
            orders = [o.to_public(s.alias) for o in list(s.sim.orders.values())[-50:]]
        return {"clock_ms": s.now, "pools": pools, "series": series, "equity": equity, "orders": orders, "note": "Only observations with available_ms <= clock are shown; no future data."}

    def export(self, run_id: str, role: str = "admin", include_mappings: bool = False) -> dict[str, Any]:
        row = self.store.run(run_id)
        if row is None:
            raise ApiError(404, f"unknown run {run_id}", "NOT_FOUND")
        run_dir = self.runs_dir / run_id
        manifest = json.loads((run_dir / "run_manifest.json").read_text()) if (run_dir / "run_manifest.json").exists() else {}
        trace = []
        if (run_dir / "trace.jsonl").exists():
            trace = [json.loads(l) for l in (run_dir / "trace.jsonl").read_text().splitlines() if l.strip()]
        report = self.report(run_id, role)
        pack_row, pack = self.load_pack(row["pack_id"])
        ctx = self._contexts.get(run_id)
        bundle: dict[str, Any] = {
            "bundle_version": "run_export_v1",
            "role": role,
            "run": self.run_view(run_id),
            "public_descriptor": self.public_descriptor(pack, pack_row, row["isolation"]).model_dump(mode="json"),
            "report": report,
            "trace": trace,
        }
        if role == "admin":
            bundle["run_manifest"] = manifest
            if ctx is not None:
                bundle["ledger"] = [e.to_public() for e in ctx.session.sim.ledger.entries]
                bundle["orders"] = [o.to_public(ctx.session.alias) for o in ctx.session.sim.orders.values()]
                bundle["equity_points"] = [{"time_ms": p.time_ms, "equity_raw": None if p.equity is None else str(p.equity), "complete": p.complete, "source": p.source} for p in ctx.session.sim.equity_points]
                if include_mappings:
                    bundle["alias_mappings"] = {a: {"kind": k, "canonical": c} for a, (k, c) in ctx.session.alias.known_aliases().items()}
                    if row["mode"] == "practice":
                        self.store.update_run(run_id, exposed=1)
                        bundle["run"]["exposed"] = True
            bundle["private_period"] = {"start_utc": pack_row["start_utc"], "end_utc": pack_row["end_utc"]} if row["mode"] == "practice" else "sealed"
        else:
            bundle["run"] = {k: v for k, v in bundle["run"].items() if k not in ("pack_id", "pack_name")}
            if ctx is not None:
                bundle["trace"] = redact_for_role(trace, role, ctx.session.scanner)
        bundle["bundle_hash"] = hashlib.sha256(json.dumps({k: v for k, v in bundle.items() if k != "run"}, sort_keys=True, default=str).encode()).hexdigest()
        return bundle

    def replay(self, run_id: str) -> dict[str, Any]:
        """Re-execute the recorded action trace on a fresh session and compare hashes (Acceptance test 37)."""
        row = self.store.run(run_id)
        if row is None:
            raise ApiError(404, f"unknown run {run_id}", "NOT_FOUND")
        _pack_row, pack = self.load_pack(row["pack_id"])
        run_dir = self.runs_dir / run_id
        trace = [json.loads(l) for l in (run_dir / "trace.jsonl").read_text().splitlines() if l.strip()] if (run_dir / "trace.jsonl").exists() else []
        replayed = replay_trace(pack, trace, bankroll_raw=int(row["bankroll_raw"]), mask_seed=row["mask_seed"], engine_seed=row["engine_seed"], mode=row["mode"])
        original = json.loads(row["report_json"]) if row["report_json"] else None
        orig_hashes = (original or {}).get("reproducibility", {})
        result = {
            "run_id": run_id,
            "trace_length": len(trace),
            "replayed_ledger_hash": replayed.sim.ledger.content_hash(),
            "replayed_state_hash": replayed.sim.state_hash(),
            "replayed_result_hash": replayed.result_hash(),
            "original": orig_hashes,
            "ledger_matches": orig_hashes.get("ledger_hash") == replayed.sim.ledger.content_hash(),
            "state_matches": orig_hashes.get("state_hash") == replayed.sim.state_hash(),
        }
        return result

    # ------------------------------------------------------------------ suites
    def suites_view(self) -> list[dict[str, Any]]:
        out = []
        for s in self.suites.values():
            ids = []
            for name in s.packs:
                r = self.store.pack(name)
                ids.append(r["pack_id"] if r else None)
            out.append(suite_public(s, [i for i in ids if i]) | {"all_packs_imported": all(ids)})
        return out

    def run_suite(self, suite_id: str, *, agent_id: str, launch_spec: dict[str, Any], isolation: str | None = None, wait: bool = True, agent_seed: str | None = None) -> dict[str, Any]:
        s = self.suites.get(suite_id)
        if s is None:
            raise ApiError(404, f"unknown suite {suite_id}", "NOT_FOUND")
        suite_run_id = "srun_" + secrets.token_hex(6)
        run_ids = []
        for name in s.packs:
            r = self.store.pack(name)
            if r is None:
                raise ApiError(400, f"suite pack {name} is not imported", "NOT_IMPORTED")
            view = self.create_run(
                agent_id=agent_id,
                pack_ref=r["pack_id"],
                mode=s.mode,
                bankroll_raw=s.bankroll_raw,
                mask_seed=f"{s.mask_seed}:{name}",
                engine_seed=f"{s.engine_seed}:{name}",
                isolation=isolation or s.isolation,
                launch_spec=launch_spec,
                suite_id=suite_id,
                suite_run_id=suite_run_id,
                agent_seed=agent_seed,
            )
            run_ids.append(view["run_id"])
            if wait:
                self.wait_for_run(view["run_id"])
        self.store.insert_suite_run(suite_run_id, suite_id, agent_id, now_iso(), run_ids)
        return {"suite_run_id": suite_run_id, "suite_id": suite_id, "agent_id": agent_id, "run_ids": run_ids, "runs": [self.run_view(r) for r in run_ids]}

    # ------------------------------------------------------------------ comparisons / studies
    def compare(self, *, suite_id: str | None, agent_a: str, agent_b: str, run_ids_a: list[str] | None = None, run_ids_b: list[str] | None = None) -> dict[str, Any]:
        def collect(agent_id: str, explicit: list[str] | None) -> list[dict[str, Any]]:
            rows = [self.store.run(r) for r in explicit] if explicit else self.store.runs(agent_id=agent_id, **({"suite_id": suite_id} if suite_id else {}))
            out = []
            for r in rows:
                if r is None:
                    continue
                pack_row = self.store.pack(r["pack_id"])
                agent = self.agent_view(r["agent_id"])
                out.append(
                    {
                        "run_id": r["run_id"],
                        "pack_id": r["pack_id"],
                        "episode_label": pack_row["name"] if pack_row else r["pack_id"],
                        "chain": pack_row["chain"] if pack_row else None,
                        "state": r["state"],
                        "agent_version": f"{agent['name']}@{agent['version']}",
                        "report": json.loads(r["report_json"]) if r["report_json"] else None,
                        "profile_hash": r["profile_hash"],
                        "mask_seed": r["mask_seed"],
                        "capabilities": agent["capabilities"],
                        "error": r["error"],
                    }
                )
            return out

        ra = collect(agent_a, run_ids_a)
        rb = collect(agent_b, run_ids_b)
        if not ra or not rb:
            raise ApiError(400, "no runs found for one of the agents on this suite", "NO_RUNS")
        result = pair_runs(ra, rb)
        result["agents"] = {"a": self.agent_view(agent_a), "b": self.agent_view(agent_b)}
        result["suite_id"] = suite_id
        result["suite_fingerprint"] = self.suites[suite_id].fingerprint(sorted({r["pack_id"] for r in ra + rb})) if suite_id and suite_id in self.suites else None
        comparison_id = "cmp_" + secrets.token_hex(6)
        result["comparison_id"] = comparison_id
        result["created_at"] = now_iso()
        self.store.insert_comparison(comparison_id, result["created_at"], {"suite_id": suite_id, "agent_a": agent_a, "agent_b": agent_b, "run_ids_a": run_ids_a, "run_ids_b": run_ids_b}, result)
        return result

    def comparisons(self) -> list[dict[str, Any]]:
        return [json.loads(r["result_json"]) for r in self.store.comparisons()]

    def comparison(self, comparison_id: str) -> dict[str, Any]:
        r = self.store.comparison(comparison_id)
        if r is None:
            raise ApiError(404, "unknown comparison", "NOT_FOUND")
        return json.loads(r["result_json"])

    def register_study(self, *, candidates: list[str], pack_ids: list[str], comparison_id: str | None, intended_outcome: str, prediction: str) -> dict[str, Any]:
        rec = new_study(candidates=candidates, evaluator_version=ENGINE_VERSION, pack_ids=pack_ids, comparison_id=comparison_id, intended_outcome=intended_outcome, prediction=prediction, registered_at_utc=now_iso())
        self.store.upsert_study(rec["study_id"], rec["registered_at_utc"], rec)
        return rec

    def attach_study_outcome(self, study_id: str, *, description: str, data: dict[str, Any]) -> dict[str, Any]:
        r = self.store.study(study_id)
        if r is None:
            raise ApiError(404, "unknown study", "NOT_FOUND")
        rec = attach_outcome(json.loads(r["record_json"]), observed_at_utc=now_iso(), description=description, data=data)
        self.store.upsert_study(study_id, r["created_at"], rec)
        return rec

    def studies(self) -> list[dict[str, Any]]:
        return [json.loads(r["record_json"]) for r in self.store.studies()]

    def close(self) -> None:
        for ctx in list(self._contexts.values()):
            if ctx.proc is not None and ctx.proc.poll() is None:
                ctx.proc.terminate()
        self.store.close()
