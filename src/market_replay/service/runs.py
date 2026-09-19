"""Run manager: sessions, credentials, launchers, reports.

Durability model (works for a long-lived local server and for serverless instances alike):
the store holds every run's append-only command trace, its manifest, report and agent log.
A live ``Session`` is only a cache. When a request lands on an instance that has no cache
for the run, the session is rebuilt by deterministic replay of the stored trace, which the
replay tests already prove reproduces the ledger and state exactly. Commands of one run are
serialized by a store-level lock, so two instances can never interleave.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from ..datasets.baseline import baseline_sentence, read_market_baseline
from ..datasets.generator import dev_short_config, generate_pack, standard_suite_configs
from ..datasets.pack import Pack, PackError
from ..datasets.validator import validate_pack
from ..domain.envelope import Envelope
from ..domain.models import PublicDescriptor
from ..domain.profiles import PROFILES
from ..domain.status import ErrorCode, Isolation, RunState, UseStatus
from ..engine.session import TOOLS, UNSUPPORTED_CAPABILITIES, Session, replay_trace
from ..evaluation.compare import pair_runs
from ..evaluation.report import ENGINE_VERSION, build_report
from ..evaluation.study_registry import attach_outcome, new_study
from ..evaluation.validity import ranking_eligible
from ..observations.masking import redact_for_role
from ..observations.store import basis_for
from ..runners.inprocess import run_example_inprocess
from ..runners.restricted import launch_restricted
from ..runners.trusted import PY_EXAMPLES, TS_EXAMPLES, LaunchSpec, launch
from .auth import new_agent_token, new_identity_token, token_hash
from .db import BaseStore, RunBusy, open_store
from .suites import SuiteDef, default_suites_text, load_suites, suite_public

TERMINAL = {str(RunState.ABORTED), str(RunState.COMPLETED), str(RunState.AGENT_FAILED), str(RunState.ENVIRONMENT_FAILED)}
FIXTURE_CONFIGS = {c.name: c for c in [dev_short_config(), *standard_suite_configs()]}
# Recorded weeks live in the repository, one pack directory each (see docs/how-a-real-week-is-built.md).
WEEKS_DIR = Path(os.environ.get("MARKET_REPLAY_WEEKS_DIR") or (Path(__file__).resolve().parents[3] / "weeks"))
VENUE_NAMES = {"uniswap_v2": "v2 pairs", "uniswap_v3": "v3 pools", "uniswap_v4": "v4 pools"}
LOG = logging.getLogger("market_replay.sessions")


def week_label(name: str, chain: str, protocol: str | None = None, start_utc: str | None = None, end_utc: str | None = None) -> str:
    """Real data is labelled by its calendar: 'Base day of 2026-09-08', 'Base week of 2026-09-07' (any
    other period: 'Base 2026-09-04 (1h)'), plus the venue for a single-venue pack ('..., v4 pools')."""
    parts = name.split("_")
    venue = VENUE_NAMES.get(protocol or "", "")
    suffix = f", {venue}" if venue and protocol != "uniswap_v2" else ""
    head = chain.capitalize()
    if start_utc:
        day = str(start_utc)[:10]
        if len(parts) >= 3 and parts[1] in ("week", "day"):
            return f"{head} {parts[1]} of {day}{suffix}"
        hours = parts[3] if len(parts) >= 4 and parts[1] == "period" else None
        return f"{head} {day} ({hours}){suffix}" if hours else f"{head} {day}{suffix}"
    return f"{head}: {name}{suffix}"


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
    token_hash: str
    started_wall: float
    launch: dict[str, Any] | None = None
    proc: Any = None
    monitor: threading.Thread | None = None
    controls: dict[str, Any] | None = None
    trace_written: int = 0
    inference: dict[str, Any] = field(default_factory=lambda: {"recorded": False})


class RunManager:
    def __init__(
        self,
        *,
        data_dir: Path,
        suites_path: Path | None = None,
        dev_mode: bool = True,
        store_url: str | None = None,
        max_runs_per_day: int | None = None,
        max_cpu_seconds_per_month: float | None = None,
        runtimes_available: tuple[str, ...] | None = None,
        hosted: bool = False,
        max_runs_per_hour_per_ip: int | None = None,
    ) -> None:
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.store: BaseStore = open_store(store_url or str(data_dir / "control_plane.sqlite"))
        self.dev_mode = dev_mode
        self.hosted = hosted
        self.max_runs_per_day = int(max_runs_per_day if max_runs_per_day is not None else os.environ.get("MARKET_REPLAY_MAX_RUNS_PER_DAY", "200"))
        self.max_cpu_seconds_per_month = float(max_cpu_seconds_per_month if max_cpu_seconds_per_month is not None else os.environ.get("MARKET_REPLAY_MAX_CPU_SECONDS_PER_MONTH", str(3 * 3600)))
        self.runtimes_available = runtimes_available or (("python",) if hosted else ("python", "typescript"))
        self.max_runs_per_hour_per_ip = int(max_runs_per_hour_per_ip if max_runs_per_hour_per_ip is not None else os.environ.get("MARKET_REPLAY_MAX_RUNS_PER_HOUR_PER_IP", "20"))
        self._packs: dict[str, Pack] = {}
        self._contexts: dict[str, RunContext] = {}
        self._episode_ctx_cache: dict[str, dict[str, Any]] = {}
        self._global = threading.RLock()
        self.gateway_url = "http://127.0.0.1:8000"
        if suites_path is None:
            suites_path = data_dir / "suites.yaml"
            if not suites_path.exists():
                suites_path.write_text(default_suites_text())
        self.suites_path = suites_path
        self.suites: dict[str, SuiteDef] = load_suites(suites_path)

    def close(self) -> None:
        for ctx in list(self._contexts.values()):
            if ctx.proc is not None and ctx.proc.poll() is None:
                ctx.proc.terminate()
        self._contexts.clear()
        self.store.close()

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
        return self._register(pack, report, name or p.name)

    def _register(self, pack: Pack, report: dict[str, Any], pack_name: str) -> dict[str, Any]:
        p = pack.path
        m = pack.manifest
        existing = self.store.pack(m.pack_id)
        # Re-imports must preserve the privacy of data imported before private bundles were retired.
        visibility = existing["visibility"] if existing else "public"
        episode_id = existing["episode_id"] if existing else "ep_" + secrets.token_hex(6)
        row = {
            "pack_id": m.pack_id,
            "visibility": visibility,
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
        return self.pack_row(m.pack_id, reveal_dates=True)

    def register_shipped_weeks(self) -> list[dict[str, Any]]:
        """Register every recorded week committed under ``weeks/`` (one pack directory each). The
        repository is the store for weeks: every instance has them on its own disk, so nothing is
        downloaded. A week already registered from this same path is left alone; a moved or new one
        is (re)registered from its committed validation report, without replaying it."""
        out = []
        # A week whose directory was removed from the repository is withdrawn: it leaves the catalogue
        # and the leaderboard, and its finished runs keep their reports.
        for row in self.store.packs():
            if row["use_status"] not in ("demo", "research", "qualified_for_named_suite") or not row.get("path"):
                continue
            shipped = Path(row["path"]).resolve().parent == WEEKS_DIR.resolve()
            if shipped and not (Path(row["path"]) / "manifest.yaml").exists() and not (WEEKS_DIR / row["name"] / "manifest.yaml").exists():
                self.store.execute("UPDATE packs SET use_status=? WHERE pack_id=?", (str(UseStatus.WITHDRAWN), row["pack_id"]))
                print(f"[market-replay] {row['name']} withdrawn: its files are no longer in the repository", file=sys.stderr)
        if not WEEKS_DIR.is_dir():
            return out
        for path in sorted(p for p in WEEKS_DIR.iterdir() if (p / "manifest.yaml").exists()):
            row = self.store.pack(path.name)
            if row is not None and row["path"] == str(path.resolve()):
                try:
                    on_disk = str((yaml.safe_load((path / "manifest.yaml").read_text()) or {}).get("pack_id"))
                except (OSError, ValueError):
                    on_disk = row["pack_id"]
                if on_disk == row["pack_id"]:
                    out.append(self._pack_view(row))
                    continue
                # The directory was re-recorded: retire the old row under a distinct name so lookups by
                # name reach the new pack, then register the new one below.
                self.store.execute("UPDATE packs SET use_status=?, name=? WHERE pack_id=?", (str(UseStatus.WITHDRAWN), f"{row['name']}@{row['pack_id'][-8:]}", row["pack_id"]))
                print(f"[market-replay] {row['name']} re-recorded: {row['pack_id']} withdrawn", file=sys.stderr)
            try:
                out.append(self.register_pack(path, path.name))
            except (PackError, ApiError, ValueError, OSError) as e:  # one broken directory must not take the site down
                print(f"[market-replay] shipped week {path.name} not registered: {e}", file=sys.stderr)
        return out

    def register_pack(self, path: str | Path, name: str | None = None) -> dict[str, Any]:
        """Register a pack from its committed validation report (the collector already ran the
        validator; the hashes are verified on load). ``import_pack`` validates again instead."""
        p = Path(path)
        try:
            pack = Pack.load(p)
        except (PackError, FileNotFoundError, ValueError) as e:
            raise ApiError(400, f"pack registration failed: {e}", "PACK_INVALID") from e
        report = pack.validation
        if not report or "resulting_qualification" not in report:
            return self.import_pack(p, name)
        return self._register(pack, report, name or p.name)

    def ensure_fixture_pack(self, name: str) -> dict[str, Any]:
        """Register one of the shipped generated fixtures by name, generating it only the first time.

        A registered pack whose files are gone (ephemeral filesystem on a fresh serverless instance)
        is *not* regenerated here: ``load_pack`` does that lazily, and only for packs a run needs.
        """
        row = self.store.pack(name)
        if row is not None:
            return self._pack_view(row)
        cfg = FIXTURE_CONFIGS.get(name)
        if cfg is None:
            raise ApiError(404, f"unknown fixture {name}", "NOT_FOUND")
        target = self.data_dir / "packs" / "generated" / name
        if not (target / "manifest.yaml").exists():
            generate_pack(cfg, target)
        return self.import_pack(target, name)

    def _pack_summary(self, pack: Pack, report: dict[str, Any]) -> dict[str, Any]:
        m = pack.manifest
        supported = sum(1 for p in pack.pools.values() if _pool_executable(p))
        cov = pack.coverage or {}
        states: dict[str, int] = {}
        for i in cov.get("intervals", []):
            states[i.get("state", "unknown")] = states.get(i.get("state", "unknown"), 0) + 1
        return {
            "pools_total": len(pack.pools),
            "pools_executable": supported,
            "assets_total": len(pack.assets),
            "tape_events": pack.tape_count,
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
                path = Path(row["path"])
                if not (path / "manifest.yaml").exists():
                    # Ephemeral filesystem (serverless) or moved files: shipped fixtures are regenerated
                    # deterministically and must hash to the same pack id; recorded weeks are read from
                    # the repository checkout every instance carries.
                    cfg = FIXTURE_CONFIGS.get(row["name"])
                    if cfg is not None:
                        path = self.data_dir / "packs" / "generated" / row["name"]
                        if not (path / "manifest.yaml").exists():
                            generate_pack(cfg, path)
                    elif (WEEKS_DIR / row["name"] / "manifest.yaml").exists():
                        path = WEEKS_DIR / row["name"]
                    else:
                        raise ApiError(410, f"pack files for {row['name']} are not available on this instance", "PACK_FILES_MISSING")
                    self.store.execute("UPDATE packs SET path=? WHERE pack_id=?", (str(path.resolve()), row["pack_id"]))
                pack = Pack.load(path)
                if pack.pack_id != row["pack_id"]:
                    raise ApiError(500, "regenerated pack does not match the registered pack id", "PACK_MISMATCH")
                self._packs[row["pack_id"]] = pack
        return row, pack

    def pack_row(self, pack_ref: str, reveal_dates: bool = False) -> dict[str, Any]:
        row = self.store.pack(pack_ref)
        if row is None or (row["visibility"] == "holdout" and not reveal_dates):
            raise ApiError(404, f"unknown pack {pack_ref}", "NOT_FOUND")
        return self._pack_view(row, reveal_dates)

    def _pack_view(self, row: dict[str, Any], reveal_dates: bool = False) -> dict[str, Any]:
        summary = json.loads(row["summary_json"])
        sealed_only = any(row["name"] in s.packs and s.sealed for s in self.suites.values()) and not any(row["name"] in s.packs and not s.sealed for s in self.suites.values())
        view = {
            "pack_id": row["pack_id"],
            "visibility": row["visibility"],
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
            "label": _episode_label(row),
            "kind": "practice" if str(row.get("origin", "")) == "generated_fixture" else "real",
            "runnable": row["use_status"] in ("demo", "research", "qualified_for_named_suite") and summary.get("pools_executable", 0) > 0,
            "diagnostic_only": row["use_status"] in ("diagnostic_only",),
            "summary": summary,
            "supported_actions": sorted(TOOLS) if summary.get("pools_executable", 0) > 0 else [t for t in TOOLS if not t.startswith("broker.")],
            "unsupported_capabilities": UNSUPPORTED_CAPABILITIES,
            "predictive_validity": "not_established",
            "market_baseline": _market_public(read_market_baseline(Path(row["path"]))) if row.get("path") else None,
        }
        # The calendar is public on real data (the leaderboard is labelled by it); what stays generic is
        # everything an agent sees inside a session (pool and token names). Generated fixtures have no
        # real calendar; packs that only exist in a sealed suite keep theirs to the operator.
        if str(row.get("origin", "")) != "generated_fixture" and not sealed_only:
            view["period"] = {"start_utc": row["start_utc"], "end_utc": row["end_utc"]}
        elif reveal_dates and self.dev_mode and not sealed_only:
            view["period_dev_mode"] = {"start_utc": row["start_utc"], "end_utc": row["end_utc"], "note": "actual dates shown to the operator only"}
        return view

    def packs(self, reveal_dates: bool = False) -> list[dict[str, Any]]:
        return [self._pack_view(r, reveal_dates) for r in self.store.packs() if reveal_dates or r["visibility"] == "public"]

    def is_private_run(self, run_id: str) -> bool:
        """Keep archived private runs hidden even when their pack record is missing."""
        row = self.store.run(run_id)
        pack = self.store.pack(row["pack_id"]) if row else None
        return bool((pack and pack["visibility"] == "holdout") or self.store.one("SELECT run_id FROM assessment_episodes WHERE run_id=?", (run_id,)))

    def require_public(self, kind: str, ref: str) -> None:
        private = self.is_private_run(ref) if kind == "runs" else bool((row := self.store.pack(ref)) and row["visibility"] == "holdout")
        if private:
            raise ApiError(404, "unknown resource", "NOT_FOUND")

    def public_descriptor(self, pack: Pack, row: dict[str, Any], isolation: str) -> PublicDescriptor:
        m = pack.manifest
        supported = any(_pool_executable(p) for p in pack.pools.values())
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
        seen = set()
        dups = 0
        for r in pack.iter_tape():
            k = (r["block"], r["log_index"], r.get("tx"))
            if k in seen and r["kind"] == "swap":
                dups += 1
            seen.add(k)
        unpublished = sum(1 for r in pack.iter_tape() if r["kind"] == "swap" and r.get("available_utc_ms") is None)
        conflicts = [r.model_dump() for r in pack.restrictions if r.conflicts]
        pools_missing_state = [p.key for p in pack.pools.values() if (p.supported_by_cpmm and p.initial_reserve0 is None) or (p.supported_by_clmm and p.initial_sqrt_price_x96 is None and not _initialized_on_tape(pack, p.key))]
        attempts = self.store.query("SELECT * FROM attempts WHERE pack_id=?", (m.pack_id,))
        exposed = self.store.query("SELECT COUNT(*) AS n FROM runs WHERE pack_id=? AND exposed=1", (m.pack_id,))[0]["n"]
        return {
            "pack": self._pack_view(row),
            "universe": m.universe.model_dump(mode="json"),
            "ingestion": {"tape_events": pack.tape_count, "blocks_table_rows": len(pack.blocks), "indexed_block_ranges": m.universe.indexed_block_ranges},
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
            "exposed_runs": int(exposed),
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

    def register_or_reuse_agent(self, *, name: str, version: str, runtime: str = "external", capabilities: list[str] | None = None, config: dict[str, Any] | None = None) -> dict[str, Any]:
        """Self-serve registration: the same name and version means the same agent."""
        name = name.strip()
        version = version.strip() or "1"
        if not name or len(name) > 64 or len(version) > 32 or not name.isprintable():
            raise ApiError(400, "agent name must be 1-64 printable characters; version at most 32", "INVALID")
        existing = self.store.agent_by_name_version(name, version)
        if existing:
            return self.agent_view(existing["agent_id"])
        return self.register_agent(name=name, version=version, runtime=runtime, capabilities=capabilities or [], config=config or {})

    def rate_limit(self, kind: str, key: str, limit: int | None = None, window_s: float = 3600.0) -> None:
        """Count one event for (kind, key); refuse with 429 RATE_LIMITED past `limit` events per window."""
        limit = self.max_runs_per_hour_per_ip if limit is None else limit
        now = time.time()
        if self.store.count_rate_events(kind, key, now - window_s) >= limit:
            raise ApiError(429, f"too many {kind} requests from this address; limit {limit} per hour", "RATE_LIMITED")
        self.store.add_rate_event(kind, key, now)

    def agent_view(self, agent_id: str) -> dict[str, Any]:
        row = self.store.agent(agent_id)
        if row is None:
            raise ApiError(404, f"unknown agent {agent_id}", "NOT_FOUND")
        caps = json.loads(row["capabilities_json"])
        incompatible = sorted(c for c in set(caps) if c in UNSUPPORTED_CAPABILITIES)
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

    def agent_history(self, agent_id: str) -> dict[str, Any]:
        """Everything one agent has done here, day by day, in plain words: each run with what it was,
        how it ended, the number that counts and how the market did that day. For the site's agent page
        and for an owner asking their agent how it has done."""
        agent = self.agent_view(agent_id)
        by_pack: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for row in sorted(self.store.runs(agent_id=agent_id), key=lambda r: r["created_at"], reverse=True):
            if self.is_private_run(row["run_id"]):
                continue
            pack_row = self.store.pack(row["pack_id"])
            pid = row["pack_id"]
            if pid not in by_pack:
                ctx = self._episode_context(pack_row) if pack_row and str(pack_row.get("origin", "")) != "generated_fixture" else {}
                by_pack[pid] = {
                    "pack_id": pid,
                    "pack_name": pack_row["name"] if pack_row else None,
                    "label": _episode_label(pack_row) if pack_row else "a withdrawn episode",
                    "kind": "practice" if pack_row and str(pack_row.get("origin", "")) == "generated_fixture" else "real",
                    "date": ctx.get("date"),
                    "available": bool(pack_row and pack_row["use_status"] in ("demo", "research", "qualified_for_named_suite")),
                    "market_note": ctx.get("market_note"),
                    "market_return": ((ctx.get("market") or {}).get("launches") or {}).get("equal_weight_return"),
                    "runs": [],
                }
                order.append(pid)
            by_pack[pid]["runs"].append(self._history_item(row))
        days = [by_pack[pid] for pid in order]
        for d in days:
            counted = [r for r in d["runs"] if r["counts_for_ranking"]]
            d["counted_run_id"] = counted[0]["run_id"] if counted else None
            d["counted_return"] = counted[0]["headline_return"] if counted else None
            d["summary"] = _day_summary(d, counted[0] if counted else None)
        finished = [r for d in days for r in d["runs"] if r["counts_for_ranking"]]
        return {
            "agent": agent,
            "days": days,
            "totals": {
                "runs": sum(len(d["runs"]) for d in days),
                "days_played": len(days),
                "days_finished": sum(1 for d in days if d["counted_run_id"]),
                "runs_in_progress": sum(1 for d in days for r in d["runs"] if r["state"] in ("queued", "running", "paused")),
                "fills": sum(int(r["fills"] or 0) for r in finished),
            },
            "note": "Each run is one attempt at one recorded day. The run that counts on the leaderboard is the latest finished one scored on final ETH; earlier attempts and unfinished runs are listed but not ranked. The market line is a naive reference, not a target.",
        }

    def _history_item(self, row: dict[str, Any]) -> dict[str, Any]:
        summ = self._result_summary(row.get("report_json")) or {}
        state = str(row["state"])
        counts = state == str(RunState.COMPLETED) and summ.get("primary_metric") == "final_cash_return_v1" and summ.get("headline_return") is not None and summ.get("ranking_eligible", False)
        dec = int(summ.get("numeraire_decimals") or 18)
        unit = "ETH" if (summ.get("numeraire") or "NATIVE") == "NATIVE" else str(summ.get("numeraire"))  # the pack calls the wrapped native coin NATIVE; owners know it as ETH

        def amount(raw: Any) -> str | None:
            if raw is None:
                return None
            try:
                return f"{Decimal(raw) / 10**dec:.4f} {unit}"
            except (TypeError, ValueError):
                return None

        outcome = _state_in_words(state, row.get("error"))
        ret = summ.get("headline_return")
        if counts:
            outcome = f"Finished with a final ETH return of {Decimal(ret) * 100:+.2f}% after gas and fees" if unit == "ETH" else f"Finished with a final cash return of {Decimal(ret) * 100:+.2f}% after modeled costs"
        elif state == str(RunState.COMPLETED) and summ.get("primary_metric") == "final_cash_return_v1":
            outcome = "Finished with a provisional outcome; excluded from ranking by the execution eligibility gates"
        elif state == str(RunState.COMPLETED) and summ:
            outcome = "Finished under the old portfolio scoring, so it is not ranked; play the day again to get a ranked result"
        return {
            "run_id": row["run_id"],
            "state": state,
            "outcome": outcome,
            "counts_for_ranking": bool(counts),
            "primary_metric": summ.get("primary_metric"),
            "headline_return": ret,
            "started_at": row.get("started_at"),
            "finished_at": row.get("finished_at"),
            "created_at": row.get("created_at"),
            "fills": summ.get("confirmed_fills"),
            "orders": summ.get("orders_total"),
            "final_cash": amount(summ.get("final_cash_raw")),
            "started_with": amount(summ.get("initial_equity_raw")),
            "gas_paid": amount(summ.get("gas_total_raw")),
            "max_drawdown": summ.get("max_drawdown"),
            "unsold_holdings": summ.get("unpriced_inventory"),
            "activity": _activity_in_words(summ) if summ else None,
            "results_path": f"/runs/{row['run_id']}/results" if row.get("report_json") else None,
        }

    # ------------------------------------------------------------------ usage caps
    def usage_today(self) -> dict[str, Any]:
        return self.store.usage_today()

    def usage_view(self) -> dict[str, Any]:
        today = self.store.usage_today()
        month = self.store.usage_month()
        return {
            "today": today,
            "month": month,
            "caps": {"max_runs_per_day": self.max_runs_per_day, "max_cpu_seconds_per_month": self.max_cpu_seconds_per_month, "max_runs_per_hour_per_ip": self.max_runs_per_hour_per_ip},
            "remaining": {"runs_today": max(0, self.max_runs_per_day - today["runs"]), "cpu_seconds_month": max(0.0, self.max_cpu_seconds_per_month - month["cpu_seconds"])},
            "note": "Caps are operator settings (MARKET_REPLAY_MAX_RUNS_PER_DAY, MARKET_REPLAY_MAX_CPU_SECONDS_PER_MONTH). Raise them when more usage is purchased.",
        }

    def _check_caps(self) -> None:
        today = self.store.usage_today()
        if today["runs"] >= self.max_runs_per_day:
            raise ApiError(429, f"daily run cap reached ({self.max_runs_per_day}); raise MARKET_REPLAY_MAX_RUNS_PER_DAY or try tomorrow", "USAGE_CAP")
        month = self.store.usage_month()
        if month["cpu_seconds"] >= self.max_cpu_seconds_per_month:
            raise ApiError(429, f"monthly compute cap reached ({self.max_cpu_seconds_per_month:.0f} CPU-seconds)", "USAGE_CAP")

    # ------------------------------------------------------------------ runs
    def create_run(
        self,
        *,
        agent_id: str,
        pack_ref: str,
        mode: str = "practice",
        bankroll_raw: str | int | None = None,
        mask_seed: str | None = None,
        engine_seed: str | None = None,
        isolation: str = "trusted_external_client",
        launch_spec: dict[str, Any] | None = None,
        suite_id: str | None = None,
        suite_run_id: str | None = None,
        agent_seed: str | None = None,
        execute: str | None = None,
        resource_profile_id: str = "pack_defaults_v1",
    ) -> dict[str, Any]:
        agent = self.agent_view(agent_id)
        if not agent["compatibility"]["compatible"]:
            raise ApiError(400, f"agent requests unsupported capabilities: {agent['compatibility']['unsupported_requested']}", "INCOMPATIBLE")
        self.require_public("packs", pack_ref)
        row, pack = self.load_pack(pack_ref)
        profile = PROFILES.get(resource_profile_id)
        if profile is None:
            raise ApiError(400, "unknown resource profile", "INVALID")
        if profile.timing == "runner_measured" and not (launch_spec and launch_spec.get("runtime", "python") == "python" and (execute or ("inprocess" if self.hosted else "subprocess")) == "inprocess"):
            raise ApiError(400, "deployment timing requires the in-process measured Python runner", "UNSUPPORTED_CAPABILITY")
        if row["use_status"] not in ("demo", "research", "qualified_for_named_suite"):
            raise ApiError(400, f"pack use status '{row['use_status']}' is not runnable; see its validation report", "NOT_RUNNABLE")
        if mode not in ("practice", "sealed"):
            raise ApiError(400, "mode must be practice or sealed", "INVALID")
        if isolation not in ("trusted_external_client", "restricted_local_runner"):
            raise ApiError(400, "invalid isolation", "INVALID")
        if isolation == "restricted_local_runner" and (not launch_spec or (execute or ("inprocess" if self.hosted else "subprocess")) != "subprocess"):
            raise ApiError(400, "restricted isolation requires the restricted subprocess runner", "INVALID")
        if bankroll_raw is None:
            # One whole unit of the cash asset: 1.0 CASH on a generated pack, 1 ETH on a real Base day.
            bankroll_raw = 10 ** int(pack.manifest.numeraire_decimals)
        try:
            bankroll = int(str(bankroll_raw))
        except ValueError as e:
            raise ApiError(400, "bankroll_raw must be an integer string", "INVALID") from e
        if bankroll <= 0:
            raise ApiError(400, "bankroll must be positive", "INVALID")
        if launch_spec:
            rt = str(launch_spec.get("runtime", "python"))
            if rt not in self.runtimes_available:
                raise ApiError(400, f"runtime {rt} is not available on this server (available: {', '.join(self.runtimes_available)})", "RUNTIME_UNAVAILABLE")
            known = PY_EXAMPLES if rt == "python" else TS_EXAMPLES
            if launch_spec.get("name") not in known:
                raise ApiError(400, f"unknown reference participant {launch_spec.get('name')}", "INVALID")
        self._check_caps()
        run_id = "run_" + secrets.token_hex(8)
        mask_seed = mask_seed or ("mask-" + secrets.token_hex(8))
        engine_seed = engine_seed or ("engine-" + secrets.token_hex(8))
        session = Session.create(session_id="ses_" + run_id[4:], pack=pack, bankroll_raw=bankroll, mask_seed=mask_seed, engine_seed=engine_seed, mode=mode, resource_profile=profile.model_dump())
        profile_hash = pack.manifest.execution.parameters_hash if resource_profile_id == "pack_defaults_v1" else hashlib.sha256((pack.manifest.execution.parameters_hash + profile.fingerprint()).encode()).hexdigest()
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
            "profile_hash": profile_hash,
            "token_hash": token_hash(token),
            "launch_json": json.dumps(launch_spec) if launch_spec else None,
            "created_at": now_iso(),
            "clock_ms": 0,
        }
        self.store.insert_run(run_row)
        self.store.add_usage(runs=1)
        self.store.put_doc(
            run_id,
            "run_manifest",
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
                "profile_hash": profile_hash,
                "resource_profile": profile.model_dump(),
                "execution_profile": pack.params.profile_name,
                "budgets": pack.params.budgets.model_dump(),
                "engine_version": ENGINE_VERSION,
                "attempt_number": attempt,
                "created_at": run_row["created_at"],
            },
        )
        ctx = RunContext(run_id=run_id, session=session, token_hash=run_row["token_hash"], started_wall=time.time(), launch=launch_spec)
        with self._global:
            self._contexts[run_id] = ctx
        if launch_spec:
            mode_exec = execute or ("inprocess" if self.hosted else "subprocess")
            if mode_exec == "inprocess":
                self.execute_run(run_id, token=token)
            else:
                self._launch(ctx, token, launch_spec, isolation, agent_seed)
            return self.run_view(run_id)
        result = self.run_view(run_id)
        # The credential is returned only to the caller who created the run (the participant or its runner).
        result["session_credential"] = {"token": token, "gateway_url": self.gateway_url, "commands_url": self.gateway_url + "/agent/v1/commands", "mcp_url": self.gateway_url + "/agent/mcp"}
        return result

    def execute_run(self, run_id: str, token: str | None = None) -> dict[str, Any]:
        """Run a launched Python reference participant inside this process (hosted mode)."""
        row = self.store.run(run_id)
        if row is None:
            raise ApiError(404, f"unknown run {run_id}", "NOT_FOUND")
        if not row["launch_json"]:
            raise ApiError(400, "run has no launch specification; it waits for an external client", "NO_LAUNCH")
        if row["state"] in TERMINAL:
            return self.run_view(run_id)
        spec = json.loads(row["launch_json"])
        if spec.get("runtime", "python") != "python":
            raise ApiError(400, "only Python reference participants can run in-process", "RUNTIME_UNAVAILABLE")
        if token is None:
            # Executing an existing launched run needs a fresh credential bound to it (the original was never disclosed).
            token = new_agent_token()
            self.store.update_run(run_id, token_hash=token_hash(token))
            ctx = self._ctx(run_id)
            ctx.token_hash = token_hash(token)
        manifest = self.store.get_doc(run_id, "run_manifest") or {}
        self.store.update_run(run_id, state=str(RunState.RUNNING), started_at=now_iso())
        ctx = self._ctx(run_id)
        ctx.controls = {"isolation": "in_process_reference_participant", "enforced": ["no_network_from_agent_code_is_not_enforced"], "unenforced": ["network", "filesystem"], "note": "reference participant executed inside the server process"}
        res = run_example_inprocess(self.handle_command, token=token, name=str(spec["name"]), agent_seed=manifest.get("agent_seed"), measure_decisions=ctx.session.resource_profile.timing == "runner_measured")
        self.store.put_doc(run_id, "agent_log", res["log"])
        self.store.add_usage(cpu_seconds=res["cpu_seconds"])
        row = self.store.run(run_id)
        assert row is not None
        if row["state"] not in TERMINAL:
            if ctx.session.finished:
                self._finalize(ctx, RunState.BUDGET_EXHAUSTED if row["state"] == str(RunState.BUDGET_EXHAUSTED) else RunState.COMPLETED)
            else:
                self._finalize(ctx, RunState.AGENT_FAILED, error=f"agent finished with exit code {res['exit_code']} before session.finish")
        return self.run_view(run_id)

    def _launch(self, ctx: RunContext, token: str, spec: dict[str, Any], isolation: str, agent_seed: str | None) -> None:
        ls = LaunchSpec(name=str(spec.get("name")), runtime=str(spec.get("runtime", "python")), extra_args=[str(a) for a in spec.get("args", [])])
        log_path = self.data_dir / "runs" / ctx.run_id / "agent.log"
        try:
            if isolation == "restricted_local_runner":
                proc, controls, _wd = launch_restricted(ls, gateway_url=self.gateway_url, token=token, agent_seed=agent_seed, log_path=log_path)
                ctx.controls = controls.as_dict()
            else:
                proc = launch(ls, gateway_url=self.gateway_url, token=token, agent_seed=agent_seed, log_path=log_path)
                ctx.controls = {"isolation": "trusted_external_client", "enforced": [], "unenforced": ["network", "filesystem", "memory_across_runs"]}
        except (ValueError, OSError) as e:
            self.store.update_run(ctx.run_id, state=str(RunState.ENVIRONMENT_FAILED), error=f"launch failed: {e}", finished_at=now_iso())
            return
        ctx.proc = proc
        self.store.update_run(ctx.run_id, state=str(RunState.RUNNING), started_at=now_iso())

        def monitor() -> None:
            rc = proc.wait()
            try:
                self.store.put_doc(ctx.run_id, "agent_log", log_path.read_text() if log_path.exists() else "")
            except OSError:
                pass
            with self.store.run_lock(ctx.run_id):
                row = self.store.run(ctx.run_id)
                if row and row["state"] not in TERMINAL:
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

    def agent_log(self, run_id: str) -> str:
        doc = self.store.get_doc(run_id, "agent_log")
        if doc is None:
            p = self.data_dir / "runs" / run_id / "agent.log"
            return p.read_text() if p.exists() else ""
        return str(doc)

    # ------------------------------------------------------------------ session cache / rebuild
    def _ctx(self, run_id: str) -> RunContext:
        """Return the live context, rebuilding it from the stored trace when this instance has none."""
        with self._global:
            ctx = self._contexts.get(run_id)
            if ctx is not None:
                return ctx
        row = self.store.run(run_id)
        if row is None:
            raise ApiError(404, f"unknown run {run_id}", "NOT_FOUND")
        started = time.monotonic()
        LOG.info("session_rebuild phase=pack_load run_id=%s", run_id)
        try:
            _pack_row, pack = self.load_pack(row["pack_id"])
        except (ApiError, ValueError, OSError):
            raise ApiError(410, "session episode unavailable on this instance", "PACK_FILES_MISSING") from None
        trace = self.store.trace(run_id)
        manifest = self.store.get_doc(run_id, "run_manifest") or {}
        LOG.info("session_rebuild phase=replay run_id=%s commands=%d elapsed_s=%.3f", run_id, len(trace), time.monotonic() - started)
        session = replay_trace(pack, trace, bankroll_raw=int(row["bankroll_raw"]), mask_seed=row["mask_seed"], engine_seed=row["engine_seed"], session_id="ses_" + run_id[4:], mode=row["mode"], resource_profile=manifest.get("resource_profile"))
        LOG.info("session_rebuild phase=ready run_id=%s commands=%d clock_ms=%d elapsed_s=%.3f", run_id, len(trace), session.now, time.monotonic() - started)
        session.paused = row["state"] == str(RunState.PAUSED)
        ctx = RunContext(run_id=run_id, session=session, token_hash=row["token_hash"] or "", started_wall=time.time(), launch=json.loads(row["launch_json"]) if row["launch_json"] else None, trace_written=len(trace))
        with self._global:
            self._contexts.setdefault(run_id, ctx)
            return self._contexts[run_id]

    def _catch_up(self, ctx: RunContext) -> None:
        """Apply commands another instance appended since this cache was last synced."""
        missing = self.store.trace(ctx.run_id, after=len(ctx.session.trace) - 1)
        if missing:
            LOG.info("session_catch_up run_id=%s commands=%d", ctx.run_id, len(missing))
        for r in missing:
            ctx.session.handle(r["request_id"], r["tool"], r.get("arguments") or {}, decision_elapsed_ms=r.get("decision_elapsed_ms", 0))
            if r.get("delivered"):
                ctx.session.trace[-1].delivered = r["delivered"]
            else:
                ctx.session.trace[-1].delivered["evidence_basis"] = "reconstructed_under_current_engine"
        ctx.trace_written = len(ctx.session.trace)

    def handle_command(self, token: str, request_id: str, tool: str, arguments: dict[str, Any] | None, session_id: str | None = None, *, measured_elapsed_ms: int | None = None) -> Envelope:
        started = time.monotonic()
        safe_tool = tool if tool in TOOLS else "unknown"
        LOG.info("session_command phase=authenticate tool=%s", safe_tool)
        row = self.store.run_by_token_hash(token_hash(token))
        if row is None:
            raise ApiError(401, "invalid credential", "UNAUTHORIZED")
        run_id = row["run_id"]
        if session_id is not None and session_id != "ses_" + run_id[4:]:
            raise ApiError(403, "session_id does not match the credential", "FORBIDDEN")
        LOG.info("session_command phase=lock_wait run_id=%s tool=%s", run_id, safe_tool)
        try:
            return self._locked_command(run_id, token, request_id, tool, arguments, measured_elapsed_ms, started)
        except RunBusy:
            LOG.warning("session_command phase=busy run_id=%s tool=%s elapsed_s=%.3f", run_id, safe_tool, time.monotonic() - started)
            raise

    def _locked_command(self, run_id: str, token: str, request_id: str, tool: str, arguments: dict[str, Any] | None, measured_elapsed_ms: int | None, started: float) -> Envelope:
        # Acquire ownership before cold replay so concurrent retries cannot all rebuild the run.
        with self.store.run_lock(run_id):
            LOG.info("session_command phase=load run_id=%s elapsed_s=%.3f", run_id, time.monotonic() - started)
            row = self.store.run(run_id)
            assert row is not None
            if row["token_hash"] != token_hash(token):
                raise ApiError(401, "invalid credential", "UNAUTHORIZED")
            state = row["state"]
            if state in TERMINAL:
                return Envelope.fail(request_id=request_id, session_id="ses_" + run_id[4:], clock_ms=row["clock_ms"], code=ErrorCode.SESSION_FINISHED, message=f"run is {state}")
            ctx = self._ctx(run_id)
            self._catch_up(ctx)
            if ctx.session.resource_profile.timing == "runner_measured" and measured_elapsed_ms is None:
                raise ApiError(403, "this run only accepts commands through its measured runner", "RUNNER_REQUIRED")
            ctx.session.paused = state == str(RunState.PAUSED)
            if state == str(RunState.QUEUED):
                self.store.update_run(ctx.run_id, state=str(RunState.RUNNING), started_at=now_iso())
            try:
                LOG.info("session_command phase=execute run_id=%s clock_ms=%d", run_id, ctx.session.now)
                env = ctx.session.handle(request_id, tool, arguments, decision_elapsed_ms=measured_elapsed_ms or 0)
            except Exception as e:  # environment failure, never blamed on the agent
                tb = traceback.format_exc(limit=5)
                self._finalize(ctx, RunState.ENVIRONMENT_FAILED, error=f"{type(e).__name__}: {e}\n{tb}")
                return Envelope.fail(request_id=request_id, session_id=ctx.session.session_id, clock_ms=ctx.session.now, code=ErrorCode.ENVIRONMENT_FIDELITY_LIMIT, message="environment failure; run marked environment_failed")
            LOG.info("session_command phase=persist run_id=%s clock_ms=%d", run_id, ctx.session.now)
            self._persist_trace(ctx)
            updates: dict[str, Any] = {"clock_ms": ctx.session.now}
            if ctx.session.budget.exhausted and state != str(RunState.BUDGET_EXHAUSTED):
                updates["state"] = str(RunState.BUDGET_EXHAUSTED)
            self.store.update_run(ctx.run_id, **updates)
            if tool == "session.finish" and env.status == "ok":
                self._finalize(ctx, RunState.BUDGET_EXHAUSTED if ctx.session.budget.exhausted else RunState.COMPLETED)
            LOG.info("session_command phase=complete run_id=%s status=%s clock_ms=%d elapsed_s=%.3f", run_id, env.status, ctx.session.now, time.monotonic() - started)
            return env

    def _persist_trace(self, ctx: RunContext) -> None:
        recs = ctx.session.trace[ctx.trace_written :]
        if not recs:
            return
        from dataclasses import asdict

        self.store.append_trace(ctx.run_id, [asdict(r) for r in recs])
        ctx.trace_written = len(ctx.session.trace)

    def _run_meta(self, ctx: RunContext, row: dict[str, Any]) -> dict[str, Any]:
        manifest = self.store.get_doc(ctx.run_id, "run_manifest") or {}
        wall = {"started_at": row.get("started_at") or row.get("created_at"), "elapsed_s": round(time.time() - ctx.started_wall, 3)}
        return {
            "run_id": ctx.run_id,
            "agent": manifest.get("agent"),
            "episode_id": manifest.get("episode_id"),
            "mode": row["mode"],
            "isolation": row["isolation"],
            "isolation_controls": ctx.controls,
            "attempt_number": manifest.get("attempt_number"),
            "state": row["state"],
            "error": row.get("error"),
            "clock_ms": ctx.session.now,
            "pack_id": row["pack_id"],
            "mask_seed": row["mask_seed"],
            "engine_seed": row["engine_seed"],
            "agent_seed": manifest.get("agent_seed"),
            "profile_hash": row["profile_hash"],
            "resource_profile": ctx.session.resource_profile.public(),
            "wall_clock": wall,
            "inference": ctx.inference,
        }

    def _finalize(self, ctx: RunContext, state: RunState, error: str | None = None) -> None:
        row = self.store.run(ctx.run_id)
        assert row is not None
        if row["state"] in TERMINAL and state != RunState.ABORTED:
            return
        self._persist_trace(ctx)
        row = dict(row)
        row["state"] = str(state)
        if error:
            row["error"] = error
        try:
            report = build_report(ctx.session, run_meta=self._run_meta(ctx, row), role="admin")
        except Exception as e:
            LOG.exception("report_generation_failed run_id=%s", ctx.run_id)
            report = {"error": f"report generation failed: {e}",
                      "report_failure": {"intended_state": str(state), "original_error": error}}
            state = RunState.ENVIRONMENT_FAILED
            error = (error or "") + f" report failure: {e}"
        self.store.update_run(ctx.run_id, state=str(state), error=error, finished_at=now_iso(), clock_ms=ctx.session.now, report_json=json.dumps(report, sort_keys=True))
        self._store_trade_review(ctx)

    def _store_trade_review(self, ctx: RunContext) -> dict[str, Any] | None:
        """Keep the trade review next to the report while the session is live: rebuilding a session of a
        real day later means replaying the whole day, which a serverless function cannot afford."""
        from ..evaluation.trade_review import build_trade_review

        try:
            review = build_trade_review(ctx.session)
        except Exception:
            LOG.exception("trade_review_failed run_id=%s", ctx.run_id)
            return None
        self.store.put_doc(ctx.run_id, "trade_review", review)
        return review

    def pause(self, run_id: str) -> dict[str, Any]:
        ctx = self._ctx(run_id)
        with self.store.run_lock(run_id):
            ctx.session.paused = True
            self.store.update_run(run_id, state=str(RunState.PAUSED))
        return self.run_view(run_id, allow_private=True)

    def resume(self, run_id: str) -> dict[str, Any]:
        ctx = self._ctx(run_id)
        with self.store.run_lock(run_id):
            ctx.session.paused = False
            self.store.update_run(run_id, state=str(RunState.RUNNING))
        return self.run_view(run_id, allow_private=True)

    def abort(self, run_id: str) -> dict[str, Any]:
        ctx = self._ctx(run_id)
        with self.store.run_lock(run_id):
            if ctx.proc is not None and ctx.proc.poll() is None:
                ctx.proc.terminate()
            self._catch_up(ctx)
            self._finalize(ctx, RunState.ABORTED, error="aborted by operator")
        return self.run_view(run_id, allow_private=True)

    def _active(self, run_id: str) -> RunContext:
        return self._ctx(run_id)

    def run_view(self, run_id: str, allow_private: bool = False) -> dict[str, Any]:
        if not allow_private:
            self.require_public("runs", run_id)
        row = self.store.run(run_id)
        if row is None:
            raise ApiError(404, f"unknown run {run_id}", "NOT_FOUND")
        pack_row = self.store.pack(row["pack_id"])
        agent_row = self.store.agent(row["agent_id"])
        view = {
            "run_id": row["run_id"],
            "agent_id": row["agent_id"],
            "agent_name": agent_row["name"] if agent_row else None,
            "agent_version": agent_row["version"] if agent_row else None,
            "pack_id": row["pack_id"],
            "episode_id": pack_row["episode_id"] if pack_row else None,
            "pack_name": pack_row["name"] if pack_row else None,
            "pack_label": _episode_label(pack_row) if pack_row else None,
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
            "result_summary": self._result_summary(row["report_json"]),
        }
        # Live detail comes only from a session this instance already holds. Rebuilding one means
        # replaying every command the agent ever sent through the week's data, which is the agent's
        # own command path's job (once per instance), never a listing's: a run list on a cold
        # instance would otherwise replay every unfinished run before answering.
        ctx = self._contexts.get(run_id)
        if (ctx is not None and ctx.session.now == row["clock_ms"]
                and (row["state"] not in TERMINAL or ctx.session.finished)):
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
        return [self.run_view(r["run_id"]) for r in self.store.runs(**where) if not self.is_private_run(r["run_id"])]

    @staticmethod
    def _result_summary(report_json: str | None) -> dict[str, Any] | None:
        """The few numbers a result list needs, taken from the stored report (null until one exists)."""
        if not report_json:
            return None
        try:
            rep = json.loads(report_json)
        except (TypeError, ValueError):
            return None
        if not isinstance(rep, dict) or rep.get("error"):
            return None
        outcome = rep.get("outcome", {}) or {}
        risk = rep.get("risk", {}) or {}
        activity = rep.get("activity", {}) or {}
        costs = rep.get("costs", {}) or {}
        unresolved = rep.get("unresolved", {}) or {}
        return {
            "primary_metric": outcome.get("primary_metric", "legacy_portfolio_return"),
            "ranking_eligible": ranking_eligible(rep),
            "execution_validity": rep.get("execution_validity"),
            "final_cash_raw": outcome.get("final_cash_raw"),
            "liquidatable_portfolio_return": outcome.get("liquidatable_portfolio_return"),
            "headline_return": outcome.get("headline_return"),
            "valuation_complete": bool(outcome.get("valuation_complete", False)),
            "numeraire": outcome.get("numeraire"),
            "numeraire_decimals": outcome.get("numeraire_decimals"),
            "initial_equity_raw": outcome.get("initial_equity_raw"),
            "terminal_model_equity_raw": outcome.get("terminal_model_equity_raw"),
            "max_drawdown": risk.get("max_drawdown"),
            "confirmed_fills": activity.get("confirmed_fills"),
            "orders_total": activity.get("orders_total"),
            "gas_total_raw": costs.get("gas_total_raw"),
            "unpriced_inventory": len(unresolved.get("unpriced_inventory", []) or []) + len(unresolved.get("no_route_inventory", []) or []),
            "unresolved_orders": len(unresolved.get("orders", []) or []),
            "status_dimensions": rep.get("status_dimensions", {}),
        }

    def repair_report(self, run_id: str) -> None:
        """Repair only a report failure after a successful finish; never issue new commands."""
        with self.store.run_lock(run_id):
            row = self.store.run(run_id)
            if row is None:
                raise ApiError(404, "unknown run", "NOT_FOUND")
            old = json.loads(row["report_json"] or "{}")
            if row["state"] == str(RunState.COMPLETED) and old.get("report_recovery"):
                return  # A retry after a lost response is harmless.
            prefix = "report generation failed: "
            message = old.get("error", "")
            failure = old.get("report_failure")
            report_only = (
                failure.get("intended_state") == str(RunState.COMPLETED)
                and failure.get("original_error") is None
            ) if isinstance(failure, dict) else (
                message.startswith(prefix) and row["error"] == " report failure: " + message[len(prefix):]
            )
            if (row["state"] != str(RunState.ENVIRONMENT_FAILED) or not message.startswith(prefix)
                    or not report_only or row["error"] != " report failure: " + message[len(prefix):]):
                raise ApiError(409, "Only a report-only failure after session.finish can be repaired.", "REPORT_NOT_REPAIRABLE")
            trace = self.store.trace(run_id)
            last = trace[-1] if trace else {}
            if last.get("tool") != "session.finish" or last.get("status") != "ok" or last.get("clock_after_ms") != row["clock_ms"]:
                raise ApiError(409, "No matching successful terminal command was recorded.", "REPORT_NOT_REPAIRABLE")
            ctx = self._ctx(run_id)
            self._catch_up(ctx)
            saved_terminal = (last.get("delivered", {}).get("payload") or {}).get("data")
            if (ctx.session.terminal is None or ctx.session.now != row["clock_ms"]
                    or ctx.session.budget.exhausted or saved_terminal != ctx.session.terminal):
                raise ApiError(409, "Stored terminal receipt does not match the reconstructed state.", "REPORT_NOT_REPAIRABLE")
            repaired_row = dict(row) | {"state": str(RunState.COMPLETED), "error": None}
            meta = self._run_meta(ctx, repaired_row)
            if row["started_at"] and row["finished_at"]:
                meta["wall_clock"]["elapsed_s"] = round((datetime.fromisoformat(row["finished_at"]) - datetime.fromisoformat(row["started_at"])).total_seconds(), 3)
            report = build_report(ctx.session, run_meta=meta, role="admin")
            report["report_recovery"] = {
                "previous_state": row["state"], "previous_error": row["error"],
                "original_finished_at": row["finished_at"], "repaired_at": now_iso(),
                "basis": "unchanged_stored_trace_and_matching_terminal_receipt",
            }
            # Publish only after the complete report exists. Preserve finish time, clock and trace.
            self.store.update_run(run_id, state=str(RunState.COMPLETED), error=None,
                                  report_json=json.dumps(report, sort_keys=True))
            LOG.info("report_repaired run_id=%s clock_ms=%d fills=%d", run_id, ctx.session.now,
                     report["activity"]["confirmed_fills"])

    def report(self, run_id: str, role: str = "admin") -> dict[str, Any]:
        row = self.store.run(run_id)
        if row is None:
            raise ApiError(404, f"unknown run {run_id}", "NOT_FOUND")
        ctx = self._contexts.get(run_id)
        if row["report_json"]:
            report = json.loads(row["report_json"])
        else:
            ctx = ctx or self._ctx(run_id)
            report = build_report(ctx.session, run_meta=self._run_meta(ctx, dict(row)), role="admin")
            report["provisional"] = True
        if role != "admin":
            scanner = ctx.session.scanner if ctx else None
            report = redact_for_role(report, role, scanner)
            if "versions" in report:
                report["versions"]["pack_id"] = "hidden"
            for k in ("mask_seed", "engine_seed", "pack_id"):
                report.get("run", {}).pop(k, None)
        return report

    def trade_review(self, run_id: str) -> dict[str, Any]:
        """The review stored when the run ended. A run finished before reviews were stored is rebuilt from
        its trace once, then kept; never change its trace."""
        row = self.store.run(run_id)
        if row is None:
            raise ApiError(404, "unknown run", "NOT_FOUND")
        if row["state"] not in TERMINAL:
            raise ApiError(409, "Trade review is available after the run ends.", "RUN_ACTIVE")
        stored = self.store.get_doc(run_id, "trade_review")
        if isinstance(stored, dict):
            return stored
        with self.store.run_lock(run_id):
            ctx = self._ctx(run_id)
            self._catch_up(ctx)
            review = self._store_trade_review(ctx)
            if review is None:
                raise ApiError(500, "trade review could not be built", "INTERNAL")
            return review

    def observed(self, run_id: str, pool_id: str | None = None, interval_ms: int = 60_000) -> dict[str, Any]:
        """Agent-visible view for the Run screen: only observations available at the current clock."""
        with self.store.run_lock(run_id):
            ctx = self._ctx(run_id)
            s = ctx.session
            self._catch_up(ctx)
            discovered = s.sim.discovered_pools(s.now)
            pools = [{"pool_id": s.alias.pool(k)} for k in discovered]
            series: dict[str, Any] | None = None
            if pool_id or pools:
                pid = pool_id or pools[0]["pool_id"]
                r = s.alias.resolve(pid)
                if r and r[0] == "pool" and r[1] in discovered:
                    from fractions import Fraction

                    from ..domain.quantities import fraction_to_decimal_str
                    from ..observations.store import aggregate_candles

                    key = r[1]
                    base, quote = s._base_quote(key)
                    end = s.now
                    start = max(end - interval_ms * 400, -(10**12))
                    candles, gaps = aggregate_candles(s.sim.obs[key], base_asset=base, interval_ms=interval_ms, start_ms=start, end_ms=end, as_of=s.now, availability_delay_ms=s.params.availability_delay_ms, include_partial=False)
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
        manifest = self.store.get_doc(run_id, "run_manifest") or {}
        trace = self.store.trace(run_id)
        report = self.report(run_id, role)
        pack_row, pack = self.load_pack(row["pack_id"])
        ctx = self._ctx(run_id)
        bundle: dict[str, Any] = {
            "bundle_version": "run_export_v1",
            "role": role,
            "run": self.run_view(run_id, allow_private=role == "admin"),
            "public_descriptor": self.public_descriptor(pack, pack_row, row["isolation"]).model_dump(mode="json"),
            "report": report,
            "trace": trace,
        }
        if role == "admin":
            bundle["run_manifest"] = manifest
            bundle["agent_log"] = self.agent_log(run_id)
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
            bundle["run"] = {k: v for k, v in bundle["run"].items() if k not in ("pack_id", "pack_name", "pack_label")}
            bundle["trace"] = redact_for_role(trace, role, ctx.session.scanner)
        bundle["bundle_hash"] = hashlib.sha256(json.dumps({k: v for k, v in bundle.items() if k != "run"}, sort_keys=True, default=str).encode()).hexdigest()
        return bundle

    def replay(self, run_id: str) -> dict[str, Any]:
        """Re-execute the recorded action trace on a fresh session and compare hashes (Acceptance test 37)."""
        row = self.store.run(run_id)
        if row is None:
            raise ApiError(404, f"unknown run {run_id}", "NOT_FOUND")
        try:
            _pack_row, pack = self.load_pack(row["pack_id"])
        except (ApiError, ValueError, OSError):
            raise ApiError(410, "session episode unavailable on this instance", "PACK_FILES_MISSING") from None
        trace = self.store.trace(run_id)
        manifest = self.store.get_doc(run_id, "run_manifest") or {}
        replayed = replay_trace(pack, trace, bankroll_raw=int(row["bankroll_raw"]), mask_seed=row["mask_seed"], engine_seed=row["engine_seed"], mode=row["mode"], resource_profile=manifest.get("resource_profile"))
        original = json.loads(row["report_json"]) if row["report_json"] else None
        orig_hashes = (original or {}).get("reproducibility", {})
        return {
            "run_id": run_id,
            "trace_length": len(trace),
            "replayed_ledger_hash": replayed.sim.ledger.content_hash(),
            "replayed_state_hash": replayed.sim.state_hash(),
            "replayed_result_hash": replayed.result_hash(),
            "original": orig_hashes,
            "ledger_matches": orig_hashes.get("ledger_hash") == replayed.sim.ledger.content_hash(),
            "state_matches": orig_hashes.get("state_hash") == replayed.sim.state_hash(),
        }

    # ------------------------------------------------------------------ suites
    def suites_view(self) -> list[dict[str, Any]]:
        out = []
        for s in self.suites.values():
            if any((r := self.store.pack(n)) and r["visibility"] == "holdout" for n in s.packs):
                continue
            ids = []
            for name in s.packs:
                r = self.store.pack(name)
                ids.append(r["pack_id"] if r else None)
            out.append(suite_public(s, [i for i in ids if i]) | {"label": _suite_label(s) if all(name in FIXTURE_CONFIGS for name in s.packs) else s.suite_id, "all_packs_imported": all(ids)})
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
                if name in FIXTURE_CONFIGS:
                    r = self.store.pack(self.ensure_fixture_pack(name)["pack_id"])
                else:
                    raise ApiError(400, f"suite pack {name} is not imported", "NOT_IMPORTED")
            assert r is not None
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
                execute="inprocess" if (self.hosted and wait) else ("subprocess" if not self.hosted else "deferred"),
            )
            run_ids.append(view["run_id"])
            if wait and not self.hosted:
                self.wait_for_run(view["run_id"])
        self.store.insert_suite_run(suite_run_id, suite_id, agent_id, now_iso(), run_ids)
        return {"suite_run_id": suite_run_id, "suite_id": suite_id, "agent_id": agent_id, "run_ids": run_ids, "runs": [self.run_view(r) for r in run_ids]}

    ONE_NAME_WINDOW_S = 2 * 3600.0

    def enroll(self, *, agent: dict[str, Any], client_key: str | None = None) -> dict[str, Any]:
        """Join: register (or reuse) the agent and hand it its identity token. Creates no runs; that is
        ``play``, which an agent calls whenever it wants to trade what it has not finished yet.

        One name per agent: an address that enrolled under one name recently may not enroll a second
        name (a new version of the same name is fine), so a name stays one agent's reputation instead
        of one per strategy. Joining again with the same name and version issues a fresh identity
        token and retires the previous one.
        """
        name = str(agent.get("name", "")).strip()
        if client_key:
            now = time.time()
            recent = [k.split(":", 1)[1] for k in self.store.rate_event_kinds("enroll_name:", client_key, now - self.ONE_NAME_WINDOW_S)]
            if recent and name.lower() not in recent:
                raise ApiError(409, f"one name per agent: this address already enrolled as {recent[0]!r}. Keep that name; to try another strategy, enroll the same name with a new version.", "ONE_NAME")
            if name.lower() not in recent:
                self.store.add_rate_event("enroll_name:" + name.lower(), client_key, now)
        view = self.register_or_reuse_agent(name=name, version=str(agent.get("version", "1")), runtime=str(agent.get("runtime", "external")), capabilities=list(agent.get("capabilities") or []), config=dict(agent.get("config") or {}))
        agent_id = view["agent_id"]
        with self.store.run_lock("identity:" + agent_id):
            token = new_identity_token()
            self.store.set_agent_token_hash(agent_id, token_hash(token))
        return {
            "agent_id": agent_id,
            "agent_name": view["name"],
            "agent_version": view["version"],
            "agent_token": token,
            "episodes": self.episodes_for(agent_id),
            "play_url": self.gateway_url + "/api/v1/play",
            "results_url": f"{self.gateway_url}/?agent={agent_id}",
            "skill_url": self.gateway_url + "/skill.md",
            "note": "Joined. Keep agent_token: POST play_url with it (Authorization: Bearer) whenever you want to trade; it creates one run per episode you have not finished and returns their session credentials. Joining again with the same name and version replaces the token.",
        }

    def agent_by_token(self, token: str) -> dict[str, Any]:
        row = self.store.agent_by_token_hash(token_hash(token)) if token else None
        if row is None:
            raise ApiError(401, "unknown or retired agent token; join again (POST /api/v1/enroll with the same name and version) to get a new one", "UNAUTHORIZED")
        return row

    def rename_agent(self, *, agent_token: str, name: str) -> dict[str, Any]:
        """Change the name an agent shows under, keeping its id, token, runs and board rows. For an agent
        that joined with a suffix its user never asked for."""
        row = self.agent_by_token(agent_token)
        name = name.strip()
        if not name or len(name) > 64 or not name.isprintable():
            raise ApiError(400, "agent name must be 1-64 printable characters", "INVALID")
        other = self.store.agent_by_name_version(name, row["version"])
        if other and other["agent_id"] != row["agent_id"]:
            raise ApiError(409, f"another agent already uses the name {name!r} with version {row['version']!r}", "NAME_TAKEN")
        self.store.set_agent_name(row["agent_id"], name)
        return self.agent_view(row["agent_id"])

    def _episode_context(self, row: dict[str, Any]) -> dict[str, Any]:
        """What an agent's owner needs to choose an episode: size, launches, gas, and who is on its board.
        Read once per pack per process from the small pack files; never the tape."""
        cached = self._episode_ctx_cache.get(row["pack_id"])
        if cached is None:
            summary = json.loads(row.get("summary_json") or "{}")
            path = Path(row["path"])
            try:
                params = yaml.safe_load((path / "execution_params.yaml").read_text()) or {}
            except (OSError, ValueError):
                params = {}
            launches = None
            candidates = None
            try:
                inv = json.loads((path / "inventory.json").read_text())
                launches = ((inv.get("venues") or {}).get("all_launches_in_window_v1") or {}).get("pools")
                candidates = inv.get("candidate_count")
            except (OSError, ValueError):
                pass
            gas = int(params.get("gas_cost_raw") or 0)
            dec = int(summary.get("numeraire_decimals") or 18)
            baseline = read_market_baseline(path)
            cached = {
                "date": str(row.get("start_utc") or "")[:10] or None,
                "duration_ms": row.get("duration_ms"),
                "pools_tradable": summary.get("pools_executable"),
                "launches": launches,
                "pools_created": candidates,
                "tape_events": summary.get("tape_events"),
                "gas_per_fill_raw": str(gas),
                "gas_per_fill": f"{Decimal(gas) / 10**dec:.9f}".rstrip("0").rstrip(".") if gas else "0",
                "gas_basis": params.get("gas_basis"),
                "market": _market_public(baseline),
                "market_note": baseline_sentence(baseline),
            }
            self._episode_ctx_cache[row["pack_id"]] = cached
        return dict(cached)

    def _market_fields(self, row: dict[str, Any]) -> dict[str, Any]:
        """The day's naive market baseline for a leaderboard category, when the pack carries one."""
        if str(row.get("origin", "")) == "generated_fixture":
            return {}
        ctx = self._episode_context(row)
        return {"market": ctx.get("market"), "market_note": ctx.get("market_note")}

    def episodes_for(self, agent_id: str) -> list[dict[str, Any]]:
        """Every real episode on the server, with the context to choose between them and this agent's
        standing on each: new, running or finished (with its return)."""
        out = []
        for r in self.real_weeks():
            mine = self.store.runs(agent_id=agent_id, pack_id=r["pack_id"])
            done = [x for x in mine if x["state"] == str(RunState.COMPLETED) and (self._result_summary(x.get("report_json")) or {}).get("primary_metric") == "final_cash_return_v1"]
            open_ = [x for x in mine if x["state"] not in TERMINAL]
            status = "finished" if done else "running" if open_ else "new"
            board = self.leaderboard(pack_id=r["pack_id"])["rows"]
            # Episode selection gives context for the default timing profile only.
            decimals = int(json.loads(r["summary_json"]).get("numeraire_decimals") or 18)
            board = [x for x in board if x["group"]["resource_profile"] == "pack_defaults_v1"
                     and x["group"]["isolation"] == "trusted_external_client"
                     and x["group"]["bankroll_raw"] == str(10**decimals)
                     and x["group"]["engine"] == ENGINE_VERSION]
            item = {"pack_id": r["pack_id"], "pack_name": r["name"], "label": _episode_label(r), **self._episode_context(r)}
            item.update({
                "ranking_basis": "default timing profile, one whole cash unit, trusted external client, current evaluator",
                "agents_ranked": len(board),
                "top_return": board[0].get("median_return") if board else None,
                "your_status": status,
                "your_run_id": (done or open_ or [{}])[-1].get("run_id"),
                "your_return": (self._result_summary(done[-1].get("report_json")) or {}).get("headline_return") if done else None,
                "status": status,  # kept for older clients
                "run_id": (done or open_ or [{}])[-1].get("run_id"),
            })
            out.append(item)
        return out

    def play(self, *, agent_token: str, suite_id: str | None = None, pack_id: str | None = None, pack_ids: list[str] | None = None, client_key: str | None = None) -> dict[str, Any]:
        """Create one external-client run per episode this agent has not finished (or for one pack, or an
        operator suite) and return their one-time session credentials. Call it as often as you like: an
        episode you already completed is skipped, a new day on the server is played."""
        agent_id = self.agent_by_token(agent_token)["agent_id"]
        view = self.agent_view(agent_id)
        runs: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        suite_run_id: str | None = None
        if suite_id:
            s = self.suites.get(suite_id)
            if s is None:
                raise ApiError(404, f"unknown suite {suite_id}", "NOT_FOUND")
            suite_run_id = "srun_" + secrets.token_hex(6)
            for name in s.packs:
                r = self.store.pack(name)
                if r is None:
                    if name in FIXTURE_CONFIGS:
                        r = self.store.pack(self.ensure_fixture_pack(name)["pack_id"])
                    else:
                        raise ApiError(400, f"suite pack {name} is not imported", "NOT_IMPORTED")
                assert r is not None
                runs.append(self.create_run(agent_id=agent_id, pack_ref=r["pack_id"], mode=s.mode, bankroll_raw=s.bankroll_raw, mask_seed=f"{s.mask_seed}:{name}", engine_seed=f"{s.engine_seed}:{name}", isolation=s.isolation, suite_id=suite_id, suite_run_id=suite_run_id))
            self.store.insert_suite_run(suite_run_id, suite_id, agent_id, now_iso(), [r["run_id"] for r in runs])
        elif pack_ids or pack_id:
            # An explicit choice is played as asked, finished or not: a new attempt on that episode.
            for ref in list(pack_ids or []) + ([pack_id] if pack_id else []):
                runs.append(self.create_run(agent_id=agent_id, pack_ref=ref))
        else:
            # No choice made: every real episode on the server this agent has not finished; on a server
            # without one yet, the generated suite.
            weeks = self.real_weeks()
            if not weeks:
                if "generated-practice-v1" in self.suites:
                    return self.play(agent_token=agent_token, suite_id="generated-practice-v1", client_key=client_key)
                raise ApiError(409, "no episode is available on this server yet; pass suite_id or pack_id", "NO_WEEKS")
            for r in weeks:
                done = [x for x in self.store.runs(agent_id=agent_id, pack_id=r["pack_id"]) if x["state"] == str(RunState.COMPLETED) and (self._result_summary(x.get("report_json")) or {}).get("primary_metric") == "final_cash_return_v1"]
                if done:
                    skipped.append({"pack_id": r["pack_id"], "pack_name": r["name"], "reason": "already finished by this agent", "run_id": done[-1]["run_id"]})
                    continue
                runs.append(self.create_run(agent_id=agent_id, pack_ref=r["pack_id"]))
        return {
            "agent_id": agent_id,
            "agent_name": view["name"],
            "agent_version": view["version"],
            "suite_id": suite_id,
            "suite_run_id": suite_run_id,
            "runs": [{"run_id": r["run_id"], "pack_id": r["pack_id"], "pack_name": r["pack_name"], "episode_id": r["episode_id"], "mode": r["mode"], "bankroll_raw": r["bankroll_raw"], "session_credential": r["session_credential"]} for r in runs],
            "skipped": skipped,
            "results_url": f"{self.gateway_url}/?agent={agent_id}",
            "note": "Each session_credential is shown once and works only for its run. Call session.finish when done; the report appears at results_url." + (" Episodes you already finished are under skipped; play a new version of your name to trade them again." if skipped else "") + ("" if runs else " Nothing new to play right now."),
        }

    # ------------------------------------------------------------------ leaderboard
    def real_weeks(self) -> list[dict[str, Any]]:
        """Recorded weeks that agents can trade, newest first (the public catalogue)."""
        rows = [r for r in self.store.packs() if r["visibility"] == "public" and str(r.get("origin", "")) != "generated_fixture" and r["use_status"] in ("demo", "research", "qualified_for_named_suite")]
        return sorted(rows, key=lambda r: str(r.get("start_utc") or ""), reverse=True)

    def leaderboard_categories(self, include_artificial: bool = True) -> list[dict[str, Any]]:
        """What can be ranked: every real week first, newest first, then the generated data (artificial
        weeks made for testing agents), each labelled in plain words."""
        cats: list[dict[str, Any]] = []
        for r in self.real_weeks():
            cats.append({"kind": "pack", "id": r["pack_id"], "label": _episode_label(r), "description": _episode_description(r), "episodes": [r["name"]], **self._market_fields(r)})
        if include_artificial:
            for s in self.suites.values():
                if any((p := self.store.pack(n)) and p["visibility"] == "holdout" for n in s.packs):
                    continue
                if s.sealed or len(s.packs) < 2:  # a one-episode suite is its episode's own tab
                    continue
                cats.append({"kind": "suite", "id": s.suite_id, "label": _suite_label(s), "description": s.description, "episodes": list(s.packs)})
            for r in self.store.packs():
                if r["visibility"] != "public":
                    continue
                if str(r.get("origin", "")) == "generated_fixture":
                    cats.append({"kind": "pack", "id": r["pack_id"], "label": _episode_label(r), "description": _episode_description(r), "episodes": [r["name"]], **self._market_fields(r)})
        return cats

    def leaderboard(self, *, suite_id: str | None = None, pack_id: str | None = None) -> dict[str, Any]:
        """Agents ranked by median return after modeled costs over their latest completed final-cash-scored
        run per episode. Ranking never claims an edge: the footnote travels with the table."""
        if suite_id:
            s = self.suites.get(suite_id)
            if s is None:
                raise ApiError(404, f"unknown suite {suite_id}", "NOT_FOUND")
            episode_names = list(s.packs)
            for name in episode_names:
                self.require_public("packs", name)
            category = {"kind": "suite", "id": suite_id, "label": _suite_label(s), "description": s.description, "episodes": episode_names}
            wanted_packs = {r["pack_id"] for n in episode_names if (r := self.store.pack(n))}
        elif pack_id:
            self.require_public("packs", pack_id)
            r = self.store.pack(pack_id)
            if r is None:
                raise ApiError(404, f"unknown pack {pack_id}", "NOT_FOUND")
            wanted_packs = {r["pack_id"]}
            category = {"kind": "pack", "id": r["pack_id"], "label": _episode_label(r), "description": _episode_description(r), "episodes": [r["name"]], **self._market_fields(r)}
        else:
            public_packs = [r for r in self.store.packs() if r["visibility"] == "public"]
            wanted_packs = {r["pack_id"] for r in public_packs}
            category = {"kind": "all", "id": "all", "label": "Every episode", "description": "All recorded and generated episodes, with separate rankings for comparable runs.", "episodes": [r["name"] for r in public_packs]}
        # latest valued run per (agent, pack)
        best: dict[tuple[str, str, str], dict[str, Any]] = {}
        groups: dict[str, dict[str, Any]] = {}
        attempts: dict[str, int] = {}
        for row in self.store.runs():
            if row["pack_id"] not in wanted_packs or self.is_private_run(row["run_id"]):
                continue
            attempts[row["agent_id"]] = attempts.get(row["agent_id"], 0) + 1
            if row["state"] != str(RunState.COMPLETED) or not row["report_json"]:
                continue
            summ = self._result_summary(row["report_json"])
            if not summ or summ["primary_metric"] != "final_cash_return_v1" or summ["headline_return"] is None or not summ["ranking_eligible"]:
                continue
            report = json.loads(row["report_json"])
            group = {"resource_profile": report.get("resource_profile", PROFILES["pack_defaults_v1"].public())["profile_id"],
                     "resource_fingerprint": report.get("resource_profile", PROFILES["pack_defaults_v1"].public())["fingerprint"],
                     "origin": report.get("status_dimensions", {}).get("data_origin"),
                     "execution_model": report.get("status_dimensions", {}).get("execution_model"),
                     "isolation": row["isolation"], "bankroll_raw": row["bankroll_raw"],
                     "numeraire": report.get("outcome", {}).get("numeraire"),
                     "decimals": report.get("outcome", {}).get("numeraire_decimals"),
                     "engine": report.get("versions", {}).get("engine")}
            gid = hashlib.sha256(json.dumps(group, sort_keys=True).encode()).hexdigest()[:16]
            groups[gid] = group
            key = (row["agent_id"], row["pack_id"], gid)
            if key not in best or (row["finished_at"] or "") > (best[key]["finished_at"] or ""):
                best[key] = {"finished_at": row["finished_at"], "run_id": row["run_id"], **summ}
        per_agent: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for (aid, _pid, gid), v in best.items():
            per_agent.setdefault((aid, gid), []).append(v)
        rows = []
        for (aid, gid), vals in per_agent.items():
            group = groups[gid]
            episode_count = sum(1 for pid in wanted_packs if (p := self.store.pack(pid)) and p["origin"] == group["origin"] and p["execution_model"] == group["execution_model"])
            a = self.store.agent(aid)
            rets = sorted(Decimal(v["headline_return"]) for v in vals)
            mid = len(rets) // 2
            median = rets[mid] if len(rets) % 2 else (rets[mid - 1] + rets[mid]) / 2
            dds = [Decimal(v["max_drawdown"]) for v in vals if v.get("max_drawdown") is not None]
            rows.append(
                {
                    "agent_id": aid,
                    "agent_name": a["name"] if a else aid,
                    "agent_version": a["version"] if a else "",
                    "runtime": a["runtime"] if a else "",
                    "episodes_valued": len(vals),
                    "episodes_total": episode_count,
                    "covers_all": len(vals) >= episode_count,
                    "comparison_group": gid,
                    "group": group,
                    "runs_attempted": attempts.get(aid, 0),
                    "median_return": f"{median:.6f}",
                    "mean_return": f"{sum(rets) / len(rets):.6f}",
                    "best_return": f"{rets[-1]:.6f}",
                    "worst_return": f"{rets[0]:.6f}",
                    "worst_drawdown": f"{max(dds):.6f}" if dds else None,
                    "fills": sum(int(v.get("confirmed_fills") or 0) for v in vals),
                    "last_finished_at": max((v["finished_at"] or "") for v in vals) or None,
                    "run_ids": [v["run_id"] for v in vals],
                }
            )
        rows.sort(key=lambda r: (r["comparison_group"], not r["covers_all"], -Decimal(r["median_return"]), r["agent_name"]))
        ranks: dict[str, int] = {}
        for r in rows:
            gid = r["comparison_group"]
            ranks[gid] = ranks.get(gid, 0) + 1
            r["rank"] = ranks[gid]
        return {
            "category": category,
            "categories": self.leaderboard_categories(),
            "rows": rows,
            "note": "Ranks restart within each resource profile, data origin, execution model, bankroll, numeraire, engine version and isolation group. Material fidelity failures and budget exhaustion are excluded. Ranked by median final ETH/cash return after modeled costs over each agent's latest completed final-cash-scored run per episode. Agents that covered every episode rank above partial coverage. Unsold tokens do not count. Legacy portfolio-scored runs require a new run and are excluded. Generated episodes are artificial; a high rank is not an edge and predicts nothing.",
        }

    # ------------------------------------------------------------------ comparisons / studies
    def compare(self, *, suite_id: str | None, agent_a: str, agent_b: str, run_ids_a: list[str] | None = None, run_ids_b: list[str] | None = None) -> dict[str, Any]:
        def collect(agent_id: str, explicit: list[str] | None) -> list[dict[str, Any]]:
            rows = [self.store.run(r) for r in explicit] if explicit else self.store.runs(agent_id=agent_id, **({"suite_id": suite_id} if suite_id else {}))
            out = []
            for r in rows:
                if r is None:
                    continue
                if self.is_private_run(r["run_id"]):
                    if explicit:
                        raise ApiError(404, "unknown resource", "NOT_FOUND")
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
        for ref in pack_ids:
            self.require_public("packs", ref)
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


