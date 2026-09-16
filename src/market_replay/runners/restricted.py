"""Restricted local runner for operator-approved participants.

Concrete controls applied by this runner (each reported as enforced or unenforced):

* ``env_scrub``           enforced: the child receives only an allowlisted environment
                          (gateway URL, session token, agent seed, PATH, minimal locale). No
                          provider keys, no dates, no pack paths.
* ``no_pack_mount``       enforced: working directory is an empty temporary directory; the
                          pack directory path is never passed.
* ``no_secrets``          enforced: variables matching *KEY*, *SECRET*, *TOKEN* (other than the
                          session token), *PRIVATE*, *MNEMONIC* are dropped.
* ``network_namespace``   enforced only when ``unshare -n`` with a loopback-only namespace is
                          available and the gateway is bound to a loopback address reachable
                          from inside (Linux, requires user namespaces); otherwise reported as
                          unenforced.
* ``no_docker_socket``    enforced by not mounting anything; there is no container in this
                          runner, so this is an absence, not a sandbox guarantee.

This is a concrete restriction, not a claim that ordinary processes provide
hostile-code isolation. A hardened public execution service is out of scope for v1.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .trusted import REPO_ROOT, LaunchSpec, resolve_command

ALLOWED_ENV = {"PATH", "LANG", "LC_ALL", "HOME", "TMPDIR", "PYTHONUNBUFFERED", "PYTHONPATH", "NODE_OPTIONS", "PLAYWRIGHT_BROWSERS_PATH"}
SECRET_MARKERS = ("KEY", "SECRET", "TOKEN", "PRIVATE", "MNEMONIC", "PASSWORD", "CREDENTIAL")
DATE_MARKERS = ("DATE", "WEEK", "EPISODE", "PACK", "PERIOD")


@dataclass(slots=True)
class RestrictedControls:
    env_scrub: str = "enforced"
    no_pack_mount: str = "enforced"
    no_secrets: str = "enforced"
    network_namespace: str = "unenforced"
    no_docker_socket: str = "enforced_by_absence"
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, str | list[str]]:
        return {
            "env_scrub": self.env_scrub,
            "no_pack_mount": self.no_pack_mount,
            "no_secrets": self.no_secrets,
            "network_namespace": self.network_namespace,
            "no_docker_socket": self.no_docker_socket,
            "notes": self.notes,
        }


def scrub_env(base: dict[str, str], *, gateway_url: str, token: str, agent_seed: str | None) -> dict[str, str]:
    env: dict[str, str] = {}
    for k, v in base.items():
        ku = k.upper()
        if k not in ALLOWED_ENV:
            continue
        if any(m in ku for m in SECRET_MARKERS):
            continue
        if any(m in ku for m in DATE_MARKERS):
            continue
        env[k] = v
    env["MARKET_REPLAY_URL"] = gateway_url
    env["MARKET_REPLAY_TOKEN"] = token
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = str(REPO_ROOT / "sdk" / "python")
    env["HOME"] = env.get("TMPDIR", tempfile.gettempdir())
    if agent_seed is not None:
        env["MARKET_REPLAY_AGENT_SEED"] = agent_seed
    return env


def _unshare_available() -> bool:
    if shutil.which("unshare") is None:
        return False
    try:
        r = subprocess.run(["unshare", "-rn", "true"], capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def launch_restricted(
    spec: LaunchSpec,
    *,
    gateway_url: str,
    token: str,
    agent_seed: str | None,
    log_path: Path,
    try_network_namespace: bool = False,
) -> tuple[subprocess.Popen, RestrictedControls, Path]:
    controls = RestrictedControls()
    workdir = Path(tempfile.mkdtemp(prefix="mr-restricted-"))
    env = scrub_env(dict(os.environ), gateway_url=gateway_url, token=token, agent_seed=agent_seed)
    cmd = resolve_command(spec)
    if try_network_namespace and _unshare_available():
        # A fresh network namespace has only loopback; the gateway must be reachable there, which it is
        # not from the host loopback. This mode therefore only works with a proxy inside the namespace;
        # we report it honestly rather than pretending.
        controls.network_namespace = "unenforced"
        controls.notes.append("unshare available but gateway would be unreachable from an isolated namespace; not applied")
    else:
        controls.notes.append("network egress is not restricted by this runner; use the container configuration for network policy")
    controls.notes.append("working directory is an empty temporary directory; no pack path or date is passed")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("ab")
    proc = subprocess.Popen(cmd, env=env, cwd=str(workdir), stdout=log, stderr=subprocess.STDOUT)
    return proc, controls, workdir
