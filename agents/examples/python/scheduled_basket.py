"""Reference participant 2: scheduled basket.

Predeclared schedule, no market view:
  * At each buy slot (relative hours listed in BUY_SLOTS_H) spend a fixed fraction of the
    *initial* bankroll, split equally across all currently discoverable, execution-supported
    pools whose quote passes the capacity profile. New listings join at the next slot.
  * At SELL_SLOT_H, submit sells for every holding (skipping no-route holdings).
  * Otherwise wait. Orders use a 1% minimum-output tolerance from the quote.
This is a control, not a strategy recommendation.
"""

from __future__ import annotations

import sys

from market_replay_client import CommandError, client_from_env

BUY_SLOTS_H = [1, 25, 49, 73, 97]
SELL_SLOT_H = 150
SPEND_FRACTION_PER_SLOT_BPS = 800  # 8% of initial bankroll per slot
MIN_OUT_TOLERANCE_BPS = 100
HOUR = 3_600_000


def main() -> int:
    c = client_from_env()
    info = c.describe()
    duration = info["episode"]["duration_ms"]
    numeraire = info["numeraire"]["asset_id"]
    bankroll = int(info["bankroll_raw"])
    gas = int(info["execution"]["gas_cost_raw"])
    schedule = sorted([(h * HOUR, "buy") for h in BUY_SLOTS_H] + [(SELL_SLOT_H * HOUR, "sell")])
    schedule = [(t, k) for t, k in schedule if t < duration]
    if not schedule or schedule[-1][1] != "sell":
        # Short fixtures (sell slot beyond the episode): compress the schedule proportionally.
        n = max(1, len(BUY_SLOTS_H))
        schedule = [((i + 1) * duration // (n + 3), "buy") for i in range(n)] + [(duration * 9 // 10, "sell")]
    intent = 0
    for slot_ms, kind in schedule:
        if c.clock_ms < slot_ms:
            c.advance(slot_ms)
        if kind == "buy":
            pools = [m for m in c.all_markets(execution_supported_only=True)]
            if not pools:
                continue
            per_slot = bankroll * SPEND_FRACTION_PER_SLOT_BPS // 10_000
            per_pool = per_slot // len(pools)
            cash = int(next(b["available_raw"] for b in c.portfolio()["balances"] if b["asset_id"] == numeraire))
            for m in sorted(pools, key=lambda x: x["pool_id"]):
                if per_pool <= 0 or cash < per_pool + gas:
                    break
                try:
                    q = c.quote(m["pool_id"], numeraire, per_pool)
                except CommandError:
                    continue
                if not q["capacity_ok"]:
                    continue
                min_out = int(q["expected_amount_out_raw"]) * (10_000 - MIN_OUT_TOLERANCE_BPS) // 10_000
                intent += 1
                env = c.submit(pool_id=m["pool_id"], asset_in=numeraire, asset_out=m["base_asset"], amount_in_raw=per_pool, min_amount_out_raw=min_out, deadline_ms=c.clock_ms + 10 * 60_000, idempotency_key=f"basket_buy_{intent}", quote_id=q["quote_id"])
                if env["status"] == "ok":
                    cash -= per_pool + gas
        else:
            pf = c.portfolio()
            pools_by_base = {m["base_asset"]: m for m in c.all_markets(execution_supported_only=True)}
            for h in pf["valuation"]["holdings"]:
                if h["class"] != "priced_liquidatable":
                    continue  # no-route or unpriced inventory stays inventory
                m = pools_by_base.get(h["asset_id"])
                if not m:
                    continue
                qty = int(h["quantity_raw"])
                try:
                    q = c.quote(m["pool_id"], h["asset_id"], qty)
                except CommandError:
                    continue
                if not q["capacity_ok"]:
                    # Sell what the capacity profile allows instead of bypassing it with splits.
                    continue
                min_out = int(q["expected_amount_out_raw"]) * (10_000 - MIN_OUT_TOLERANCE_BPS) // 10_000
                intent += 1
                c.submit(pool_id=m["pool_id"], asset_in=h["asset_id"], asset_out=numeraire, amount_in_raw=qty, min_amount_out_raw=min_out, deadline_ms=c.clock_ms + 10 * 60_000, idempotency_key=f"basket_sell_{intent}", quote_id=q["quote_id"])
        # Let pending orders resolve before the next slot.
        c.advance_next(15 * 60_000)
    while True:
        adv = c.advance(min(c.clock_ms + 6 * HOUR, duration))
        if adv["episode_ended"]:
            break
    result = c.finish()
    print("scheduled_basket finished", result["clock_ms"], "valuation_complete", result["valuation_complete"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
