"""Reference participant 3: seeded random actions.

Bounded, valid random actions drawn from a seeded RNG (MARKET_REPLAY_AGENT_SEED). Useful
for detecting accounting or selection artifacts. The RNG is the only source of variation;
market information is read but never used to choose.
"""

from __future__ import annotations

import os
import random
import sys

from market_replay_client import CommandError, client_from_env

MIN_WAIT_MS = 60_000
MAX_WAIT_MS = 4 * 3_600_000


def main() -> int:
    seed = os.environ.get("MARKET_REPLAY_AGENT_SEED", "42")
    rng = random.Random(seed)
    c = client_from_env()
    info = c.describe()
    duration = info["episode"]["duration_ms"]
    numeraire = info["numeraire"]["asset_id"]
    gas = int(info["execution"]["gas_cost_raw"])
    intent = 0
    steps = 0
    while c.clock_ms < duration and steps < 5000:
        steps += 1
        action = rng.choices(["wait", "buy", "sell", "look"], weights=[5, 2, 2, 1])[0]
        if action == "wait":
            c.advance(min(c.clock_ms + rng.randint(MIN_WAIT_MS, MAX_WAIT_MS), duration))
            continue
        pools = c.all_markets(execution_supported_only=True)
        if not pools:
            c.advance(min(c.clock_ms + MAX_WAIT_MS, duration))
            continue
        m = rng.choice(pools)
        if action == "look":
            try:
                c.trades(m["pool_id"], limit=20)
                c.candles(m["pool_id"], 300_000, start_ms=max(-3_600_000, c.clock_ms - 3_600_000))
            except CommandError:
                pass
            continue
        pf = c.portfolio()
        if action == "buy":
            cash = int(next(b["available_raw"] for b in pf["balances"] if b["asset_id"] == numeraire))
            spend = rng.randint(1, max(1, cash // 50))
            if spend + gas > cash:
                continue
            asset_in, asset_out, amount = numeraire, m["base_asset"], spend
        else:
            h = [x for x in pf["valuation"]["holdings"] if x["class"] == "priced_liquidatable" and x["asset_id"] == m["base_asset"]]
            if not h:
                continue
            qty = int(h[0]["quantity_raw"])
            amount = rng.randint(1, qty)
            asset_in, asset_out = m["base_asset"], numeraire
        try:
            q = c.quote(m["pool_id"], asset_in, amount)
        except CommandError:
            continue
        if not q["capacity_ok"]:
            continue
        tol_bps = rng.choice([0, 50, 100, 300])
        min_out = int(q["expected_amount_out_raw"]) * (10_000 - tol_bps) // 10_000
        intent += 1
        c.submit(pool_id=m["pool_id"], asset_in=asset_in, asset_out=asset_out, amount_in_raw=amount, min_amount_out_raw=min_out, deadline_ms=c.clock_ms + rng.randint(5_000, 600_000), idempotency_key=f"rand_{intent}")
        c.advance_next(rng.randint(5_000, 120_000))
    while c.clock_ms < duration:
        c.advance(min(c.clock_ms + MAX_WAIT_MS, duration))
    result = c.finish()
    print("random_actions finished", result["clock_ms"], "intents", intent)
    return 0


if __name__ == "__main__":
    sys.exit(main())
