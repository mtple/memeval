"""Hosted entrypoint (Vercel Functions or any single-process host) configured from the environment.

    DATABASE_URL / POSTGRES_URL            Postgres store (Neon on Vercel); SQLite under the data dir otherwise
    MARKET_REPLAY_ADMIN_TOKEN              control-plane token (required in production; generated + logged otherwise)
    MARKET_REPLAY_PUBLIC_URL               public base URL handed to agents (defaults to Vercel's production URL)
    MARKET_REPLAY_DATA_DIR                 scratch dir for regenerated packs (ephemeral on serverless; /tmp default)
    MARKET_REPLAY_BOOTSTRAP                "all" | "dev" | "none": fixture packs registered on first start (hosted default: all)
    MARKET_REPLAY_MAX_RUNS_PER_DAY, MARKET_REPLAY_MAX_CPU_SECONDS_PER_MONTH   cost caps (raise when you buy usage)
    MARKET_REPLAY_CORS_ORIGINS             comma-separated browser origins (same-origin needs none)
    BASE_RPC_URL / RPC_URL                 read-only EVM RPC endpoint; when set, anyone can request a real past week
    MARKET_REPLAY_MAX_WEEKS_PER_DAY, MARKET_REPLAY_WEEK_MAX_REQUESTS, MARKET_REPLAY_WEEK_MAX_PAIRS,
    MARKET_REPLAY_WEEK_LOG_CHUNK, MARKET_REPLAY_WEEK_SLICE_SECONDS   collection caps and slice length
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
from pathlib import Path

from fastapi import FastAPI, Request
from starlette.concurrency import run_in_threadpool

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
    rpc = os.environ.get("BASE_RPC_URL") or os.environ.get("RPC_URL") or None
    from .weeks import WeekJobs

    mgr.weeks = WeekJobs(
        mgr,
        rpc_url=rpc,
        slice_seconds=float(os.environ.get("MARKET_REPLAY_WEEK_SLICE_SECONDS", "240")),
        max_requests=int(os.environ.get("MARKET_REPLAY_WEEK_MAX_REQUESTS", "20000")),
        log_chunk_blocks=int(os.environ.get("MARKET_REPLAY_WEEK_LOG_CHUNK", "10000")),
        max_pairs=int(os.environ.get("MARKET_REPLAY_WEEK_MAX_PAIRS", "16")),
        max_jobs_per_day=int(os.environ.get("MARKET_REPLAY_MAX_WEEKS_PER_DAY", "3")),
    )
    boot = os.environ.get("MARKET_REPLAY_BOOTSTRAP") or ("all" if mgr.hosted else "")
    names = [] if boot in ("", "none") else (["gen_dev_short"] if boot == "dev" else list(FIXTURE_NAMES))
    app = create_app(mgr, admin, cors_origins=cors_origins_from_env())
    if names:
        install_lazy_bootstrap(app, mgr, names)
    return app, mgr


FIXTURE_NAMES = ("gen_dev_short", "gen_week_trending", "gen_week_reversal", "gen_week_sparse_missing", "gen_week_liquidity_shift")


def install_lazy_bootstrap(app: FastAPI, mgr: RunManager, names: list[str]) -> None:
    """Register fixture packs on the first real request, once per process, never at import time.

    Serverless runtimes import the entrypoint during instance initialization, which has a short
    time limit; generating the fixtures the first time takes ~20 s. After that first registration
    the check is a handful of store reads per cold start (registered packs are not regenerated
    until a run needs them). Health checks never trigger it.
    """
    lock = threading.Lock()
    state = {"done": False}

    def bootstrap() -> None:
        with lock:
            if state["done"]:
                return
            for n in names:
                try:
                    mgr.ensure_fixture_pack(n)
                except Exception as e:  # pragma: no cover - one broken fixture must not take the service down
                    print(f"[market-replay] bootstrap of {n} failed: {e}", file=sys.stderr)
            state["done"] = True

    @app.middleware("http")
    async def _bootstrap_once(request: Request, call_next):
        if not state["done"] and not request.url.path.endswith("/health"):
            await run_in_threadpool(bootstrap)
        return await call_next(request)

    app.state.bootstrap = bootstrap
