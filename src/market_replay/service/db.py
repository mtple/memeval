"""Control-plane store with two backends: SQLite (local, WAL) and Postgres (hosted, e.g. Neon on Vercel).

Both expose the same methods. SQL is written once with ``?`` placeholders and translated for
Postgres. Besides metadata rows the store holds, per run: the append-only command trace (the
durable truth from which a session can be rebuilt by deterministic replay), JSON documents
(run manifest, report, agent log) and daily usage counters for cost caps.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class RunBusy(RuntimeError):
    """No command ran because another request still owns the run lock."""

    status = 503
    code = "RUN_BUSY"
    message = "Another request is using this run. Wait briefly and retry the same command; do not reset the run."

    def __init__(self) -> None:
        super().__init__(self.message)


# Columns added after a table first shipped; each statement must be safe to re-run (SQLite raises on
# a duplicate column and the error is swallowed; Postgres gets IF NOT EXISTS).
MIGRATIONS = ["ALTER TABLE agents ADD COLUMN token_hash TEXT", "ALTER TABLE packs ADD COLUMN visibility TEXT NOT NULL DEFAULT 'public'"]

# Retain the retired bundle tables for existing data and the legacy run-privacy guard.
# No application workflow creates or updates these records.
SCHEMA = """
CREATE TABLE IF NOT EXISTS assessment_bundles (
  bundle_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, manifest_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assessments (
  assessment_id TEXT PRIMARY KEY, bundle_id TEXT NOT NULL, agent_id TEXT NOT NULL,
  created_at TEXT NOT NULL, state TEXT NOT NULL, commitment_json TEXT NOT NULL,
  UNIQUE(bundle_id, agent_id)
);
CREATE TABLE IF NOT EXISTS assessment_episodes (
  assessment_id TEXT NOT NULL, slot INTEGER NOT NULL, pack_id TEXT NOT NULL,
  run_id TEXT UNIQUE NOT NULL, PRIMARY KEY(assessment_id, slot)
);
CREATE TABLE IF NOT EXISTS packs (
  pack_id TEXT PRIMARY KEY,
  episode_id TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  path TEXT NOT NULL,
  origin TEXT NOT NULL,
  chain TEXT NOT NULL,
  scope_label TEXT NOT NULL,
  use_status TEXT NOT NULL,
  duration_ms BIGINT NOT NULL,
  is_full_week INTEGER NOT NULL,
  start_utc TEXT NOT NULL,
  end_utc TEXT NOT NULL,
  execution_model TEXT NOT NULL,
  imported_at TEXT NOT NULL,
  summary_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agents (
  agent_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  version TEXT NOT NULL,
  runtime TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  capabilities_json TEXT NOT NULL,
  config_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  token_hash TEXT,
  UNIQUE(name, version)
);
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL,
  pack_id TEXT NOT NULL,
  suite_id TEXT,
  suite_run_id TEXT,
  mode TEXT NOT NULL,
  isolation TEXT NOT NULL,
  state TEXT NOT NULL,
  bankroll_raw TEXT NOT NULL,
  mask_seed TEXT NOT NULL,
  engine_seed TEXT NOT NULL,
  profile_hash TEXT NOT NULL,
  token_hash TEXT,
  launch_json TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  error TEXT,
  clock_ms BIGINT NOT NULL DEFAULT 0,
  report_json TEXT,
  exposed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS traces (
  run_id TEXT NOT NULL,
  idx INTEGER NOT NULL,
  record TEXT NOT NULL,
  PRIMARY KEY (run_id, idx)
);
CREATE TABLE IF NOT EXISTS docs (
  run_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  doc TEXT NOT NULL,
  PRIMARY KEY (run_id, kind)
);
CREATE TABLE IF NOT EXISTS usage (
  day TEXT PRIMARY KEY,
  runs INTEGER NOT NULL DEFAULT 0,
  cpu_seconds DOUBLE PRECISION NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS comparisons (
  comparison_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  request_json TEXT NOT NULL,
  result_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS studies (
  study_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  record_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS suite_runs (
  suite_run_id TEXT PRIMARY KEY,
  suite_id TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  run_ids_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
  pack_id TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  count INTEGER NOT NULL,
  PRIMARY KEY (pack_id, agent_id)
);
CREATE TABLE IF NOT EXISTS rate_events (
  kind TEXT NOT NULL,
  key TEXT NOT NULL,
  ts DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS rate_events_kind_key_ts ON rate_events (kind, key, ts);
"""

TABLES = ("packs", "agents", "runs", "traces", "docs", "usage", "comparisons", "studies", "suite_runs", "attempts", "rate_events", "assessment_bundles", "assessments", "assessment_episodes")


def today_key() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


class BaseStore:
    backend = "base"

    # ------------------------------------------------------------------ primitives (implemented per backend)
    def execute(self, sql: str, params: tuple | dict = ()) -> None:
        raise NotImplementedError

    def query(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        raise NotImplementedError

    @contextmanager
    def run_lock(self, run_id: str, timeout: float = 5.0) -> Iterator[None]:
        raise NotImplementedError

    def try_run_lock(self, run_id: str) -> Iterator[bool]:
        """Like run_lock but never waits: yields False when another holder has it."""
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def reset_for_tests(self) -> None:
        for t in TABLES:
            self.execute(f"DELETE FROM {t}")

    def one(self, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # ------------------------------------------------------------------ packs
    def upsert_pack(self, row: dict[str, Any]) -> None:
        self.execute(
            """INSERT INTO packs (pack_id, episode_id, name, path, origin, chain, scope_label, use_status, duration_ms, is_full_week,
               start_utc, end_utc, execution_model, imported_at, summary_json, visibility)
               VALUES (:pack_id, :episode_id, :name, :path, :origin, :chain, :scope_label, :use_status, :duration_ms, :is_full_week,
               :start_utc, :end_utc, :execution_model, :imported_at, :summary_json, :visibility)
               ON CONFLICT(pack_id) DO UPDATE SET path=excluded.path, name=excluded.name, use_status=excluded.use_status, summary_json=excluded.summary_json""",
            {"visibility": "public", **row},
        )

    def delete_pack(self, pack_id: str) -> None:
        """Forget a pack and its archive (runs that referenced it keep their reports)."""
        self.execute("DELETE FROM packs WHERE pack_id=?", (pack_id,))

    def packs(self) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM packs ORDER BY imported_at")

    def pack(self, pack_id: str) -> dict[str, Any] | None:
        return self.one("SELECT * FROM packs WHERE pack_id=? OR episode_id=? OR name=?", (pack_id, pack_id, pack_id))

    # ------------------------------------------------------------------ agents
    def insert_agent(self, row: dict[str, Any]) -> None:
        self.execute(
            """INSERT INTO agents (agent_id, name, version, runtime, fingerprint, capabilities_json, config_json, created_at)
               VALUES (:agent_id, :name, :version, :runtime, :fingerprint, :capabilities_json, :config_json, :created_at)""",
            row,
        )

    def agents(self) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM agents ORDER BY created_at")

    def agent(self, agent_id: str) -> dict[str, Any] | None:
        return self.one("SELECT * FROM agents WHERE agent_id=?", (agent_id,))

    def agent_by_name_version(self, name: str, version: str) -> dict[str, Any] | None:
        return self.one("SELECT * FROM agents WHERE name=? AND version=?", (name, version))

    def agent_by_token_hash(self, h: str) -> dict[str, Any] | None:
        return self.one("SELECT * FROM agents WHERE token_hash=?", (h,))

    def set_agent_token_hash(self, agent_id: str, h: str) -> None:
        self.execute("UPDATE agents SET token_hash=? WHERE agent_id=?", (h, agent_id))

    def set_agent_name(self, agent_id: str, name: str) -> None:
        self.execute("UPDATE agents SET name=? WHERE agent_id=?", (name, agent_id))

    # ------------------------------------------------------------------ runs
    def insert_run(self, row: dict[str, Any]) -> None:
        cols = ", ".join(row)
        vals = ", ".join(f":{k}" for k in row)
        self.execute(f"INSERT INTO runs ({cols}) VALUES ({vals})", row)

    def update_run(self, run_id: str, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k}=:{k}" for k in fields)
        fields["run_id"] = run_id
        self.execute(f"UPDATE runs SET {sets} WHERE run_id=:run_id", fields)

    def run(self, run_id: str) -> dict[str, Any] | None:
        return self.one("SELECT * FROM runs WHERE run_id=?", (run_id,))

    def runs(self, **where: Any) -> list[dict[str, Any]]:
        if not where:
            return self.query("SELECT * FROM runs ORDER BY created_at")
        cond = " AND ".join(f"{k}=:{k}" for k in where)
        return self.query(f"SELECT * FROM runs WHERE {cond} ORDER BY created_at", where)

    def run_by_token_hash(self, token_hash: str) -> dict[str, Any] | None:
        return self.one("SELECT * FROM runs WHERE token_hash=?", (token_hash,))

    def bump_attempt(self, pack_id: str, agent_id: str) -> int:
        self.execute(
            "INSERT INTO attempts (pack_id, agent_id, count) VALUES (?, ?, 1) ON CONFLICT(pack_id, agent_id) DO UPDATE SET count=attempts.count+1",
            (pack_id, agent_id),
        )
        row = self.one("SELECT count FROM attempts WHERE pack_id=? AND agent_id=?", (pack_id, agent_id))
        return int(row["count"]) if row else 1

    # ------------------------------------------------------------------ traces & docs
    def append_trace(self, run_id: str, records: list[dict[str, Any]]) -> None:
        for r in records:
            self.execute("INSERT INTO traces (run_id, idx, record) VALUES (?, ?, ?) ON CONFLICT(run_id, idx) DO NOTHING", (run_id, int(r["index"]), json.dumps(r, sort_keys=True)))

    def trace(self, run_id: str, after: int = -1) -> list[dict[str, Any]]:
        rows = self.query("SELECT record FROM traces WHERE run_id=? AND idx>? ORDER BY idx", (run_id, after))
        return [json.loads(r["record"]) for r in rows]

    def trace_len(self, run_id: str) -> int:
        row = self.one("SELECT COUNT(*) AS n FROM traces WHERE run_id=?", (run_id,))
        return int(row["n"]) if row else 0

    def put_doc(self, run_id: str, kind: str, doc: Any) -> None:
        self.execute("INSERT INTO docs (run_id, kind, doc) VALUES (?, ?, ?) ON CONFLICT(run_id, kind) DO UPDATE SET doc=excluded.doc", (run_id, kind, json.dumps(doc, sort_keys=True, default=str)))

    def get_doc(self, run_id: str, kind: str) -> Any:
        row = self.one("SELECT doc FROM docs WHERE run_id=? AND kind=?", (run_id, kind))
        return json.loads(row["doc"]) if row else None

    # ------------------------------------------------------------------ usage caps
    def add_usage(self, *, runs: int = 0, cpu_seconds: float = 0.0) -> None:
        self.execute(
            "INSERT INTO usage (day, runs, cpu_seconds) VALUES (?, ?, ?) ON CONFLICT(day) DO UPDATE SET runs=usage.runs+excluded.runs, cpu_seconds=usage.cpu_seconds+excluded.cpu_seconds",
            (today_key(), runs, float(cpu_seconds)),
        )

    def usage_today(self) -> dict[str, Any]:
        row = self.one("SELECT runs, cpu_seconds FROM usage WHERE day=?", (today_key(),))
        return {"runs": int(row["runs"]), "cpu_seconds": float(row["cpu_seconds"])} if row else {"runs": 0, "cpu_seconds": 0.0}

    def usage_month(self) -> dict[str, Any]:
        prefix = today_key()[:7] + "-%"
        row = self.one("SELECT COALESCE(SUM(runs),0) AS runs, COALESCE(SUM(cpu_seconds),0) AS cpu FROM usage WHERE day LIKE ?", (prefix,))
        return {"runs": int(row["runs"]), "cpu_seconds": float(row["cpu"])} if row else {"runs": 0, "cpu_seconds": 0.0}

    # ------------------------------------------------------------------ per-key rate limiting (shared across instances)
    def count_rate_events(self, kind: str, key: str, since_ts: float) -> int:
        row = self.one("SELECT COUNT(*) AS n FROM rate_events WHERE kind=? AND key=? AND ts>=?", (kind, key, float(since_ts)))
        return int(row["n"]) if row else 0

    def rate_event_kinds(self, prefix: str, key: str, since_ts: float) -> list[str]:
        """Distinct event kinds starting with ``prefix`` recorded for ``key`` since ``since_ts``."""
        rows = self.query("SELECT DISTINCT kind FROM rate_events WHERE key=? AND ts>=? AND kind LIKE ?", (key, float(since_ts), prefix + "%"))
        return [str(r["kind"]) for r in rows]

    def add_rate_event(self, kind: str, key: str, ts: float) -> None:
        self.execute("INSERT INTO rate_events (kind, key, ts) VALUES (?, ?, ?)", (kind, key, float(ts)))
        # Keep the table small: anything older than a day is irrelevant to every window we use.
        self.execute("DELETE FROM rate_events WHERE ts < ?", (float(ts) - 86400.0,))

    # ------------------------------------------------------------------ comparisons / studies / suite runs
    def insert_comparison(self, comparison_id: str, created_at: str, request: dict, result: dict) -> None:
        self.execute(
            "INSERT INTO comparisons (comparison_id, created_at, request_json, result_json) VALUES (?, ?, ?, ?)",
            (comparison_id, created_at, json.dumps(request, sort_keys=True), json.dumps(result, sort_keys=True)),
        )

    def comparisons(self) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM comparisons ORDER BY created_at")

    def comparison(self, comparison_id: str) -> dict[str, Any] | None:
        return self.one("SELECT * FROM comparisons WHERE comparison_id=?", (comparison_id,))

    def upsert_study(self, study_id: str, created_at: str, record: dict) -> None:
        self.execute(
            "INSERT INTO studies (study_id, created_at, record_json) VALUES (?, ?, ?) ON CONFLICT(study_id) DO UPDATE SET record_json=excluded.record_json",
            (study_id, created_at, json.dumps(record, sort_keys=True)),
        )

    def studies(self) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM studies ORDER BY created_at")

    def study(self, study_id: str) -> dict[str, Any] | None:
        return self.one("SELECT * FROM studies WHERE study_id=?", (study_id,))

    def insert_suite_run(self, suite_run_id: str, suite_id: str, agent_id: str, created_at: str, run_ids: list[str]) -> None:
        self.execute(
            "INSERT INTO suite_runs (suite_run_id, suite_id, agent_id, created_at, run_ids_json) VALUES (?, ?, ?, ?, ?)",
            (suite_run_id, suite_id, agent_id, created_at, json.dumps(run_ids)),
        )

    def suite_runs(self) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM suite_runs ORDER BY created_at")


def _named_to_positional(sql: str, params: dict[str, Any]) -> tuple[str, tuple]:
    """Translate ``:name`` placeholders into positional ``?`` in order of appearance."""
    import re

    names: list[str] = []

    def repl(m: re.Match[str]) -> str:
        names.append(m.group(1))
        return "?"

    out = re.sub(r"(?<![:\w]):(\w+)", repl, sql)
    return out, tuple(params[n] for n in names)


class SqliteStore(BaseStore):
    backend = "sqlite"

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._lock = threading.RLock()
        self._run_locks: dict[str, threading.RLock] = {}
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        with self._lock:
            self._conn.executescript(SCHEMA.replace("DOUBLE PRECISION", "REAL").replace("BIGINT", "INTEGER"))
            for stmt in MIGRATIONS:
                try:
                    self._conn.execute(stmt)
                except sqlite3.OperationalError:
                    pass  # column already there

    def execute(self, sql: str, params: tuple | dict = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)

    def query(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    @contextmanager
    def run_lock(self, run_id: str, timeout: float = 5.0) -> Iterator[None]:
        """Serialize commands of one run across threads and store instances sharing the same file (flock)."""
        import fcntl
        import hashlib

        with self._lock:
            lk = self._run_locks.setdefault(run_id, threading.RLock())
        lock_dir = self._path.parent / "locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_file = lock_dir / (hashlib.sha256(run_id.encode()).hexdigest()[:24] + ".lock")
        deadline = time.monotonic() + timeout
        if not lk.acquire(timeout=max(0.0, timeout)):
            raise RunBusy()
        try:
            fd = open(lock_file, "a+")
            try:
                while True:
                    try:
                        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise RunBusy() from None
                        time.sleep(min(0.01, remaining))
                yield
            finally:
                fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
                fd.close()
        finally:
            lk.release()

    @contextmanager
    def try_run_lock(self, run_id: str) -> Iterator[bool]:
        import fcntl
        import hashlib

        with self._lock:
            lk = self._run_locks.setdefault(run_id, threading.RLock())
        if not lk.acquire(blocking=False):
            yield False
            return
        lock_dir = self._path.parent / "locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_file = lock_dir / (hashlib.sha256(run_id.encode()).hexdigest()[:24] + ".lock")
        fd = open(lock_file, "a+")
        try:
            try:
                fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        finally:
            fd.close()
            lk.release()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class PgStore(BaseStore):
    backend = "postgres"

    def __init__(self, url: str) -> None:
        import psycopg

        self._psycopg = psycopg
        self.url = url
        self._lock = threading.RLock()
        self._conn = self._connect()
        with self._lock:
            for stmt in SCHEMA.split(";"):
                if stmt.strip():
                    self._conn.execute(stmt)
            for stmt in MIGRATIONS:
                self._conn.execute(stmt.replace("ADD COLUMN", "ADD COLUMN IF NOT EXISTS"))

    def _connect(self):
        # prepare_threshold=None: no server-side prepared statements, so a transaction-mode pooler
        # (Supabase Supavisor, Neon, PgBouncer) can hand each statement to any backend.
        return self._psycopg.connect(self.url, autocommit=True, connect_timeout=15, prepare_threshold=None)

    def _live(self):
        """The connection, reopened if the server dropped it (Neon suspends idle compute; warm serverless
        instances outlive that). Callers hold the lock."""
        if self._conn.closed or self._conn.broken:
            self._conn = self._connect()
        return self._conn

    def _with_retry(self, fn):
        """Run fn(conn) once; on a dropped connection reconnect and run it once more."""
        with self._lock:
            try:
                return fn(self._live())
            except (self._psycopg.OperationalError, self._psycopg.InterfaceError):
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = self._connect()
                return fn(self._conn)

    @staticmethod
    def _translate(sql: str, params: tuple | dict) -> tuple[str, tuple]:
        if isinstance(params, dict):
            sql, params = _named_to_positional(sql, params)
        return sql.replace("?", "%s"), tuple(params)

    def execute(self, sql: str, params: tuple | dict = ()) -> None:
        q, p = self._translate(sql, params)
        self._with_retry(lambda conn: conn.execute(q, p))

    def query(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        from psycopg.rows import dict_row

        q, p = self._translate(sql, params)

        def go(conn):
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(q, p)
                return [dict(r) for r in cur.fetchall()]

        return self._with_retry(go)

    @contextmanager
    def run_lock(self, run_id: str, timeout: float = 5.0) -> Iterator[None]:
        """Bound the wait for ownership; never expire a lock while its owner is executing."""
        conn = self._lock_connection()
        try:
            conn.execute("SELECT set_config('lock_timeout', %s, true)", (f"{max(1, int(timeout * 1000))}ms",))
            try:
                conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (run_id,))
            except (self._psycopg.errors.LockNotAvailable, self._psycopg.errors.QueryCanceled):
                raise RunBusy() from None
            yield
        finally:
            try:
                conn.rollback()
            finally:
                conn.close()

    def _lock_connection(self):
        return self._psycopg.connect(self.url, autocommit=False, prepare_threshold=None,
                                     connect_timeout=5, options="-c statement_timeout=10000")

    @contextmanager
    def try_run_lock(self, run_id: str) -> Iterator[bool]:
        conn = self._lock_connection()
        try:
            got = conn.execute("SELECT pg_try_advisory_xact_lock(hashtext(%s))", (run_id,)).fetchone()[0]
            yield bool(got)
        finally:
            try:
                conn.commit()
            finally:
                conn.close()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def open_store(url_or_path: str | Path) -> BaseStore:
    s = str(url_or_path)
    if s.startswith(("postgres://", "postgresql://")):
        return PgStore(s)
    return SqliteStore(Path(s))


# Backwards-compatible name used by earlier code.
Store = SqliteStore
