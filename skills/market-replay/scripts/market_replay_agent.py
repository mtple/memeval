#!/usr/bin/env python3
"""Market Replay participant. Joins, lists the recorded days, and plays the ones you pick. Standard library only.

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
REVIEW_AFTER_MS = 300_000  # example attention schedule, chosen by the client and replaceable in decide()


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


def decide(s: Session, info: dict, snapshot: dict, notifications: list[dict], memory: dict) -> dict:
    """Inspect the snapshot and decide what to do, then when to revisit.

    The default observes and holds cash. Replace this function with your decision policy.
    Use s.ok for drill-down reads or quotes; no LLM call is required. Return tool commands as
    actions, e.g. {"tool": "broker.submit", "arguments": your_order}. Results are supplied to
    the next decision in memory["action_results"]. Quotes use expected_amount_out_raw.

    Return watchlist as pool aliases to narrow the next snapshot, or None for the market page.
    Discoveries remain separate. Check next_cursor on markets/discoveries/orders and fetch
    further snapshot pages with the same since_ms when your policy needs them. A snapshot's
    first page does not claim complete market inspection.

    Conditions last for one wait. Add price_cross, liquidity_below or order_terminal conditions
    as needed. Set review_after_ms for a position review deadline. All thresholds, order sizes,
    slippage tolerances and exit decisions belong to your policy.
    """
    return {"actions": [], "watchlist": None, "review_after_ms": REVIEW_AFTER_MS,
            "conditions": [{"kind": "new_pool", "since_ms": snapshot["as_of_ms"]}]}


def play(s: Session) -> dict:
    info = s.ok("session.describe")
    duration = int(info["episode"]["duration_ms"])
    memory: dict = {}
    since_ms, watchlist, notifications = None, None, []
    while s.clock_ms < duration:
        arguments = {} if since_ms is None else {"since_ms": since_ms}
        if watchlist is not None:
            arguments["pool_ids"] = watchlist
        snapshot = s.ok("session.snapshot", **arguments)
        if s.clock_ms >= duration:
            break
        plan = decide(s, info, snapshot, notifications, memory)
        memory["action_results"] = []
        for action in plan.get("actions", []):
            env = s.call(action["tool"], **action.get("arguments", {}))
            memory["action_results"].append(env)
            if env["status"] != "ok":
                print("  action refused:", env["error"]["code"], env["error"]["message"], file=sys.stderr)
        watchlist = plan.get("watchlist", watchlist)
        since_ms = snapshot["as_of_ms"]
        adv = s.ok("clock.wait", until_ms=min(s.clock_ms + max(1, int(plan.get("review_after_ms", REVIEW_AFTER_MS))), duration),
                   conditions=plan.get("conditions", []))
        notifications = adv["alerts"]
        if adv.get("episode_ended") or s.clock_ms >= duration:
            break
    result = s.ok("session.finish")
    valuation = result["terminal_portfolio"]["valuation"]
    return {"clock_ms": result.get("clock_ms", s.clock_ms), "model_equity_raw": valuation.get("model_equity_raw"),
            "final_cash_raw": str(int(valuation["cash_available_raw"]) + int(valuation["cash_reserved_raw"])),
            "valuation_complete": valuation.get("complete")}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default=DEFAULT_SERVER)
    ap.add_argument("--agent", required=True, help="your agent's name (same name + version = same agent)")
    ap.add_argument("--version", default="1")
    ap.add_argument("--suite", default=None, help="an operator test suite id (default: every real recorded episode you have not finished)")
    ap.add_argument("--pack", action="append", default=None, help="play this episode (its pack id from the list); repeatable")
    ap.add_argument("--all", action="store_true", help="play every recorded day you have not finished")
    a = ap.parse_args()
    server = a.server.rstrip("/")

    joined = http("POST", f"{server}/api/v1/enroll", {"agent": {"name": a.agent, "version": a.version, "runtime": "external"}})
    body: dict = {}
    if a.pack:
        body["pack_ids"] = a.pack
    elif a.suite:
        body["suite_id"] = a.suite
    elif not a.all:
        # The choice of what to play is the user's: show the days and stop.
        print(f"joined as {joined['agent_name']} v{joined['agent_version']}. Recorded days:")
        for e in joined.get("episodes") or []:
            print(f"- {e['pack_id']}  {e['label']}: {e.get('pools_tradable')} tradable pools ({e.get('launches')} launched that day), {e.get('tape_events')} events, gas {e.get('gas_per_fill')} per fill, {e.get('agents_ranked')} agent(s) ranked, top {e.get('top_return')}; you: {e.get('your_status')}")
        print("pick with --pack <pack_id> (repeatable), or --all for every day you have not finished.")
        return 0
    enrolled = http("POST", f"{server}/api/v1/play", body, joined["agent_token"])
    print(f"joined as {enrolled['agent_name']} v{enrolled['agent_version']}; {len(enrolled['runs'])} episode(s) to play, {len(enrolled.get('skipped') or [])} already finished; results: {enrolled['results_url']}")
    for r in enrolled["runs"]:
        cred = r["session_credential"]
        print(f"- {r['pack_name']} ({r['run_id']}) ...", end=" ", flush=True)
        try:
            out = play(Session(cred["commands_url"], cred["token"]))
            print(f"finished at {out['clock_ms']} ms; settled cash {out['final_cash_raw']} raw; model equity {out['model_equity_raw']} (valuation complete: {out['valuation_complete']})")
        except Exception as e:  # keep going: one failed episode is still a result
            print(f"failed: {e}")
    print(f"done. read the reports at {enrolled['results_url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
