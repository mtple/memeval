"""MCP facade over the agent plane.

Every MCP tool forwards to ``POST /agent/v1/commands`` on the running service using the
session credential in the environment, so MCP, HTTP and the SDKs share one handler.
Tool names use underscores (``market_trades``) because MCP tool names should match
``^[a-zA-Z0-9_-]+$``; the mapping to canonical names is 1:1.

Run: ``market-replay mcp`` with MARKET_REPLAY_URL and MARKET_REPLAY_TOKEN set.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from mcp.server.fastmcp import Context, FastMCP

from ..engine.session import TOOLS

MCP_NAME_MAP = {name.replace(".", "_"): name for name in TOOLS}


class Forwarder:
    def __init__(self, base_url: str | None = None, token: str | None = None, transport: httpx.BaseTransport | None = None) -> None:
        self.base_url = (base_url or os.environ.get("MARKET_REPLAY_URL", "")).rstrip("/")
        self.token = token or os.environ.get("MARKET_REPLAY_TOKEN", "")
        self._client = httpx.Client(base_url=self.base_url, headers={"Authorization": f"Bearer {self.token}"}, timeout=120, transport=transport)
        self._n = 0

    def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self.base_url or not self.token:
            return {"status": "error", "error": {"code": "UNAUTHORIZED", "message": "MARKET_REPLAY_URL / MARKET_REPLAY_TOKEN not configured"}}
        self._n += 1
        r = self._client.post("/agent/v1/commands", json={"request_id": f"mcp_{self._n}", "tool": tool, "arguments": arguments})
        if r.status_code >= 400:
            return {"status": "error", "error": {"code": f"HTTP_{r.status_code}", "message": r.text[:300]}}
        return r.json()


def build_mcp(forwarder: Forwarder | None = None) -> FastMCP:
    fwd = forwarder or Forwarder()
    mcp = FastMCP("market-replay", instructions="Blinded market simulation tools. All quantities are decimal strings in raw units; times are relative milliseconds.")

    def register(mcp_name: str, canonical: str, description: str) -> None:
        def tool(arguments: dict[str, Any] | None = None) -> dict[str, Any]:
            return fwd.call(canonical, arguments or {})

        tool.__name__ = mcp_name
        tool.__doc__ = f"{description} Canonical tool: {canonical}. Pass tool arguments as the `arguments` object."
        mcp.tool(name=mcp_name, description=tool.__doc__, structured_output=True)(tool)

    for mcp_name, canonical in MCP_NAME_MAP.items():
        register(mcp_name, canonical, TOOLS[canonical])
    return mcp


def main() -> None:
    build_mcp().run(transport="stdio")


if __name__ == "__main__":
    main()


# ---------------------------------------------------------------------- remote (streamable HTTP) facade
def build_remote_mcp(manager, public_runs=lambda: True, client_ip=None) -> FastMCP:
    """MCP over streamable HTTP for agents that speak MCP (OpenClaw, Hermes, Claude and similar).

    Stateless and JSON-response so it works behind serverless functions. `enroll` needs no
    credential and returns one session token per episode; every other tool needs that token, either
    as the `Authorization: Bearer` header or as the `token` argument (for clients that cannot set
    headers). Same handler as the HTTP command endpoint.
    """
    from mcp.server.transport_security import TransportSecuritySettings

    mcp = FastMCP(
        "market-replay",
        instructions=(
            "Market Replay: test a trading agent on replayed market episodes, no real money. "
            "Call `enroll` with your agent name to get a session token per episode, then pass that token to every other tool "
            "(`token` argument or Authorization header). Tool arguments go in the `arguments` object. Quantities are decimal "
            "strings in raw units; times are relative milliseconds. Finish each run with session_finish. Full guide: <server>/skill.md."
        ),
        stateless_http=True,
        json_response=True,
        streamable_http_path="/mcp",
        # The endpoint is public by design (agents connect from anywhere); every session tool is gated by a per-run token,
        # so Host-header rebinding protection would only block legitimate hosts.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    def request_of(ctx: Context):
        return ctx.request_context.request if ctx.request_context else None

    def resolve_token(ctx: Context, token: str | None) -> str | None:
        req = request_of(ctx)
        auth = req.headers.get("authorization", "") if req is not None else ""
        if auth.startswith("Bearer agt_"):
            return auth[7:]
        if token and token.startswith("agt_"):
            return token
        return None

    def register(mcp_name: str, canonical: str, description: str) -> None:
        def tool(ctx: Context, arguments: dict[str, Any] | None = None, token: str | None = None) -> dict[str, Any]:
            tok = resolve_token(ctx, token)
            if tok is None:
                return {"status": "error", "error": {"code": "UNAUTHORIZED", "message": "session token required: call `enroll` first, then pass its token as the `token` argument or Authorization: Bearer header"}}
            try:
                env = manager.handle_command(tok, f"mcp_{canonical}", canonical, arguments or {}, None)
            except Exception as e:
                return {"status": "error", "error": {"code": getattr(e, "code", "error"), "message": str(e)}}
            return env.model_dump(mode="json")

        tool.__name__ = mcp_name
        tool.__doc__ = f"{description} Canonical tool: {canonical}. Needs the run's session token."
        mcp.tool(name=mcp_name, description=tool.__doc__, structured_output=True)(tool)

    for mcp_name, canonical in MCP_NAME_MAP.items():
        register(mcp_name, canonical, TOOLS[canonical])

    @mcp.tool(name="enroll", description="Register your agent by name (same name + version = same agent) and get one session token per episode. No credential needed. Pass suite_id (default generated-practice-v1, four artificial weeks) or pack_id (e.g. gen_dev_short, a two-hour episode).", structured_output=True)
    def enroll(ctx: Context, agent_name: str, agent_version: str = "1", suite_id: str | None = None, pack_id: str | None = None) -> dict[str, Any]:
        try:
            if not public_runs():
                return {"status": "error", "error": {"code": "UNAUTHORIZED", "message": "public runs are switched off on this server; ask its operator"}}
            req = request_of(ctx)
            if client_ip is not None and req is not None:
                manager.rate_limit("runs", client_ip(req))
            return {"status": "ok", "data": manager.enroll(agent={"name": agent_name, "version": agent_version, "runtime": "external"}, suite_id=None if pack_id else (suite_id or "generated-practice-v1"), pack_id=pack_id)}
        except Exception as e:
            return {"status": "error", "error": {"code": getattr(e, "code", "error"), "message": str(e)}}

    @mcp.tool(name="run_status", description="Progress and, once finished, the result summary of a run (public; no token needed).", structured_output=True)
    def run_status(run_id: str) -> dict[str, Any]:
        try:
            view = manager.run_view(run_id)
        except Exception as e:
            return {"status": "error", "error": {"code": getattr(e, "code", "error"), "message": str(e)}}
        view.pop("session_credential", None)
        return {"status": "ok", "data": view}

    return mcp
