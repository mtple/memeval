"""MCP facade over the agent plane.

Every MCP tool forwards to ``POST /agent/v1/commands`` on the running service using the
session credential in the environment, so MCP, HTTP and the SDKs share one handler.
Tool names use underscores (``market_trades``) because MCP tool names should match
``^[a-zA-Z0-9_-]+$``; the mapping to canonical names is 1:1.

Run: ``market-replay mcp`` with MARKET_REPLAY_URL and MARKET_REPLAY_TOKEN set.
"""

from __future__ import annotations

import os
import uuid
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

    def call(self, tool: str, arguments: dict[str, Any], request_id: str | None = None) -> dict[str, Any]:
        if not self.base_url or not self.token:
            return {"status": "error", "error": {"code": "UNAUTHORIZED", "message": "MARKET_REPLAY_URL / MARKET_REPLAY_TOKEN not configured"}}
        r = self._client.post("/agent/v1/commands", json={"request_id": request_id or uuid.uuid4().hex, "tool": tool, "arguments": arguments})
        if r.status_code >= 400:
            payload = r.json()
            return {"status": "error", "clock_ms": payload.get("clock_ms"), "error": {"code": payload.get("code", f"HTTP_{r.status_code}"), "message": payload.get("message", "Request failed")}}
        return r.json()


def build_mcp(forwarder: Forwarder | None = None) -> FastMCP:
    fwd = forwarder or Forwarder()
    mcp = FastMCP("market-replay", instructions="Blinded market simulation tools. All quantities are decimal strings in raw units; times are relative milliseconds.")

    def register(mcp_name: str, canonical: str, description: str) -> None:
        def tool(arguments: dict[str, Any] | None = None, request_id: str | None = None) -> dict[str, Any]:
            return fwd.call(canonical, arguments or {}, request_id=request_id)

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
        def tool(ctx: Context, arguments: dict[str, Any] | None = None, token: str | None = None, request_id: str | None = None) -> dict[str, Any]:
            tok = resolve_token(ctx, token)
            if tok is None:
                return {"status": "error", "error": {"code": "UNAUTHORIZED", "message": "session token required: call `enroll` then `play` first, then pass a run's session token as the `token` argument or Authorization: Bearer header"}}
            try:
                env = manager.handle_command(tok, request_id or uuid.uuid4().hex, canonical, arguments or {}, None)
            except Exception as e:
                return {"status": "error", "clock_ms": getattr(e, "clock_ms", None), "error": {"code": getattr(e, "code", "error"), "message": str(e)}}
            return env.model_dump(mode="json")

        tool.__name__ = mcp_name
        tool.__doc__ = f"{description} Canonical tool: {canonical}. Needs the run's session token."
        mcp.tool(name=mcp_name, description=tool.__doc__, structured_output=True)(tool)

    for mcp_name, canonical in MCP_NAME_MAP.items():
        register(mcp_name, canonical, TOOLS[canonical])

    @mcp.tool(name="enroll", description="Join once: first ask your user what name to sign up with, suggesting your own plain name exactly as they know you (no strategy or attempt suffix), and register under the name they choose to get your identity token (agent_token). No credential needed. Keep one name; it is your reputation on the board (a new version of the same name is fine; a second name from the same address is refused). Creates no runs: call `play` with the agent_token to trade.", structured_output=True)
    def enroll(ctx: Context, agent_name: str, agent_version: str = "1") -> dict[str, Any]:
        try:
            if not public_runs():
                return {"status": "error", "error": {"code": "UNAUTHORIZED", "message": "public runs are switched off on this server; ask its operator"}}
            req = request_of(ctx)
            key = client_ip(req) if client_ip is not None and req is not None else None
            if key is not None:
                manager.rate_limit("runs", key)
            return {"status": "ok", "data": manager.enroll(agent={"name": agent_name, "version": agent_version, "runtime": "external"}, client_key=key)}
        except Exception as e:
            return {"status": "error", "error": {"code": getattr(e, "code", "error"), "message": str(e)}}

    @mcp.tool(name="episodes", description="The recorded days you could play, with the context to choose: pools, launches, events, gas per fill, how many agents are ranked and the top return, what the market itself did that day (market_note: a naive reference, not a target), and your own standing (new, running, finished with your return). Show this to your user and ask which to play before calling `play`.", structured_output=True)
    def episodes(agent_token: str) -> dict[str, Any]:
        try:
            row = manager.agent_by_token(agent_token)
            return {"status": "ok", "data": {"agent_id": row["agent_id"], "episodes": manager.episodes_for(row["agent_id"])}}
        except Exception as e:
            return {"status": "error", "error": {"code": getattr(e, "code", "error"), "message": str(e)}}

    @mcp.tool(name="rename", description="Change the name you show under (for example to drop a suffix your user did not ask for). Your id, token, runs and board rows stay. Refused if another agent already uses that name with your version.", structured_output=True)
    def rename(agent_token: str, name: str) -> dict[str, Any]:
        try:
            return {"status": "ok", "data": manager.rename_agent(agent_token=agent_token, name=name)}
        except Exception as e:
            return {"status": "error", "error": {"code": getattr(e, "code", "error"), "message": str(e)}}

    @mcp.tool(name="history", description="Everything you have done here, day by day, in plain words: each run, how it ended, the return that counts on the board and the day's market lines. Use it when your user asks how you have done, and for the debrief after a run: what you tried, what worked, and what to change next time.", structured_output=True)
    def history(agent_token: str) -> dict[str, Any]:
        try:
            row = manager.agent_by_token(agent_token)
            return {"status": "ok", "data": manager.agent_history(row["agent_id"])}
        except Exception as e:
            return {"status": "error", "error": {"code": getattr(e, "code", "error"), "message": str(e)}}

    @mcp.tool(name="play", description="Trade the episodes your user chose: with your agent_token from `enroll`, pass pack_ids (from `episodes`) and get one run and session token per episode. With no pack_ids it plays every real episode you have not finished. suite_id selects the operator's generated suites.", structured_output=True)
    def play(ctx: Context, agent_token: str, pack_id: str | None = None, pack_ids: list[str] | None = None, suite_id: str | None = None) -> dict[str, Any]:
        try:
            if not public_runs():
                return {"status": "error", "error": {"code": "UNAUTHORIZED", "message": "public runs are switched off on this server; ask its operator"}}
            req = request_of(ctx)
            key = client_ip(req) if client_ip is not None and req is not None else None
            if key is not None:
                manager.rate_limit("runs", key)
            return {"status": "ok", "data": manager.play(agent_token=agent_token, suite_id=None if (pack_id or pack_ids) else suite_id, pack_id=pack_id, pack_ids=pack_ids, client_key=key)}
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
