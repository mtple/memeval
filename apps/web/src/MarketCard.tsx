import type { MarketBaseline } from "./api";
import { Card } from "./ui";

function pct(v: string | null | undefined, digits = 1): string {
  if (v === null || v === undefined || v === "") return "not read";
  const n = Number(v) * 100;
  if (!Number.isFinite(n)) return String(v);
  return `${n > 0 ? "+" : ""}${n.toFixed(digits)}%`;
}

/** The three market lines of a day, each a change in dollars over the day. */
export function marketLines(m: MarketBaseline | null | undefined) {
  const eco = m?.ecosystem ?? null;
  const t = eco?.base_tokens ?? null;
  const vsEth = t?.depth_weighted_return_vs_eth ?? null;
  const ethUsd = eco?.eth_usd_return ?? null;
  const baseUsd = vsEth !== null && ethUsd !== null ? String((1 + Number(vsEth)) * (1 + Number(ethUsd)) - 1) : null;
  return { baseUsd, vsEth, tokens: t?.tokens ?? null, median: t?.median_return_vs_eth ?? null, shareUp: t?.share_up ?? null, ethUsd, btcUsd: eco?.btc_usd_return ?? null, crypto: m?.crypto_market?.return ?? null, coins: m?.crypto_market?.coins?.length ?? null };
}

export function marketPct(v: string | null | undefined): string {
  return pct(v, 1);
}

/** What the market did on a recorded day: the Base ecosystem, ETH and the crypto market, side by side. Never a verdict. */
export function MarketCard({ market, title = "Market that day", compact = false }: { market: MarketBaseline | null | undefined; title?: string; compact?: boolean }) {
  if (!market) return null;
  const L = marketLines(market);
  const eco = market.ecosystem;
  const crypto = market.crypto_market;
  return (
    <Card title={title} className="stack">
      <div className="metrics">
        <div className="metric">
          <span className="lbl">Base ecosystem</span>
          <span className="val">{pct(L.baseUsd)}</span>
          <span className="lbl">{L.vsEth !== null ? `${pct(L.vsEth)} against ETH, ${L.tokens} tokens weighted by pool depth` : "every Base-native token with an ETH pool"}</span>
        </div>
        <div className="metric">
          <span className="lbl">ETH</span>
          <span className="val">{pct(L.ethUsd)}</span>
          <span className="lbl">in dollars; 0% is what results are scored against</span>
        </div>
        <div className="metric">
          <span className="lbl">Crypto market</span>
          <span className="val">{pct(L.crypto)}</span>
          <span className="lbl">{L.coins ? `market value of the ${L.coins} largest coins` : "the largest coins' market value"}</span>
        </div>
      </div>
      {!compact && eco && eco.base_tokens?.tokens > 0 && (
        <p className="small muted" style={{ margin: 0, maxWidth: "80ch" }}>
          Inside the Base line: the median token moved {pct(L.median)} against ETH and {pct(L.shareUp, 0).replace("+", "")} of the {L.tokens} tokens ended up. One in ten did better than {pct(eco.base_tokens.p90_return_vs_eth)}, one in ten worse than {pct(eco.base_tokens.p10_return_vs_eth)}.
          {L.btcUsd !== null && ` BTC moved ${pct(L.btcUsd)} in dollars.`}
        </p>
      )}
      {!compact && (eco?.caveats?.length || crypto?.caveats?.length) && (
        <details>
          <summary className="small">How these are measured</summary>
          <ul className="plain small" style={{ paddingLeft: 18 }}>
            {[...(eco?.caveats ?? []), ...(crypto?.caveats ?? []), ...(eco?.notes ?? [])].map((c, i) => (
              <li key={i}>{c}</li>
            ))}
          </ul>
        </details>
      )}
    </Card>
  );
}
