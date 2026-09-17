"""Real weeks on demand: anyone asks for a past week, the server collects it in resumable slices.

A collection job runs the historical collector for at most `slice_seconds` per invocation (a
serverless function has a hard duration limit), archives the collector's working state into the
store, and continues on the next tick, from any instance. Ticks come from a cron and from open
pages. When the collector finishes, the pack is validated, imported and archived like an upload,
and it appears as a leaderboard category.
"""

from __future__ import annotations

import gzip
import hashlib
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
# Bump when the collector or the reconciliation changes what a built week contains. A week that came
# out diagnostic_only under an older version is collected again once, automatically, on an idle tick.
COLLECTOR_VERSION = "2026-09-17.2"
# Venues a week can be recorded from. v2 pairs are constant-product; v3/v4 pools are concentrated
# liquidity (v4 is where Clanker/Bankr launches trade, behind hooks).
PROTOCOLS = {
    "uniswap_v2": "v2 pairs",
    "uniswap_v3": "v3 pools",
    "uniswap_v4": "v4 pools",
}
MAX_PROVIDER_RETRIES = 6
LEASE_GRACE_SECONDS = 90  # a slice may overrun its deadline by one request plus the save


class WeekJobs:
    def __init__(
        self,
        manager: RunManager,
        *,
        rpc_url: str | None,
        slice_seconds: float = 200.0,
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
        # Provider caps on eth_getLogs block ranges (research, Sept 2026): Coinbase Developer Platform 1,000;
        # QuickNode paid 10,000; Alchemy 2,000 with no result cap. Start at the cap instead of halving into it.
        host = (rpc_url or "").split("//")[-1].split("/")[0].lower()
        if "coinbase.com" in host:
            log_chunk_blocks = min(log_chunk_blocks, 1000)
        elif "alchemy.com" in host:
            log_chunk_blocks = min(log_chunk_blocks, 2000)
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
    def request(self, start_utc: str, end_utc: str | None = None, *, requested_by: str = "", chain: str = "base", protocol: str = "uniswap_v2") -> dict[str, Any]:
        """Queue a period (a week from `start_utc` unless `end_utc` is given). Idempotent per period and venue."""
        if not self.enabled:
            raise ApiError(503, "no RPC endpoint is configured on this server (BASE_RPC_URL); real weeks cannot be collected", "WEEKS_DISABLED")
        if protocol not in PROTOCOLS:
            raise ApiError(400, f"unknown venue {protocol!r}; one of {sorted(PROTOCOLS)}", "INVALID")
        start = _parse(start_utc)
        end = _parse(end_utc) if end_utc else start + timedelta(days=7)
        if end <= start:
            raise ApiError(400, "period end must be after its start", "INVALID")
        if end > datetime.now(UTC) - timedelta(hours=12):
            raise ApiError(400, "the period must have ended at least 12 hours ago", "INVALID")
        if (end - start) > timedelta(days=7, hours=1):
            raise ApiError(400, "a period is at most one week", "INVALID")
        is_week = (end - start) >= timedelta(days=7)
        existing = self.m.store.week_job_by_period(chain, _iso(start), _iso(end), protocol)
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
            "protocol": protocol,
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
            "collector_version": COLLECTOR_VERSION,
            "availability_delay_ms": 4000,
            "out_dir": f"packs/historical/{name}",
            "authorization_note": "Operator-configured read-only RPC endpoint with a fixed request budget; public chain data; redistribution not cleared.",
        }
        now = now_iso()
        self.m.store.insert_week_job({"job_id": job_id, "name": name, "chain": chain, "protocol": protocol, "period_start_utc": cfg["period_start_utc"], "period_end_utc": cfg["period_end_utc"], "status": "queued", "config_json": json.dumps(cfg, sort_keys=True), "requests_used": 0, "attempts": 0, "note": "queued; the next tick starts collecting", "error": None, "pack_id": None, "requested_by": requested_by, "created_at": now, "updated_at": now})
        return self.view(self.m.store.week_job(job_id))

    def jobs(self, role: str = "public") -> list[dict[str, Any]]:
        return [self.view(r, role) for r in self.m.store.week_jobs()]

    def job(self, job_id: str, role: str = "public") -> dict[str, Any]:
        row = self.m.store.week_job(job_id)
        if row is None:
            raise ApiError(404, f"unknown week job {job_id}", "NOT_FOUND")
        return self.view(row, role)

    def _stale_diagnostic_job(self) -> dict[str, Any] | None:
        """A built week whose pack is diagnostic_only and which an older collector produced."""
        for row in self.m.store.week_jobs():
            if row["status"] != "built" or not row.get("pack_id"):
                continue
            cfg = json.loads(row["config_json"])
            if cfg.get("collector_version") == COLLECTOR_VERSION:
                continue
            if self._qualification(row["pack_id"]) == "diagnostic_only":
                return row
        return None

    def rebuild(self, job_id: str) -> dict[str, Any]:
        """Collect a finished (built or failed) week again with the current collector. The old pack is
        forgotten (reports of runs on it survive); the sealed name and sequence number stay."""
        row = self.m.store.week_job(job_id)
        if row is None:
            raise ApiError(404, f"unknown week job {job_id}", "NOT_FOUND")
        if row["status"] in RESUMABLE:
            raise ApiError(409, "this week is still being collected", "BUSY")
        if self._slice_in_flight(row):
            raise ApiError(409, "a slice is still finishing; try again in a minute", "BUSY")
        if row.get("pack_id"):
            self.m.forget_pack(row["pack_id"])
        self.m.store.delete_week_job_files(job_id)
        self.m.store.set_week_job_archive(job_id, None)
        cfg = json.loads(row["config_json"])
        shutil.rmtree(self.m.data_dir / cfg["out_dir"], ignore_errors=True)
        shutil.rmtree(self.m.data_dir / (cfg["out_dir"] + "_work"), ignore_errors=True)
        cfg["collector_version"] = COLLECTOR_VERSION
        self.m.store.update_week_job(job_id, status="queued", requests_used=0, attempts=0, note="queued for a rebuild; the next tick starts collecting", error=None, pack_id=None, updated_at=now_iso(), lease_until=None, config_json=json.dumps(cfg, sort_keys=True))
        return self.view(self.m.store.week_job(job_id))

    def view(self, row: dict[str, Any], role: str = "public") -> dict[str, Any]:
        """Public views never carry the calendar; the operator's view does."""
        cfg = json.loads(row["config_json"])
        start, end = _parse(row["period_start_utc"]), _parse(row["period_end_utc"])
        hours = int((end - start).total_seconds() // 3600)
        out = {
            "job_id": row["job_id"],
            "name": row["name"],
            "label": week_label(row["name"], row["chain"], row.get("protocol") or cfg.get("protocol") or "uniswap_v2"),
            "chain": row["chain"],
            "protocol": row.get("protocol") or cfg.get("protocol") or "uniswap_v2",
            "duration_hours": hours,
            "dates_sealed": True,
            "status": row["status"],
            "requests_used": row["requests_used"],
            "request_budget": cfg["max_requests"],
            "attempts": row["attempts"],
            "note": row["note"],
            "error": row["error"],
            "pack_id": row["pack_id"],
            "qualification": self._qualification(row["pack_id"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        if role == "admin":
            out.update(period_start_utc=row["period_start_utc"], period_end_utc=row["period_end_utc"], requested_by=row["requested_by"], dates_sealed=False)
        return out

    def _qualification(self, pack_id: str | None) -> str | None:
        if not pack_id:
            return None
        pack = self.m.store.pack(pack_id)
        return str(pack["use_status"]) if pack else None

    # ------------------------------------------------------------------ slices
    def tick(self, slice_seconds: float | None = None) -> dict[str, Any]:
        """Advance the oldest unfinished job by one time slice. Safe to call from anywhere, any time."""
        pending = [r for r in self.m.store.week_jobs() if r["status"] in RESUMABLE]
        if not pending:
            stale = self._stale_diagnostic_job()
            if stale is None:
                return {"advanced": None, "pending": 0}
            view = self.rebuild(stale["job_id"])
            return {"advanced": view, "pending": 1, "rebuilt": True}
        pending.sort(key=lambda r: r["created_at"])
        row = pending[0]
        if self._slice_in_flight(row):
            return {"advanced": None, "busy": True, "pending": len(pending), "job_id": row["job_id"]}
        # Never queue behind a running slice: a function that waits for the lock would only burn its
        # own duration limit. Whoever holds the lock reports progress; everyone else says "busy".
        with self.m.store.try_run_lock("weekjob:" + row["job_id"]) as held:
            if not held:
                return {"advanced": None, "busy": True, "pending": len(pending), "job_id": row["job_id"]}
            row = self.m.store.week_job(row["job_id"]) or row
            if row["status"] not in RESUMABLE or self._slice_in_flight(row):
                return {"advanced": None, "busy": True, "pending": len(pending), "job_id": row["job_id"]}
            return {"advanced": self._run_slice(row, slice_seconds or self.slice_seconds), "pending": len(pending)}

    def _slice_in_flight(self, row: dict[str, Any]) -> bool:
        """Another instance holds a lease on this job (set at slice start, cleared at slice end)."""
        lease = row.get("lease_until")
        if not lease:
            return False
        try:
            until = datetime.fromisoformat(str(lease).replace("Z", "+00:00"))
        except ValueError:
            return False
        return datetime.now(UTC) < until

    def _run_slice(self, row: dict[str, Any], slice_seconds: float) -> dict[str, Any]:
        cfg = json.loads(row["config_json"])
        name = row["name"]
        data_dir = self.m.data_dir
        out_dir = data_dir / cfg["out_dir"]
        work = out_dir.parent / (out_dir.name + "_work")
        self._restore_work(row["job_id"], work)
        cfg_dir = data_dir / "collection" / "weeks"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        cfg_path = cfg_dir / f"{name}.yaml"
        cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False))
        lease = (datetime.now(UTC) + timedelta(seconds=slice_seconds + LEASE_GRACE_SECONDS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.m.store.update_week_job(row["job_id"], status="collecting", updated_at=now_iso(), lease_until=lease)
        res = run_collection(cfg_path, data_dir, transport=self.transport, rpc_url_override=self.rpc_url, sleep=self.sleep, deadline=time.monotonic() + slice_seconds, store_bodies=False, budget_used=int(row["requests_used"]))
        status = res["status"]
        used = int(res.get("budget", {}).get("requests", row["requests_used"]))
        log = res.get("decision_log") or []
        note = log[-1] if log else status
        fields: dict[str, Any] = {"requests_used": used, "note": note[:400], "updated_at": now_iso(), "lease_until": None}
        if status == "pack_built":
            pack_archive = _targz(Path(res["pack_dir"]))
            view = self.m.upload_pack(pack_archive, name)
            fields.update(status="built", pack_id=view["pack_id"], note=f"pack built: {res['tape_events']} events in {res['pools']} pools; qualification {res['qualification']}")
            shutil.rmtree(work, ignore_errors=True)
            self.m.store.delete_week_job_files(row["job_id"])
            self.m.store.set_week_job_archive(row["job_id"], None)
        elif status == "in_progress_resumable":
            fields.update(status="collecting")
            self._save_work(row["job_id"], work)
        elif status == "provider_error_resumable":
            attempts = int(row["attempts"]) + 1
            fields.update(attempts=attempts, status="collecting" if attempts < MAX_PROVIDER_RETRIES else "failed", error=str(res.get("reason"))[:400])
            self._save_work(row["job_id"], work)
        else:  # budget_exhausted_resumable, blocked
            fields.update(status="failed", error=f"{status}: {res.get('reason')}"[:400])
            self._save_work(row["job_id"], work)
        self.m.store.update_week_job(row["job_id"], **fields)
        return self.view(self.m.store.week_job(row["job_id"]))

    # ------------------------------------------------------------------ work directory sync
    # The collector's working state (checkpoints, coverage ledger, one raw-log file per pool) lives in
    # the store between slices so any instance can continue. Only files whose content changed since the
    # last slice travel: a week's worth of logs is written once, not re-uploaded every three minutes.
    def _restore_work(self, job_id: str, work: Path) -> None:
        if (work / "checkpoints.json").exists():
            return  # same instance (or a warm filesystem) still has it
        index = self.m.store.week_job_file_index(job_id)
        if not index:
            legacy = self.m.store.week_job_archive(job_id)
            if legacy:
                _restore(legacy, work)
            return
        for rel in index:
            body = self.m.store.week_job_file(job_id, rel)
            if body is None:
                continue
            dest = work / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(gzip.decompress(body))

    def _save_work(self, job_id: str, work: Path) -> dict[str, int]:
        index = self.m.store.week_job_file_index(job_id)
        seen: set[str] = set()
        uploaded = 0
        for path in sorted(p for p in work.rglob("*") if p.is_file()):
            rel = path.relative_to(work).as_posix()
            if rel.startswith("receipts/") or rel.endswith(".tmp"):
                continue  # receipts are bodies-off in hosted mode and never needed to resume
            raw = path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            seen.add(rel)
            if index.get(rel) == digest:
                continue
            self.m.store.put_week_job_file(job_id, rel, digest, gzip.compress(raw, 6))
            uploaded += 1
        for rel in index:
            if rel not in seen:
                self.m.store.delete_week_job_file(job_id, rel)
        return {"uploaded": uploaded, "kept": len(seen) - uploaded}


def week_label(name: str, chain: str, protocol: str | None = None) -> str:
    """'base_week_03' -> 'Base week 3'; 'base_period_02_1h' -> 'Base period 2 (1h)'. The venue is named for
    anything but the original v2 pairs ('Base week 4, v4 pools'); the calendar never is."""
    parts = name.split("_")
    venue = PROTOCOLS.get(protocol or "", "")
    suffix = f", {venue}" if venue and protocol != "uniswap_v2" else ""
    if len(parts) >= 3 and parts[1] == "week" and parts[2].isdigit():
        return f"{chain.capitalize()} week {int(parts[2])}{suffix}"
    if len(parts) >= 4 and parts[1] == "period" and parts[2].isdigit():
        return f"{chain.capitalize()} period {int(parts[2])} ({parts[3]}){suffix}"
    return name.replace("_", " ") + suffix


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
