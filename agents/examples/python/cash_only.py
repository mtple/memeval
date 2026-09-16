"""Reference participant 1: cash-only. Never trades.

Verifies that abstention is a legitimate action and that cost accounting is stable.
Ordinary external client; no engine imports.
"""

from __future__ import annotations

import sys

from market_replay_client import client_from_env

STEP_MS = 6 * 3_600_000


def main(client=None) -> int:
    c = client or client_from_env()
    info = c.describe()
    duration = info["episode"]["duration_ms"]
    while True:
        adv = c.advance(min(c.clock_ms + STEP_MS, duration))
        if adv["episode_ended"]:
            break
    pf = c.portfolio()
    result = c.finish()
    print("cash_only finished at", result["clock_ms"], "equity", pf["valuation"]["model_equity_raw"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
