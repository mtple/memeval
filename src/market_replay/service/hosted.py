"""Hosted entrypoint (Vercel Functions or any single-process host) configured from the environment.

    DATABASE_URL / POSTGRES_URL            Postgres store (Neon on Vercel); SQLite under the data dir otherwise
    MARKET_REPLAY_ADMIN_TOKEN              control-plane token (required in production; generated + logged otherwise)
    MARKET_REPLAY_PUBLIC_URL               public base URL handed to agents (defaults to Vercel's production URL)
    MARKET_REPLAY_DATA_DIR                 scratch dir for regenerated packs (ephemeral on serverless; /tmp default)
    MARKET_REPLAY_BOOTSTRAP                "all" | "dev" | "none": fixture packs registered on first start (hosted default: all)
    MARKET_REPLAY_MAX_RUNS_PER_DAY, MARKET_REPLAY_MAX_CPU_SECONDS_PER_MONTH   cost caps (raise when you buy usage)
    MARKET_REPLAY_CORS_ORIGINS             comma-separated browser origins (same-origin needs none)
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from fastapi import FastAPI

from .app import cors_origins_from_env, create_app
from .auth import new_admin_token
from .runs import RunManager


def build_hosted_app() -> tuple[FastAPI, RunManager]:
    db_url = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL") or None
    data_dir = Path(os.environ.get("MARKET_REPLAY_DATA_DIR") or (Path(tempfile.gettempdir()) / "market-replay"))
    admin = os.environ.get("MARKET_REPLAY_ADMIN_TOKEN")
    if not admin:
        admin = new_admin_token()
        print(f"[market-replay] MARKET_REPLAY_ADMIN_TOKEN not set; generated one for this instance: {admin}", file=sys.stderr)
    hosted = bool(os.environ.get("VERCEL") or os.environ.get("MARKET_REPLAY_HOSTED"))
    mgr = RunManager(data_dir=data_dir, store_url=db_url, dev_mode=os.environ.get("MARKET_REPLAY_DEV_MODE", "1") != "0", hosted=hosted or db_url is not None, runtimes_available=("python",))
    public = os.environ.get("MARKET_REPLAY_PUBLIC_URL")
    if not public and os.environ.get("VERCEL_PROJECT_PRODUCTION_URL"):
        public = "https://" + os.environ["VERCEL_PROJECT_PRODUCTION_URL"]
    if public:
        mgr.gateway_url = public.rstrip("/")
    boot = os.environ.get("MARKET_REPLAY_BOOTSTRAP") or ("all" if mgr.hosted else "")
    if boot and boot != "none":
        names = ["gen_dev_short"] if boot == "dev" else ["gen_dev_short", "gen_week_trending", "gen_week_reversal", "gen_week_sparse_missing", "gen_week_liquidity_shift"]
        for n in names:
            try:
                mgr.ensure_fixture_pack(n)
            except Exception as e:  # pragma: no cover - startup must not fail on one pack
                print(f"[market-replay] bootstrap of {n} failed: {e}", file=sys.stderr)
    app = create_app(mgr, admin, cors_origins=cors_origins_from_env())
    return app, mgr