def _pool_executable(p: Any) -> bool:
    """A pool the engine can trade: a CPMM pool with reserves, or a concentrated-liquidity pool (its
    state comes from the record or from its cl_init row inside the window)."""
    return bool((p.supported_by_cpmm and p.initial_reserve0 is not None) or p.supported_by_clmm)


def _initialized_on_tape(pack: Pack, key: str) -> bool:
    return any(r["kind"] == "cl_init" and r["pool"] == key for r in pack.iter_tape())


FIXTURE_SCENARIO_NAMES = {
    "gen_week_trending": "trending market",
    "gen_week_reversal": "pump then reversal",
    "gen_week_sparse_missing": "thin market with gaps",
    "gen_week_liquidity_shift": "liquidity moves between pools",
    "gen_dev_short": "short warm-up",
}


def _state_in_words(state: str, error: str | None) -> str:
    """What happened to a run, for someone who has never seen the state names."""
    words = {
        "queued": "Waiting for the agent to connect and start trading",
        "running": "Still playing: the agent is trading through the day right now",
        "paused": "Paused: the agent stopped calling and can resume",
        "completed": "Finished",
        "agent_failed": "Stopped because the agent failed or went away before the day ended",
        "environment_failed": "Stopped because the server hit an error; not the agent's fault",
        "budget_exhausted": "Stopped because the run used up its compute budget",
        "aborted": "Cancelled before the day ended",
    }
    out = words.get(state, state.replace("_", " ").capitalize())
    if error and state in ("agent_failed", "environment_failed"):
        out += f" ({error})"
    return out


