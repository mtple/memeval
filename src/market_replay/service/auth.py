"""Bearer credentials with two privilege levels.

* Admin token: control plane (packs, agents, runs, comparisons, exports, data health).
* Session token (agt_): bound to exactly one run/session; can only call the agent plane and
  only for its own session.
* Identity token (agn_): handed out when an agent joins; it can create that agent's runs (play)
  and list its episodes, nothing else. Neither kind can reach the control plane.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets


def new_admin_token() -> str:
    return "adm_" + secrets.token_urlsafe(24)


def new_agent_token() -> str:
    return "agt_" + secrets.token_urlsafe(24)


def new_identity_token() -> str:
    """The credential an agent keeps after joining: it creates runs for that agent and nothing else."""
    return "agn_" + secrets.token_urlsafe(24)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def constant_time_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def resolve_admin_token(explicit: str | None = None) -> str:
    """Admin token from argument, then env, else a fresh random token (printed once by the CLI)."""
    if explicit:
        return explicit
    env = os.environ.get("MARKET_REPLAY_ADMIN_TOKEN")
    if env:
        return env
    return new_admin_token()
