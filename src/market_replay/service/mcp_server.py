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
from mcp.server.fastmcp import FastMCP

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