def _activity_in_words(summ: dict[str, Any]) -> str:
    fills, orders = int(summ.get("confirmed_fills") or 0), int(summ.get("orders_total") or 0)
    if orders == 0:
        return "Placed no orders and held ETH the whole day"
    parts = [f"{fills} of {orders} orders filled"]
    dd = summ.get("max_drawdown")
    if dd is not None:
        parts.append(f"worst drop from a peak {float(dd) * 100:.1f}%")
    if int(summ.get("unpriced_inventory") or 0):
        parts.append(f"{summ['unpriced_inventory']} holding(s) left unsold, which count for nothing")
    return "; ".join(parts)


def _day_summary(day: dict[str, Any], counted: dict[str, Any] | None) -> str:
    """One sentence per day: the ranked result against the market, or why there is none yet."""
    n = len(day["runs"])
    attempts = f"{n} attempt{'s' if n != 1 else ''}"
    if counted is None:
        open_ = [r for r in day["runs"] if r["state"] in ("queued", "running", "paused")]
        if open_:
            return f"{attempts}, one still in progress. No ranked result yet."
        return f"{attempts}, none finished with a ranked result."
    ret = float(counted["headline_return"]) * 100
    out = f"Ranked result {ret:+.2f}% from {attempts}."
    mr = day.get("market_return")
    if mr is not None:
        m = float(mr) * 100
        rel = "better than" if ret > m else "worse than" if ret < m else "the same as"
        out += f" That is {rel} the naive market reference of {m:+.0f}% (a stake in every launch, sold at the close, before gas) and {'better than' if ret > 0 else 'worse than' if ret < 0 else 'the same as'} holding ETH."
    return out


