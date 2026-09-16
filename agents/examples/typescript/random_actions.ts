// Reference participant (TypeScript): seeded random actions. Bounded valid actions from a seeded PRNG.
import { CommandError, clientFromEnv } from "../../../sdk/typescript/src/index.ts";

const MIN_WAIT = 60_000;
const MAX_WAIT = 4 * 3_600_000;

// Small deterministic PRNG (mulberry32) seeded from MARKET_REPLAY_AGENT_SEED.
function mulberry32(seedStr: string) {
  let h = 1779033703 ^ seedStr.length;
  for (let i = 0; i < seedStr.length; i++) {
    h = Math.imul(h ^ seedStr.charCodeAt(i), 3432918353);
    h = (h << 13) | (h >>> 19);
  }
  let a = h >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

async function main() {
  const rnd = mulberry32(process.env.MARKET_REPLAY_AGENT_SEED ?? "42");
  const randInt = (lo: number, hi: number) => lo + Math.floor(rnd() * (hi - lo + 1));
  const c = clientFromEnv();
  const info = (await c.describe()) as any;
  const duration: number = info.episode.duration_ms;
  const numeraire: string = info.numeraire.asset_id;
  const gas = BigInt(info.execution.gas_cost_raw);
  let intent = 0;
  let steps = 0;
  while (c.clockMs < duration && steps < 5000) {
    steps += 1;
    const r = rnd();
    const action = r < 0.5 ? "wait" : r < 0.7 ? "buy" : r < 0.9 ? "sell" : "look";
    if (action === "wait") {
      await c.advance(Math.min(c.clockMs + randInt(MIN_WAIT, MAX_WAIT), duration));
      continue;
    }
    const pools = await c.allMarkets({ execution_supported_only: true });
    if (pools.length === 0) {
      await c.advance(Math.min(c.clockMs + MAX_WAIT, duration));
      continue;
    }
    const m = pools[randInt(0, pools.length - 1)];
    if (action === "look") {
      try {
        await c.trades(m.pool_id, { limit: 20 });
        await c.candles(m.pool_id, 300_000, { start_ms: Math.max(-3_600_000, c.clockMs - 3_600_000) });
      } catch (e) {
        if (!(e instanceof CommandError)) throw e;
      }
      continue;
    }
    const pf = await c.portfolio();
    let assetIn: string, assetOut: string, amount: bigint;
    if (action === "buy") {
      const cash = BigInt(pf.balances.find((b: any) => b.asset_id === numeraire).available_raw);
      const maxSpend = cash / 50n;
      if (maxSpend <= 0n) continue;
      amount = BigInt(randInt(1, Number(maxSpend > 1_000_000_000n ? 1_000_000_000n : maxSpend)));
      if (amount + gas > cash) continue;
      assetIn = numeraire;
      assetOut = m.base_asset;
    } else {
      const h = pf.valuation.holdings.find((x: any) => x.class === "priced_liquidatable" && x.asset_id === m.base_asset);
      if (!h) continue;
      const qty = BigInt(h.quantity_raw);
      // random fraction of the holding in 1/1000 steps
      amount = (qty * BigInt(randInt(1, 1000))) / 1000n;
      if (amount <= 0n) continue;
      assetIn = m.base_asset;
      assetOut = numeraire;
    }
    let q: any;
    try {
      q = await c.quote(m.pool_id, assetIn, amount);
    } catch (e) {
      if (e instanceof CommandError) continue;
      throw e;
    }
    if (!q.capacity_ok) continue;
    const tol = [0n, 50n, 100n, 300n][randInt(0, 3)];
    const minOut = (BigInt(q.expected_amount_out_raw) * (10_000n - tol)) / 10_000n;
    intent += 1;
    await c.submit({
      poolId: m.pool_id,
      assetIn,
      assetOut,
      amountInRaw: amount,
      minAmountOutRaw: minOut,
      deadlineMs: c.clockMs + randInt(5_000, 600_000),
      idempotencyKey: `rand_${intent}`,
    });
    await c.advanceNext(randInt(5_000, 120_000));
  }
  while (c.clockMs < duration) await c.advance(Math.min(c.clockMs + MAX_WAIT, duration));
  const result = await c.finish();
  console.log("random_actions(ts) finished", result.clock_ms, "intents", intent);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
