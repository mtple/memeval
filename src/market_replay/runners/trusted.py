"""Trusted external-client launcher.

Runs a participant as an ordinary subprocess and hands it the gateway URL and its
session credential through environment variables. The platform cannot prevent this
process from using the web or reading local files; reports say isolation is
unenforced.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
EXAMPLES_DIR = REPO_ROOT / "agents" / "examples"

PY_EXAMPLES = {"cash_only", "scheduled_basket", "random_actions", "model_client"}
TS_EXAMPLES = {"cash_only", "scheduled_basket", "random_actions"}


@dataclass(slots=True)
class LaunchSpec:
    name: str
    runtime: str  # python | typescript
    extra_args: list[str]


def resolve_command(spec: LaunchSpec) -> list[str]:
    if spec.runtime == "python":
        if spec.name not in PY_EXAMPLES:
            raise ValueError(f"unknown python example {spec.name}")
        return [sys.executable, str(EXAMPLES_DIR / "python" / f"{spec.name}.py"), *spec.extra_args]
    if spec.runtime == "typescript":
        if spec.name not in TS_EXAMPLES:
            raise ValueError(f"unknown typescript example {spec.name}")
        return ["node", "--experimental-strip-types", "--no-warnings", str(EXAMPLES_DIR / "typescript" / f"{spec.name}.ts"), *spec.extra_args]
    raise ValueError(f"unknown runtime {spec.runtime}")


def launch(spec: LaunchSpec, *, gateway_url: str, token: str, agent_seed: str | None, log_path: Path, cwd: Path | None = None) -> subprocess.Popen:
    env = dict(os.environ)
    env.update({"MARKET_REPLAY_URL": gateway_url, "MARKET_REPLAY_TOKEN": token, "PYTHONUNBUFFERED": "1"})
    if agent_seed is not None:
        env["MARKET_REPLAY_AGENT_SEED"] = agent_seed
    env["PYTHONPATH"] = str(REPO_ROOT / "sdk" / "python") + os.pathsep + env.get("PYTHONPATH", "")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("ab")
    return subprocess.Popen(resolve_command(spec), env=env, stdout=log, stderr=subprocess.STDOUT, cwd=str(cwd or REPO_ROOT))