def _market_public(baseline: dict[str, Any] | None) -> dict[str, Any] | None:
    """The market baseline without the pack id: it is agent-visible and must carry no private key."""
    if not baseline:
        return None
    return {k: v for k, v in baseline.items() if k != "pack_id"}


def _episode_label(row: dict[str, Any]) -> str:
    """'gen_week_trending' -> 'Week: trending'; real data -> 'Base week of 2026-09-07, v4 pools'."""
    name, is_full_week, duration_ms = row["name"], row["is_full_week"], row["duration_ms"]
    if str(row.get("origin", "")) != "generated_fixture":
        try:
            models = json.loads(row.get("summary_json") or "{}").get("universe", {}).get("pool_models") or []
        except (TypeError, ValueError):
            models = []
        kinds = {("v4" if "v4" in str(m) else "v3" if "v3" in str(m) else "v2") for m in models}
        protocol = "uniswap_v2" if len(kinds) != 1 else f"uniswap_{kinds.pop()}"
        return week_label(name, str(row.get("chain") or ""), protocol, row.get("start_utc"), row.get("end_utc"))
    base = name.replace("gen_week_", "").replace("gen_", "").replace("_", " ")
    scenario = FIXTURE_SCENARIO_NAMES.get(name, base)
    if is_full_week:
        return f"Generated week: {scenario} (artificial)"
    hours = float(duration_ms or 0) / 3_600_000
    return f"Generated: {scenario}, {hours:g} hours (artificial)" if hours else f"Generated: {scenario} (artificial)"


