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
import gzip
import hashlib
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zlib
from http.client import HTTPException
from pathlib import Path

DEFAULT_SERVER = "https://memeval-web.vercel.app"
REVIEW_AFTER_MS = 300_000  # example attention schedule, chosen by the client and replaceable in decide()


class HTTPFailure(RuntimeError):
    def __init__(self, status: int, payload: dict):
        self.status, self.payload = status, payload
        super().__init__(f"HTTP {status}: {payload.get('message', 'request failed')}")


def http(method: str, url: str, body: dict | None = None, token: str | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json", "Accept-Encoding": "gzip"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            raw = r.read()  # Reject incomplete Content-Length bodies before JSON parsing.
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)  # Includes end-of-stream and checksum validation.
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("Expected a JSON object")
            return payload
    except urllib.error.HTTPError as e:
        raw = e.read()
        if e.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("Expected a JSON error object")
        except (ValueError, UnicodeError):
            payload = {"message": "non-JSON error response"}
        raise HTTPFailure(e.code, payload) from None


class Credentials:
    """An atomic, owner-only credential file. Never print its contents or commit it."""

    def __init__(self, path: Path, server: str, agent: str, version: str) -> None:
        self.path = path.expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.scope = {"server": server, "agent": agent, "version": version}
        self.data = {"schema": 1, **self.scope, "identity": None, "runs": {}}

    def __enter__(self):
        import fcntl

        self.lock_fd = os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self.path.exists():
                self.data = json.loads(self.path.read_text())
                if self.data.get("schema") != 1 or any(self.data.get(k) != v for k, v in self.scope.items()):
                    raise ValueError("Credential file belongs to a different server, agent or version.")
                if not isinstance(self.data.get("runs"), dict):
                    raise ValueError("Credential file has invalid run records.")
            self.save()  # Check persistence before creating any credentials on the server.
        except BaseException:
            os.close(self.lock_fd)
            raise
        return self

    def __exit__(self, *_):
        os.close(self.lock_fd)

    def save(self) -> None:
        fd, name = tempfile.mkstemp(prefix=self.path.name + ".", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self.data, f, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)


def default_state_path(server: str, agent: str, version: str) -> Path:
    scope = json.dumps([server, agent, version], separators=(",", ":"))
    key = hashlib.sha256(scope.encode()).hexdigest()[:24]
    root = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return root / "market-replay" / f"{key}.json"


class Session:
    """One run. Every tool call returns the same envelope: request_id, session_id, clock_ms, status, data, quality, error."""

    def __init__(self, commands_url: str, token: str, *, run_record: dict | None = None, save=None) -> None:
        self.url, self.token, self.session_id, self.clock_ms, self.n = commands_url, token, None, 0, 0
        self.request_prefix = uuid.uuid4().hex
        self.run_record = run_record if run_record is not None else {}
        self.save = save or (lambda: None)

    def _sync(self, response: dict) -> None:
        if type(response.get("clock_ms")) is int:
            self.clock_ms = response["clock_ms"]
        if response.get("session_id"):
            self.session_id = response["session_id"]

    def recover_pending(self) -> dict | None:
        body = self.run_record.get("pending_command")
        if body is None:
            return None
        for attempt in range(5):
            try:
                env = http("POST", self.url, body, self.token)
                self._sync(env)
                if (env.get("request_id") != body["request_id"] or env.get("status") not in ("ok", "error")
                        or type(env.get("clock_ms")) is not int or not env.get("session_id")):
                    raise ValueError("Incomplete command envelope")
                break
            except HTTPFailure as exc:
                self._sync(exc.payload)
                if exc.status not in (408, 429, 500, 502, 503, 504):
                    raise
            except (urllib.error.URLError, HTTPException, OSError, EOFError, ValueError, zlib.error):
                pass
            if attempt < 4:
                time.sleep(min(0.5 * 2**attempt, 4))
        else:
            raise RuntimeError("Response unavailable after retries. Pending request and credentials preserved; resume this run. Do not call session.finish.")
        self.run_record.pop("pending_command")
        self.save()
        return env

    def call(self, tool: str, **arguments) -> dict:
        if self.run_record.get("pending_command"):
            raise RuntimeError("Resolve the saved pending command with recover_pending before sending another command.")
        self.n += 1
        body = {"request_id": f"{self.request_prefix}_{self.n}", "tool": tool, "arguments": arguments}
        if self.session_id:
            body["session_id"] = self.session_id
        self.run_record["pending_command"] = body
        self.save()  # Persist the exact request before any bytes can reach the server.
        return self.recover_pending()

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
        arguments = {"format": "compact", "limit": 25}
        if since_ms is not None:
            arguments["since_ms"] = since_ms
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
    # Reaching the deadline is the only automatic completion path. Exceptions propagate
    # to run_saved and preserve the attempt. Custom policies own their exit decisions.
    portfolio = s.ok("portfolio.get")
    if portfolio.get("pending_orders") or any(b["asset_id"] != info["numeraire"]["asset_id"]
            and any(int(b.get(k, "0")) for k in ("available_raw", "reserved_raw", "pending_raw"))
            for b in portfolio.get("balances", [])):
        raise RuntimeError("Holdings or pending orders remain. Review deliberately before confirming finish; the starter will not liquidate or finish automatically.")
    result = s.ok("session.finish", confirm=True)
    valuation = result["terminal_portfolio"]["valuation"]
    return {"clock_ms": result.get("clock_ms", s.clock_ms), "model_equity_raw": valuation.get("model_equity_raw"),
            "final_cash_raw": str(int(valuation["cash_available_raw"]) + int(valuation["cash_reserved_raw"])),
            "valuation_complete": valuation.get("complete")}


