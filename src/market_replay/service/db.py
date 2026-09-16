"""SQLite (WAL) control-plane store. Raw traces live in append-only JSONL files, not here."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS packs (
  pack_id TEXT PRIMARY KEY,
  episode_id TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  path TEXT NOT NULL,
  origin TEXT NOT NULL,
  chain TEXT NOT NULL,
  scope_label TEXT NOT NULL,
  use_status TEXT NOT NULL,
  duration_ms INTEGER NOT NULL,
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
  clock_ms INTEGER NOT NULL DEFAULT 0,
  report_json TEXT,
  exposed INTEGER NOT NULL DEFAULT 0
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
"""


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.executescript(SCHEMA)

    # ------------------------------------------------------------------ generic
    def execute(self, sql: str, params: tuple | dict = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)

    def query(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    def one(self, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # ------------------------------------------------------------------ packs
    def upsert_pack(self, row: dict[str, Any]) -> None:
        self.execute(
            """INSERT INTO packs (pack_id, episode_id, name, path, origin, chain, scope_label, use_status, duration_ms, is_full_week,
               start_utc, end_utc, execution_model, imported_at, summary_json)
               VALUES (:pack_id, :episode_id, :name, :path, :origin, :chain, :scope_label, :use_status, :duration_ms, :is_full_week,
               :start_utc, :end_utc, :execution_model, :imported_at, :summary_json)
               ON CONFLICT(pack_id) DO UPDATE SET path=excluded.path, use_status=excluded.use_status, summary_json=excluded.summary_json""",
            row,
        )

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
        with self._lock:
            self._conn.execute(
                "INSERT INTO attempts (pack_id, agent_id, count) VALUES (?, ?, 1) ON CONFLICT(pack_id, agent_id) DO UPDATE SET count=count+1",
                (pack_id, agent_id),
            )
            row = self.one("SELECT count FROM attempts WHERE pack_id=? AND agent_id=?", (pack_id, agent_id))
            return int(row["count"]) if row else 1

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

    def close(self) -> None:
        with self._lock:
            self._conn.close()
