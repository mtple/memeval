#!/usr/bin/env python3
"""Market Replay participant. Enrolls an agent in a suite and plays every episode. Standard library only.

    python3 market_replay_agent.py --agent my-bot --version 1
    python3 market_replay_agent.py --agent my-bot --server https://memeval-web.vercel.app

Replace `decide` with your strategy. The default holds cash, which is a legitimate result.
All quantities are decimal strings in raw units (never floats); all times are integer milliseconds
relative to the episode start.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

DEFAULT_SERVER = "https://memeval-web.vercel.app"
STEP_MS = 6 * 3_600_000  # advance the virtual clock six hours per decision


def http(method: str, url: str, body: dict | None = None, token: str | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{method} {url} -> HTTP {e.code}: {e.read()[:400].decode(errors='replace')}") from None


class Session:
    """One run. Every tool call returns the same envelope: request_id, session_id, clock_ms, status, data, quality, error."""

    def __init__(self, commands_url: str, token: str) -> None:
        self.url, self.token, self.session_id, self.clock_ms, self.n = commands_url, token, None, 0, 0

    def call(self, tool: str, **arguments) -> dict:
        self.n += 1
        body = {"request_id": f"req_{self.n}", "tool": tool, "arguments": arguments}
        if self.session_id:
            body["session_id"] = self.session_id
        env = http("POST", self.url, body, self.token)
        self.session_id = env.get("session_id", self.session_id)
        self.clock_ms = int(env.get("clock_ms", self.clock_ms))
        return env

    def ok(self, tool: str, **arguments) -> dict:
        env = self.call(tool, **arguments)
        if env.get("status") != "ok":
            err = env.get("error") or {}
            raise RuntimeError(f"{tool}: {err.get('code')}: {err.get('message')}")
        return env["data"]


def decide(s: Session, info: dict, memory: dict) -> list[dict]:
    """Called once per step with the session, the session.describe data and a dict you may keep state in.

    Return broker.submit argument dicts to place orders, or [] to wait. Example that buys the first
    tradable pool once with 10% of the bankroll:

        if not memory.get("bought"):
            cash = info["numeraire"]["asset_id"]
            pools = s.ok("markets.list", limit=50, filters={"execution_supported_only": True})["items"]
            if pools:
                p = pools[0]
                amount = str(int(info["bankroll_raw"]) // 10)
                q = s.ok("broker.quote", pool_id=p["pool_id"], asset_in=cash, amount_in_raw=amount)
                memory["bought"] = True
                return [{"pool_id": p["pool_id"], "asset_in": cash, "asset_out": p["base_asset_id"] if p["quote_asset_id"] == cash else p["quote_asset_id"],
                         "amount_in_raw": amount, "min_amount_out_raw": str(int(q["amount_out_raw"]) * 95 // 100),
                         "deadline_ms": s.clock_ms + 900_000, "idempotency_key": "first_buy", "quote_id": q.get("quote_id")}]
        return []
    """
    return []


def play(s: Session) -> dict:
    info = s.ok("session.describe")
    duration = int(info["episode"]["duration_ms"])
    memory: dict = {}
    while True:
        for order in decide(s, info, memory):
            env = s.call("broker.submit", **order)
            if env["status"] != "ok":
                print("  order refused:", env["error"]["code"], env["error"]["message"], file=sys.stderr)
        adv = s.ok("clock.advance", to_ms=min(s.clock_ms + STEP_MS, duration))
        if adv.get("episode_ended") or s.clock_ms >= duration:
            break
    portfolio = s.ok("portfolio.get")
    result = s.ok("session.finish")
    return {"clock_ms": result.get("clock_ms", s.clock_ms), "model_equity_raw": (portfolio.get("valuation") or {}).get("model_equity_raw"), "valuation_complete": (portfolio.get("valuation") or {}).get("complete")}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default=DEFAULT_SERVER)
    ap.add_argument("--agent", required=True, help="your agent's name (same name + version = same agent)")
    ap.add_argument("--version", default="1")
    ap.add_argument("--suite", default=None, help="an operator test suite id (default: every real recorded week on the server)")
    ap.add_argument("--pack", default=None, help="run one week only (its pack id from GET /api/v1/packs)")
    a = ap.parse_args()
    server = a.server.rstrip("/")

    body = {"agent": {"name": a.agent, "version": a.version, "runtime": "external"}}
    if a.pack:
        body["pack_id"] = a.pack
    elif a.suite:
        body["suite_id"] = a.suite
    enrolled = http("POST", f"{server}/api/v1/enroll", body)
    print(f"enrolled {enrolled['agent_name']} v{enrolled['agent_version']} in {len(enrolled['runs'])} episode(s); results: {enrolled['results_url']}")
    for r in enrolled["runs"]:
        cred = r["session_credential"]
        print(f"- {r['pack_name']} ({r['run_id']}) ...", end=" ", flush=True)
        try:
            out = play(Session(cred["commands_url"], cred["token"]))
            print(f"finished at {out['clock_ms']} ms; model equity {out['model_equity_raw']} (valuation complete: {out['valuation_complete']})")
        except Exception as e:  # keep going: one failed episode is still a result
            print(f"failed: {e}")
    print(f"done. read the reports at {enrolled['results_url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
