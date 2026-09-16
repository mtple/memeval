"""Client conformance checks that any implementation can run against a fresh session.

Run: ``python -m market_replay_client.conformance`` with MARKET_REPLAY_URL/TOKEN set.
Each check returns (name, passed, detail). The checks only use the public tool surface.
"""

from __future__ import annotations

import json
import sys

from . import MarketReplayClient, client_from_env

REQUIRED_KEYS = {"request_id", "session_id", "clock_ms", "status", "data", "quality", "error"}


def run_conformance(c: MarketReplayClient) -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        results.append((name, bool(cond), detail))

    env = c.call("session.describe", request_id="conf_describe")
    check("envelope_shape", REQUIRED_KEYS.issubset(env), str(sorted(env)))
    check("request_id_echo", env.get("request_id") == "conf_describe")
    check("describe_ok", env["status"] == "ok" and "tools" in env["data"])
    d = env["data"]
    numeraire = d["numeraire"]["asset_id"]

    bad = c.call("markets.get", {"pool_id": "pool_doesnotexist"})
    bad2 = c.call("markets.get", {"pool_id": "0x0000000000000000000000000000000000000001"})
    check("unknown_and_canonical_ids_uniform", bad["status"] == "error" and bad2["status"] == "error" and bad["error"]["code"] == bad2["error"]["code"] == "NOT_YET_DISCOVERED")

    unsupported = c.call("wallet.history", {"wallet": "x"})
    check("unsupported_capability_typed", unsupported["status"] == "error" and unsupported["error"]["code"] == "UNSUPPORTED_CAPABILITY")

    lst = c.ok("markets.list", {"limit": 5})
    check("markets_list_paginated", "items" in lst and "next_cursor" in lst and "total_currently_discoverable" in lst)
    if lst["items"]:
        pid = lst["items"][0]["pool_id"]
        now = c.clock_ms
        tr = c.call("market.trades", {"pool_id": pid, "end_ms": now + 10_000_000})
        check("future_range_clamped", tr["status"] == "ok" and tr["data"]["range"]["end_ms"] <= tr["clock_ms"] and "RANGE_CLAMPED_TO_PRESENT" in tr["quality"]["warnings"])
        cd = c.call("market.candles", {"pool_id": pid, "interval_ms": 60_000, "start_ms": max(0, c.clock_ms - 600_000)})
        check("candles_closed_only_by_default", cd["status"] == "ok" and all(x["closed"] for x in cd["data"]["items"]))
        q = c.call("broker.quote", {"pool_id": pid, "asset_in": numeraire, "amount_in_raw": "1000"})
        check("quote_ok_or_typed_error", q["status"] == "ok" or q["error"]["code"] in {"NO_ROUTE", "UNSUPPORTED_CAPABILITY", "MODEL_CAPACITY_LIMIT", "MISSING_DATA"})
        if q["status"] == "ok":
            base = q["data"]["asset_out"]
            args = {"pool_id": pid, "asset_in": numeraire, "asset_out": base, "amount_in_raw": "1000", "min_amount_out_raw": "0", "deadline_ms": c.clock_ms + 60_000, "idempotency_key": "conf_idem_1"}
            s1 = c.call("broker.submit", args)
            s2 = c.call("broker.submit", args)
            check("idempotent_resubmit_returns_original", s1["status"] == "ok" and s2["status"] == "ok" and s2["data"]["created"] is False and s1["data"]["order"]["order_id"] == s2["data"]["order"]["order_id"])
            s3 = c.call("broker.submit", {**args, "amount_in_raw": "1001"})
            check("idempotency_conflict_typed", s3["status"] == "error" and s3["error"]["code"] == "IDEMPOTENCY_CONFLICT")
            adv = c.ok("clock.advance", {"to_ms": c.clock_ms + 30_000})
            o = c.ok("broker.order", {"order_id": s1["data"]["order"]["order_id"]})
            check("order_reaches_terminal_state", o["order"]["state"] in {"confirmed", "reverted", "expired", "model_capacity_rejected"}, o["order"]["state"])
            pf = c.ok("portfolio.get")
            check("portfolio_shape", {"balances", "valuation", "pending_orders"}.issubset(pf))
            check("raw_quantities_are_strings", all(isinstance(b["available_raw"], str) for b in pf["balances"]))
            _ = adv
    past = c.call("clock.advance", {"to_ms": max(0, c.clock_ms - 1)})
    check("advance_backwards_rejected", past["status"] == "error" and past["error"]["code"] == "INVALID_REQUEST")
    hist = c.ok("portfolio.history", {"limit": 5})
    check("history_paginated", "items" in hist and "total" in hist)
    return results


def main() -> int:
    c = client_from_env()
    results = run_conformance(c)
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'} {name} {detail}")
    print(json.dumps({"passed": passed, "total": len(results)}))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