TERMINAL = {"completed", "aborted", "agent_failed", "environment_failed"}


def run_saved(a, credentials: Credentials) -> int:
    server = a.server.rstrip("/")
    saved = credentials.data
    joined = saved["identity"]
    if not joined:
        if a.resume:
            raise ValueError("No saved identity or runs. Use the credential file from the original invocation.")
        joined = http("POST", f"{server}/api/v1/enroll", {"agent": {"name": a.agent, "version": a.version, "runtime": "external"}})
        saved["identity"] = joined
        credentials.save()

    if not (a.pack or a.suite or a.all or a.resume):
        listing = http("GET", f"{server}/api/v1/play", token=joined["agent_token"])
        print(f"joined as {joined['agent_name']} v{joined['agent_version']}. Recorded days:")
        for e in listing.get("episodes") or []:
            print(f"- {e['pack_id']}  {e['label']}: {e.get('pools_tradable')} tradable pools, {e.get('tape_events')} events; you: {e.get('your_status')}")
        print("pick with --pack <pack_id> (repeatable), or --all. Use --resume to continue saved unfinished runs.")
        return 0

    runs = []
    for r in saved["runs"].values():
        if a.resume and a.resume not in ("all", r["run_id"]):
            continue
        if not a.resume and not a.all:
            if a.suite and r.get("suite_id") != a.suite:
                continue
            if a.pack and not any(ref in (r["pack_id"], r["pack_name"]) for ref in a.pack):
                continue
        if r.get("state") in TERMINAL:
            continue
        status = http("GET", f"{server}/api/v1/runs/{r['run_id']}")
        r["state"] = status["state"]
        credentials.save()
        if r["state"] not in TERMINAL:
            runs.append(r)
    if a.resume and not runs:
        print("No unfinished runs match the saved credentials. No new run was created.")
        return 0

    # Resume saved sessions first. An explicit pack selection can also contain new episodes.
    body = None
    if a.pack:
        remaining = [ref for ref in a.pack if not any(ref in (r["pack_id"], r["pack_name"]) for r in runs)]
        if remaining:
            body = {"pack_ids": remaining}
    elif not runs and not a.resume:
        body = {"suite_id": a.suite} if a.suite else {}
    if body is not None:
        enrolled = http("POST", f"{server}/api/v1/play", body, joined["agent_token"])
        for r in enrolled["runs"]:
            r["suite_id"] = enrolled.get("suite_id")
            r["state"] = "running"
            saved["runs"][r["run_id"]] = r
        credentials.save()  # Persist every run token before issuing the first session command.
        runs.extend(enrolled["runs"])

    results_url = joined.get("results_url", server)
    print(f"joined as {joined['agent_name']} v{joined['agent_version']}; {len(runs)} episode(s) to play; results: {results_url}")
    failed = False
    for r in runs:
        cred = r["session_credential"]
        if cred["commands_url"] != f"{server}/agent/v1/commands":
            raise ValueError("Saved command URL does not match the selected server.")
        print(f"- {r['pack_name']} ({r['run_id']}) ...", end=" ", flush=True)
        try:
            session = Session(cred["commands_url"], cred["token"], run_record=r, save=credentials.save)
            session.recover_pending()
            out = play(session)
            r["state"] = "completed"
            credentials.save()
            print(f"finished at {out['clock_ms']} ms; settled cash {out['final_cash_raw']} raw; model equity {out['model_equity_raw']} (valuation complete: {out['valuation_complete']})")
        except Exception as e:
            failed = True
            print(f"interrupted: {e}. Credentials retained; continue with --resume {r['run_id']}.")
    print(f"{'interrupted' if failed else 'done'}. read the reports at {results_url}")
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default=DEFAULT_SERVER)
    ap.add_argument("--agent", required=True, help="your agent's name (same name + version = same agent)")
    ap.add_argument("--version", default="1")
    selection = ap.add_mutually_exclusive_group()
    selection.add_argument("--suite", help="an operator test suite id")
    selection.add_argument("--pack", action="append", help="play this episode; repeatable; saved unfinished runs resume")
    selection.add_argument("--all", action="store_true", help="resume saved runs first, otherwise play unfinished recorded days")
    selection.add_argument("--resume", nargs="?", const="all", help="resume saved unfinished runs, or one run id; creates no runs")
    ap.add_argument("--state-file", type=Path, help="private credential file; default is scoped by server, agent and version under XDG_STATE_HOME/market-replay")
    a = ap.parse_args()
    server = a.server.rstrip("/")
    try:
        with Credentials(a.state_file or default_state_path(server, a.agent, a.version), server, a.agent, a.version) as credentials:
            return run_saved(a, credentials)
    except BlockingIOError:
        print("Another starter invocation is using this credential file.", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"stopped: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