def _episode_description(row: dict[str, Any]) -> str:
    """One sentence under the category tab: the scenario for fixtures, provenance for real data. Never dates."""
    try:
        summary = json.loads(row.get("summary_json") or "{}")
    except (TypeError, ValueError):
        summary = {}
    if str(row.get("origin", "")) == "generated_fixture":
        scenario = summary.get("scenario") or ""
        return f"Generated data: an artificial market with known rules, made for testing agents. {scenario}".strip()
    hours = float(row.get("duration_ms") or 0) / 3_600_000
    span = "7 days" if row.get("is_full_week") else f"{hours / 24:g} day{'s' if hours != 24 else ''}" if hours >= 24 and hours % 24 == 0 else f"{hours:g} hours"
    pools = summary.get("pools_executable")
    start, end = str(row.get("start_utc") or "")[:10], str(row.get("end_utc") or "")[:10]
    when = f" from {start} to {end}" if start and end else ""
    return f"Real swaps recorded on {str(row.get('chain') or '').capitalize()}{when} ({span}), replayed through the execution model with {pools} tradable pools. Inside a session the pools and tokens carry generic names, so an agent cannot look them up. Gas and token taxes assumed standard. Not historical performance."


def _suite_label(s: SuiteDef) -> str:
    n = len(s.packs)
    weeks = all(name.startswith("gen_week_") for name in s.packs)
    return f"Generated: all {n} artificial weeks" if weeks else f"Generated: all {n} episodes ({s.suite_id})"
