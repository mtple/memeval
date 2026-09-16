"""Reference participant 4 (optional): generic model client.

Chooses among the market tools by asking an inference gateway for a JSON tool call. No
trading strategy is prescribed. With the default ``mock`` provider it runs offline and
never trades, so tests never require paid inference. Provider credentials never enter
this process when a real gateway is used; here the gateway object is local and reads
its own configuration.
"""

from __future__ import annotations

import json
import sys

from market_replay_client import client_from_env

try:
    from market_replay.runners.model_gateway import GatewayError, ModelGateway
except ImportError:  # gateway not installed in this environment
    ModelGateway = None  # type: ignore[assignment]
    GatewayError = RuntimeError  # type: ignore[assignment,misc]

SYSTEM = (
    "You are a participant in a blinded market simulation. You may call the listed tools. "
    "Waiting and holding cash are legitimate. Respond with one JSON object {\"tool\":..., \"arguments\":{...}}."
)
MAX_STEPS = 400


def main() -> int:
    if ModelGateway is None:
        print("model gateway unavailable; install the market-replay package to run this example")
        return 2
    gw = ModelGateway.from_env()
    c = client_from_env()
    info = c.describe()
    duration = info["episode"]["duration_ms"]
    tools = info["tools"]
    history: list[dict[str, str]] = []
    steps = 0
    while c.clock_ms < duration and steps < MAX_STEPS:
        steps += 1
        state = {"clock_ms": c.clock_ms, "remaining_ms": duration - c.clock_ms, "portfolio": c.portfolio()["valuation"]["model_equity_raw"], "tools": list(tools)}
        history.append({"role": "user", "content": json.dumps(state) + "\n" * min(steps, 50)})
        try:
            out = gw.complete(system=SYSTEM, messages=history[-6:], tools_schema=[])
        except GatewayError as e:
            print("gateway refused:", e)
            break
        try:
            choice = json.loads(out["text"])
            tool = str(choice["tool"])
            args = dict(choice.get("arguments") or {})
        except (ValueError, KeyError, TypeError):
            tool, args = "clock.advance", {"next_event": True, "max_ms": 3_600_000}
        if tool == "session.finish":
            break
        env = c.call(tool, args)
        history.append({"role": "assistant", "content": out["text"]})
        history.append({"role": "user", "content": json.dumps({"result_status": env["status"], "error": env.get("error")})})
    while c.clock_ms < duration:
        c.advance(min(c.clock_ms + 6 * 3_600_000, duration))
    result = c.finish()
    print("model_client finished", result["clock_ms"], json.dumps(gw.summary()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
