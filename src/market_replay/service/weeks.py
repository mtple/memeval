"""Real weeks on demand: anyone asks for a past week, the server collects it in resumable slices.

A collection job runs the historical collector for at most `slice_seconds` per invocation (a
serverless function has a hard duration limit), archives the collector's working state into the
store, and continues on the next tick, from any instance. Ticks come from a cron and from open
pages. When the collector finishes, the pack is validated, imported and archived like an upload,
and it appears as a leaderboard category.
"""

from __future__ import annotations

import io
import json
import shutil
import tarfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import yaml

from ..collectors.historical import run_collection
from .runs import ApiError, RunManager, now_iso

RESUMABLE = ("queued", "collecting")
MAX_PROVIDER_RETRIES = 6


class WeekJobs:
    def __init__(
        self,
        manager: RunManager,
        *,
        rpc_url: str | None,
        slice_seconds: float = 240.0,
        max_requests: int = 20000,
        log_chunk_blocks: int = 10000,
        max_pairs: int = 16,
        max_jobs_per_day: int = 3,
        selection_rule: str = "active_before_window_earliest_created_v1",
        transport: httpx.BaseTransport | None = None,
        sleep=None,
    ) -> None:
        self.m = manager
        self.rpc_url = rpc_url
        self.slice_seconds = slice_seconds
        self.max_requests = max_requests
        self.log_chunk_blocks = log_chunk_blocks
        self.max_pairs = max_pairs
        self.max_jobs_per_day = max_jobs_per_day
        self.selection_rule = selection_rule
        self.transport = transport
        self.sleep = sleep

    @property
    def enabled(self) -> bool:
        return bool(self.rpc_url)

    # ------------------------------------------------------------------ requests
    def request(self, start_utc: str, end_utc: str | None = None, *, requested_by: str = "", chain: str = "base") -> dict[str, Any]:
        """Queue a period (a week from `start_utc` unless `end_utc` is given). Idempotent per period."""
        if not self.enabled:
            raise ApiError(503, "no RPC endpoint is configured on this server (BASE_RPC_URL); real weeks cannot be collected", "WEEKS_DISABLED")
        start = _parse(start_utc)
        end = _parse(end_utc) if end_utc else start + timedelta(days=7)
        if end <= start:
            raise ApiError(400, "period end must be after its start", "INVALID")
        if end > datetime.now(UTC) - timedelta(hours=12):
            raise ApiError(400, "the period must have ended at least 12 hours ago", "INVALID")
        if (end - start) > timedelta(days=7, hours=1):
            raise ApiError(400, "a period is at most one week", "INVALID")
        is_week = (end - start) >= timedelta(days=7)
        existing = self.m.store.week_job_by_period(chain, _iso(start), _iso(end))
        if existing:
            return self.view(existing)
        # Sealed naming: the public name carries a sequence number, never the calendar. Agents (and their
        # owners' prompts) see "base_week_03"; only the operator can read which dates that is.
        seq = len(self.m.store.week_jobs()) + 1
        hours = int((end - start).total_seconds() // 3600)
        name = f"{chain}_week_{seq:02d}" if is_week else f"{chain}_period_{seq:02d}_{hours}h"
        job_id = "wk_" + name
        day_ago = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        if self.m.store.week_jobs_created_since(day_ago) >= self.max_jobs_per_day:
            raise ApiError(429, f"at most {self.max_jobs_per_day} new weeks a day on this server; try tomorrow", "USAGE_CAP")
        cfg = {
            "rpc_url_env": "BASE_RPC_URL",
            "chain": chain,
            "protocol": "uniswap_v2",
            "period_start_utc": _iso(start),
            "period_end_utc": _iso(end),
            "discovery_window_start_utc": _iso(start - timedelta(days=7 if is_week else 1)),
            "prehistory_hours": 24 if is_week else 1,
            "selection_rule": self.selection_rule,
            "activity_lookback_blocks": 43200 if is_week else 5400,
            "max_pairs": self.max_pairs,
            "max_requests": self.max_requests,
            "log_chunk_blocks": self.log_chunk_blocks,
            "initial_state_lookback_blocks": 20000,
            "availability_delay_ms": 4000,
            "out_dir": f"packs/historical/{name}",
            "authorization_note": "Operator-configured read-only RPC endpoint with a fixed request budget; public chain data; redistribution not cleared.",
        }
        now = now_iso()
        self.m.store.insert_week_job({"job_id": job_id, "name": name, "chain": chain, "period_start_utc": cfg["period_start_utc"], "period_end_utc": cfg["period_end_utc"], "status": "queued", "config_json": json.dumps(cfg, sort_keys=True), "requests_used": 0, "attempts": 0, "note": "queued; the next tick starts collecting", "error": None, "pack_id": None, "requested_by": requested_by, "created_at": now, "updated_at": now})
        return self.view(self.m.store.week_job(job_id))

    def jobs(self, role: str = "public") -> list[dict[str, Any]]:
        return [self.view(r, role) for r in self.m.store.week_jobs()]

    def job(self, job_id: str, role: str = "public") -> dict[str, Any]:
        row = self.m.store.week_job(job_id)
        if row is None:
            raise ApiError(404, f"unknown week job {job_id}", "NOT_FOUND")
        return self.view(row, role)

    def view(self, row: dict[str, Any], role: str = "public") -> dict[str, Any]:
        """Public views never carry the calendar; the operator's view does."""
        cfg = json.loads(row["config_json"])
        start, end = _parse(row["period_start_utc"]), _parse(row["period_end_utc"])
        hours = int((end - start).total_seconds() // 3600)
        out = {
            "job_id": row["job_id"],
            "name": row["name"],
            "label": week_label(row["name"], row["chain"]),
            "chain": row["chain"],
            "duration_hours": hours,
            "dates_sealed": True,
            "status": row["status"],
            "requests_used": row["requests_used"],
            "request_budget": cfg["max_requests"],
            "attempts": row["attempts"],
            "note": row["note"],
            "error": row["error"],
            "pack_id": row["pack_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        if role == "admin":
            out.update(period_start_utc=row["period_start_utc"], period_end_utc=row["period_end_utc"], requested_by=row["requested_by"], dates_sealed=False)
        return out

    # ------------------------------------------------------------------ slices
    def tick(self, slice_seconds: float | None = None) -> dict[str, Any]:
        """Advance the oldest unfinished job by one time slice. Safe to call from anywhere, any time."""
        pending = [r for r in self.m.store.week_jobs() if r["status"] in RESUMABLE]
        if not pending:
            return {"advanced": None, "pending": 0}
        pending.sort(key=lambda r: r["created_at"])
        row = pending[0]
        with self.m.store.run_lock("weekjob:" + row["job_id"]):
            row = self.m.store.week_job(row["job_id"]) or row
            if row["status"] not in RESUMABLE:
                return {"advanced": None, "pending": len(pending) - 1}
            return {"advanced": self._run_slice(row, slice_seconds or self.slice_seconds), "pending": len(pending)}

    def _run_slice(self, row: dict[str, Any], slice_seconds: float) -> dict[str, Any]:
        cfg = json.loads(row["config_json"])
        name = row["name"]
        data_dir = self.m.data_dir
        out_dir = data_dir / cfg["out_dir"]
        work = out_dir.parent / (out_dir.name + "_work")
        archive = self.m.store.week_job_archive(row["job_id"])
        if archive and not (work / "checkpoints.json").exists():
            _restore(archive, work)
        cfg_dir = data_dir / "collection" / "weeks"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        cfg_path = cfg_dir / f"{name}.yaml"
        cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False))
        self.m.store.update_week_job(row["job_id"], status="collecting", updated_at=now_iso())
        res = run_collection(cfg_path, data_dir, transport=self.transport, rpc_url_override=self.rpc_url, sleep=self.sleep, deadline=time.monotonic() + slice_seconds, store_bodies=False, budget_used=int(row["requests_used"]))
        status = res["status"]
        used = int(res.get("budget", {}).get("requests", row["requests_used"]))
        log = res.get("decision_log") or []
        note = log[-1] if log else status
        fields: dict[str, Any] = {"requests_used": used, "note": note[:400], "updated_at": now_iso()}
        if status == "pack_built":
            pack_archive = _targz(Path(res["pack_dir"]))
            view = self.m.upload_pack(pack_archive, name)
            fields.update(status="built", pack_id=view["pack_id"], note=f"pack built: {res['tape_events']} events in {res['pools']} pools; qualification {res['qualification']}")
            shutil.rmtree(work, ignore_errors=True)
        elif status == "in_progress_resumable":
            fields.update(status="collecting")
            self.m.store.set_week_job_archive(row["job_id"], _targz(work))
        elif status == "provider_error_resumable":
            attempts = int(row["attempts"]) + 1
            fields.update(attempts=attempts, status="collecting" if attempts < MAX_PROVIDER_RETRIES else "failed", error=str(res.get("reason"))[:400])
            self.m.store.set_week_job_archive(row["job_id"], _targz(work))
        else:  # budget_exhausted_resumable, blocked
            fields.update(status="failed", error=f"{status}: {res.get('reason')}"[:400])
            self.m.store.set_week_job_archive(row["job_id"], _targz(work))
        self.m.store.update_week_job(row["job_id"], **fields)
        return self.view(self.m.store.week_job(row["job_id"]))


def week_label(name: str, chain: str) -> str:
    """'base_week_03' -> 'Base week 3'; 'base_period_02_1h' -> 'Base period 2 (1h)'."""
    parts = name.split("_")
    if len(parts) >= 3 and parts[1] == "week" and parts[2].isdigit():
        return f"{chain.capitalize()} week {int(parts[2])}"
    if len(parts) >= 4 and parts[1] == "period" and parts[2].isdigit():
        return f"{chain.capitalize()} period {int(parts[2])} ({parts[3]})"
    return name.replace("_", " ")


def _parse(s: str) -> datetime:
    s = s.strip()
    try:
        if len(s) == 10:
            return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=UTC)
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError as e:
        raise ApiError(400, f"bad date {s!r}; use YYYY-MM-DD", "INVALID") from e


def _iso(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def _targz(directory: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        tf.add(directory, arcname=directory.name)
    return buf.getvalue()


def _restore(archive: bytes, work: Path) -> None:
    work.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tf:
        root = tf.getmembers()[0].name.split("/")[0] if tf.getmembers() else work.name
        for m in tf.getmembers():
            rel = m.name[len(root):].lstrip("/")
            if not rel or ".." in Path(rel).parts:
                continue
            dest = work / rel
            if m.isdir():
                dest.mkdir(parents=True, exist_ok=True)
            elif m.isfile():
                dest.parent.mkdir(parents=True, exist_ok=True)
                src = tf.extractfile(m)
                if src is not None:
                    dest.write_bytes(src.read())
