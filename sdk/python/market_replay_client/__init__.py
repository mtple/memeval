"""Thin Python client for the Market Replay agent plane.

This package is an ordinary external client: it only speaks HTTP to
``POST /agent/v1/commands`` and never imports engine internals.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import httpx

__all__ = ["MarketReplayClient", "CommandError", "client_from_env"]


class CommandError(RuntimeError):
    def __init__(self, envelope: dict[str, Any]) -> None:
        err = envelope.get("error") or {}
        super().__init__(f"{err.get('code')}: {err.get('message')}")
        self.envelope = envelope
        self.code = err.get("code")
        self.message = err.get("message")
        self.details = err.get("details") or {}


class MarketReplayClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 60.0, transport: httpx.BaseTransport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout, headers={"Authorization": f"Bearer {token}"}, transport=transport)
        self.session_id: str | None = None
        self.clock_ms: int = 0

    # ------------------------------------------------------------------ core
    def call(self, tool: str, arguments: dict[str, Any] | None = None, *, request_id: str | None = None, raise_on_error: bool = False) -> dict[str, Any]:
        body = {"request_id": request_id or f"req_{uuid.uuid4().hex[:16]}", "tool": tool, "arguments": arguments or {}}
        if self.session_id:
            body["session_id"] = self.session_id
        r = self._http.post("/agent/v1/commands", json=body)
        if r.status_code >= 400:
            raise CommandError({"error": {"code": f"HTTP_{r.status_code}", "message": r.text[:300]}})
        env = r.json()
        self.session_id = env.get("session_id", self.session_id)
        self.clock_ms = int(env.get("clock_ms", self.clock_ms))
        if raise_on_error and env.get("status") != "ok":
            raise CommandError(env)
        return env

    def ok(self, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """Call and return only ``data``; raises CommandError on an error envelope."""
        return self.call(tool, arguments, raise_on_error=True)["data"]

    def capabilities(self) -> dict[str, Any]:
        r = self._http.get("/agent/v1/capabilities")
        r.raise_for_status()
        return r.json()

    # ------------------------------------------------------------------ convenience
    def describe(self) -> dict[str, Any]:
        return self.ok("session.describe")

    def snapshot(self, **arguments: Any) -> dict[str, Any]:
        """Observe in one budgeted read; format="compact" returns columns plus row arrays."""
        return self.ok("session.snapshot", arguments)

    def wait(self, until_ms: int, conditions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Wait for a deadline or a delayed notification; conditions last for this call only."""
        return self.ok("clock.wait", {"until_ms": until_ms, "conditions": conditions or []})

    def markets(self, *, limit: int = 100, cursor: int | None = None, **filters: Any) -> dict[str, Any]:
        args: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            args["cursor"] = cursor
        if filters:
            args["filters"] = filters
        return self.ok("markets.list", args)

    def all_markets(self, **filters: Any) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor: int | None = None
        while True:
            page = self.markets(limit=200, cursor=cursor, **filters)
            items.extend(page["items"])
            cursor = page.get("next_cursor")
            if cursor is None:
                return items

    def market(self, pool_id: str) -> dict[str, Any]:
        return self.ok("markets.get", {"pool_id": pool_id})

    def trades(self, pool_id: str, **kw: Any) -> dict[str, Any]:
        return self.ok("market.trades", {"pool_id": pool_id, **kw})

    def candles(self, pool_id: str, interval_ms: int, **kw: Any) -> dict[str, Any]:
        return self.ok("market.candles", {"pool_id": pool_id, "interval_ms": interval_ms, **kw})

    def liquidity(self, pool_id: str) -> dict[str, Any]:
        return self.ok("market.liquidity", {"pool_id": pool_id})

    def restrictions(self, pool_id: str) -> dict[str, Any]:
        return self.ok("market.restrictions", {"pool_id": pool_id})

    def quote(self, pool_id: str, asset_in: str, amount_in_raw: str | int) -> dict[str, Any]:
        return self.ok("broker.quote", {"pool_id": pool_id, "asset_in": asset_in, "amount_in_raw": str(amount_in_raw)})

    def submit(self, *, pool_id: str, asset_in: str, asset_out: str, amount_in_raw: str | int, min_amount_out_raw: str | int, deadline_ms: int, idempotency_key: str, quote_id: str | None = None, reason: str | None = None, exit_condition: str | None = None) -> dict[str, Any]:
        args = {
            "pool_id": pool_id,
            "asset_in": asset_in,
            "asset_out": asset_out,
            "amount_in_raw": str(amount_in_raw),
            "min_amount_out_raw": str(min_amount_out_raw),
            "deadline_ms": deadline_ms,
            "idempotency_key": idempotency_key,
        }
        if quote_id:
            args["quote_id"] = quote_id
        if reason is not None:
            args["reason"] = reason
        if exit_condition is not None:
            args["exit_condition"] = exit_condition
        return self.call("broker.submit", args)

    def order(self, order_id: str | None = None) -> dict[str, Any]:
        return self.ok("broker.order", {"order_id": order_id} if order_id else {})

    def portfolio(self) -> dict[str, Any]:
        return self.ok("portfolio.get")

    def history(self, cursor: int = 0, limit: int = 100) -> dict[str, Any]:
        return self.ok("portfolio.history", {"cursor": cursor, "limit": limit})

    def advance(self, to_ms: int) -> dict[str, Any]:
        return self.ok("clock.advance", {"to_ms": to_ms})

    def advance_next(self, max_ms: int) -> dict[str, Any]:
        return self.ok("clock.advance", {"next_event": True, "max_ms": max_ms})

    def finish(self) -> dict[str, Any]:
        return self.ok("session.finish")

    def close(self) -> None:
        self._http.close()


def client_from_env() -> MarketReplayClient:
    url = os.environ.get("MARKET_REPLAY_URL")
    token = os.environ.get("MARKET_REPLAY_TOKEN")
    if not url or not token:
        raise SystemExit("MARKET_REPLAY_URL and MARKET_REPLAY_TOKEN must be set")
    return MarketReplayClient(url, token)
