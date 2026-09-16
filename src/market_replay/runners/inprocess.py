"""Run a reference participant inside the server process (hosted mode has no subprocesses).

The participant still talks the public protocol: an httpx transport turns its HTTP calls into
direct calls of the run manager's command handler, so the agent code is unchanged and never
touches engine internals.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from .trusted import EXAMPLES_DIR, PY_EXAMPLES, REPO_ROOT


class InProcessTransport(httpx.BaseTransport):
    def __init__(self, handle_command, token: str) -> None:
        self._handle = handle_command
        self._token = token

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization", "")
        if auth != f"Bearer {self._token}":
            return httpx.Response(401, json={"code": "UNAUTHORIZED", "message": "bad token"})
        if request.url.path != "/agent/v1/commands":
            return httpx.Response(404, json={"code": "NOT_FOUND", "message": request.url.path})
        body = json.loads(request.content or b"{}")
        try:
            env = self._handle(self._token, body.get("request_id", "inproc"), body.get("tool", ""), body.get("arguments") or {}, body.get("session_id"))
        except Exception as e:  # ApiError from the manager (e.g. run finished)
            status = getattr(e, "status", 500)
            return httpx.Response(status, json={"code": getattr(e, "code", "error"), "message": str(e)})
        return httpx.Response(200, json=env.model_dump(mode="json"))


def load_example(name: str):
    if name not in PY_EXAMPLES:
        raise ValueError(f"unknown python example {name}")
    path = EXAMPLES_DIR / "python" / f"{name}.py"
    sdk = str(REPO_ROOT / "sdk" / "python")
    if sdk not in sys.path:
        sys.path.insert(0, sdk)
    spec = importlib.util.spec_from_file_location(f"mr_example_{name}", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_example_inprocess(handle_command, *, token: str, name: str, agent_seed: str | None) -> dict[str, Any]:
    """Execute a Python reference participant against the handler. Returns exit code, log and CPU seconds."""
    from market_replay_client import MarketReplayClient  # type: ignore[import-not-found]

    client = MarketReplayClient("http://inprocess", token, transport=InProcessTransport(handle_command, token))
    mod = load_example(name)
    buf = io.StringIO()
    prev_seed = os.environ.get("MARKET_REPLAY_AGENT_SEED")
    if agent_seed is not None:
        os.environ["MARKET_REPLAY_AGENT_SEED"] = str(agent_seed)
    cpu0 = time.process_time()
    wall0 = time.time()
    code = 0
    try:
        with contextlib.redirect_stdout(buf):
            code = int(mod.main(client) or 0)
    except SystemExit as e:
        code = int(e.code or 0)
    except Exception as e:  # the agent crashed: report it as an agent failure, never an environment failure
        buf.write(f"\nagent exception: {type(e).__name__}: {e}\n")
        code = 1
    finally:
        if prev_seed is None:
            os.environ.pop("MARKET_REPLAY_AGENT_SEED", None)
        else:
            os.environ["MARKET_REPLAY_AGENT_SEED"] = prev_seed
        client.close()
    return {"exit_code": code, "log": buf.getvalue(), "cpu_seconds": time.process_time() - cpu0, "wall_seconds": time.time() - wall0}


__all__ = ["InProcessTransport", "run_example_inprocess", "load_example", "Path"]
