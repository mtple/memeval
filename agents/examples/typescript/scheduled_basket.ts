// Reference participant (TypeScript): scheduled basket. Same predeclared schedule as the Python example.
import { CommandError, clientFromEnv } from "../../../sdk/typescript/src/index.ts";

const HOUR = 3_600_000;
const BUY_SLOTS_H = [1, 25, 49, 73, 97];
const SELL_SLOT_H = 150;
const SPEND_BPS = 800n;
const TOL_BPS = 100n;

async function main() {
  const c = clientFromEnv();
  const info = (await c.describe()) as any;
  const duration: number = info.episode.duration_ms;
  const numeraire: string = info.numeraire.asset_id;
  const bankroll = BigInt(info.bankroll_raw);
  const gas = BigInt(info.execution.gas_cost_raw);
  let schedule: Array<[number, "buy" | "sell"]> = [
    ...BUY_SLOTS_H.map((h) => [h * HOUR, "buy"] as [number, "buy"]),
    [SELL_SLOT_H * HOUR, "sell"] as [number, "sell"],
  ].filter(([t]) => t < duration).sort((a, b) => a[0] - b[0]);
  if (schedule.length === 0 || schedule[schedule.length - 1][1] !== "sell") {
    const n = BUY_SLOTS_H.length;
    schedule = [
      ...Array.from({ length: n }, (_, i) => [Math.floor(((i + 1) * duration) / (n + 3)), "buy"] as [number, "buy"]),
      [Math.floor((duration * 9) / 10), "sell"] as [number, "sell"],
    ];
  }
  let intent = 0;
  for (const [slotMs, kind] of schedule) {
    if (c.clockMs < slotMs) await c.advance(slotMs);
    if (kind === "buy") {
      const pools = (await c.allMarkets({ execution_supported_only: true })).sort((a, b) => (a.pool_id < b.pool_id ? -1 : 1));
      if (pools.length === 0) continue;
      const perPool = (bankroll * SPEND_BPS) / 10_000n / BigInt(pools.length);
      const pf = await c.portfolio();
      let cash = BigInt(pf.balances.find((b: any) => b.asset_id === numeraire).available_raw);
      for (const m of pools) {
        if (perPool <= 0n || cash < perPool + gas) break;
        let q: any;
        try {
          q = await c.quote(m.pool_id, numeraire, perPool);
        } catch (e) {
          if (e instanceof CommandError) continue;
          throw e;
        }
        if (!q.capacity_ok) continue;
        const minOut = (BigInt(q.expected_amount_out_raw) * (10_000n - TOL_BPS)) / 10_000n;
        intent += 1;
        const env = await c.submit({
          poolId: m.pool_id,
          assetIn: numeraire,
          assetOut: m.base_asset,
          amountInRaw: perPool,
          minAmountOutRaw: minOut,
          deadlineMs: c.clockMs + 10 * 60_000,
          idempotencyKey: `basket_buy_${intent}`,
          quoteId: q.quote_id,
        });
        if (env.status === "ok") cash -= perPool + gas;
      }
    } else {
      const pf = await c.portfolio();
      const pools = await c.allMarkets({ execution_supported_only: true });
      const byBase = new Map(pools.map((m) => [m.base_asset, m]));
      for (const h of pf.valuation.holdings) {
        if (h.class !== "priced_liquidatable") continue;
        const m = byBase.get(h.asset_id);
        if (!m) continue;
        const qty = BigInt(h.quantity_raw);
        let q: any;
        try {
          q = await c.quote(m.pool_id, h.asset_id, qty);
        } catch (e) {
          if (e instanceof CommandError) continue;
          throw e;
        }
        if (!q.capacity_ok) continue;
        const minOut = (BigInt(q.expected_amount_out_raw) * (10_000n - TOL_BPS)) / 10_000n;
        intent += 1;
        await c.submit({
          poolId: m.pool_id,
          assetIn: h.asset_id,
          assetOut: numeraire,
          amountInRaw: qty,
          minAmountOutRaw: minOut,
          deadlineMs: c.clockMs + 10 * 60_000,
          idempotencyKey: `basket_sell_${intent}`,
          quoteId: q.quote_id,
        });
      }
    }
    await c.advanceNext(15 * 60_000);
  }
  for (;;) {
    const adv = await c.advance(Math.min(c.clockMs + 6 * HOUR, duration));
    if (adv.episode_ended) break;
  }
  const result = await c.finish();
  console.log("scheduled_basket(ts) finished", result.clock_ms, "valuation_complete", result.valuation_complete);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
