"""One fake endpoint that answers for the v2 world and the v4 world (same anchors, different contracts)."""

from __future__ import annotations

import json

import httpx

from tests.integration.test_collector import FACTORY, PAIR, FakeBase
from tests.integration.test_collector_cl import FakePoolManager

PERIOD_START = "2025-09-04T14:53:20Z"
PERIOD_END = "2025-09-04T15:53:20Z"


class FakeAllVenues:
    def __init__(self) -> None:
        self.v2 = FakeBase()
        self.v4 = FakePoolManager(protocol="uniswap_v4")
        self.calls = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        body = json.loads(request.content)
        method, params = body["method"], body["params"]
        v2_addrs = {FACTORY, PAIR}
        if method == "eth_getLogs":
            addr = params[0].get("address")
            addrs = {a.lower() for a in (addr if isinstance(addr, list) else [addr])} if addr else set()
            return self.v2.handle(request) if addrs and addrs <= v2_addrs else self.v4.handle(request)
        if method == "eth_call" and params[0]["to"].lower() in v2_addrs:
            return self.v2.handle(request)
        return self.v4.handle(request)
