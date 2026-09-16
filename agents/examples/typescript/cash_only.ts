// Reference participant (TypeScript): cash-only. Never trades. Ordinary external client.
import { clientFromEnv } from "../../../sdk/typescript/src/index.ts";

const STEP_MS = 6 * 3_600_000;

async function main() {
  const c = clientFromEnv();
  const info = (await c.describe()) as any;
  const duration: number = info.episode.duration_ms;
  for (;;) {
    const adv = await c.advance(Math.min(c.clockMs + STEP_MS, duration));
    if (adv.episode_ended) break;
  }
  const pf = await c.portfolio();
  const result = await c.finish();
  console.log("cash_only(ts) finished at", result.clock_ms, "equity", pf.valuation.model_equity_raw);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
