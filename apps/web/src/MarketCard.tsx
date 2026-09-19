import type { Ecosystem, MarketBaseline, MarketBasket } from "./api";
import { Card } from "./ui";

function pct(v: string | null | undefined, signed = true): string {
  if (v === null || v === undefined || v === "") return "n/a";
  const n = Number(v) * 100;
  if (!Number.isFinite(n)) return String(v);
  return `${signed && n > 0 ? "+" : ""}${n.toFixed(n === Math.round(n) ? 0 : 1)}%`;
}

function Basket({ title, b, stake }: { title: string; b: MarketBasket | undefined; stake: string }) {
  if (!b || !b.pools_priced) {
    return (
      <div>
        <h3>{title}</h3>
        <p className="muted small">No pool in this group traded during the day, so there is nothing to value.</p>
      </div>
    );
  }
  return (
    <div>
      <h3>{title}</h3>
      <div className="metrics">
        <div className="metric">
          <span className="lbl">Stake {stake} in every pool, sold at the close</span>
          <span className="val">{pct(b.equal_weight_return)}</span>
          <span className="lbl">before gas</span>
        </div>
        <div className="metric">
          <span className="lbl">Median pool</span>
          <span className="val">{pct(b.median_return)}</span>
          <span className="lbl">half did worse, half better</span>
        </div>
        <div className="metric">
          <span className="lbl">Pools that ended up</span>
          <span className="val">{pct(b.share_up, false)}</span>
          <span className="lbl">of {b.pools_priced} that traded</span>
        </div>
        <div className="metric">
          <span className="lbl">Liquidity pulled</span>
          <span className="val">{pct(b.share_drained, false)}</span>
          <span className="lbl">worth nothing at the close</span>
        </div>
        <div className="metric">
          <span className="lbl">Best and worst pool</span>
          <span className="val">
            {pct(b.best_return)} / {pct(b.worst_return)}
          </span>
          <span className="lbl">one in ten did better than {pct(b.p90_return)}</span>
        </div>
      </div>
    </div>
  );
}

function EcosystemBlock({ eco }: { eco: Ecosystem }) {
  if (!eco.tokens.length) return null;
  return (
    <div>
      <h3>The large Base tokens against ETH</h3>
      <div className="metrics">
        <div className="metric">
          <span className="lbl">Weighted by pool depth</span>
          <span className="val">{pct(eco.depth_weighted_return_vs_eth)}</span>
          <span className="lbl">vs ETH, first to last block</span>
        </div>
        <div className="metric">
          <span className="lbl">Equal weight</span>
          <span className="val">{pct(eco.equal_weight_return_vs_eth)}</span>
          <span className="lbl">{eco.tokens.length} tokens</span>
        </div>
        <div className="metric">
          <span className="lbl">ETH itself in dollars</span>
          <span className="val">{pct(eco.eth_usd_return)}</span>
          <span className="lbl">holding ETH is 0% in ETH terms</span>
        </div>
      </div>
      <table style={{ marginTop: 8 }}>
        <thead>
          <tr>
            <th>Token</th>
            <th className="num">vs ETH</th>
          </tr>
        </thead>
        <tbody>
          {eco.tokens.map((t) => (
            <tr key={t.symbol}>
              <td>{t.symbol}</td>
              <td className="num">{pct(t.return_vs_eth)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {eco.tokens.length < eco.tokens_expected.length && <p className="muted small">Left out that day: {eco.tokens_expected.filter((s) => !eco.tokens.some((t) => t.symbol === s)).join(", ")}.</p>}
    </div>
  );
}

/** What the market did on a recorded day, as a naive reference an agent's result can be read against. Never a verdict. */
export function MarketCard({ market, title = "Market that day", compact = false }: { market: MarketBaseline | null | undefined; title?: string; compact?: boolean }) {
  if (!market) return null;
  const stake = market.stake ?? "0.01";
  return (
    <Card title={title} className="stack">
      <p className="small muted" style={{ margin: 0, maxWidth: "80ch" }}>
        Reference points, not strategies. Holding ETH and doing nothing returned {pct(market.numeraire_hold_return)} in ETH terms. Compare your final ETH return with these numbers, remembering that the launch basket pays no gas.
      </p>
      {market.ecosystem && <EcosystemBlock eco={market.ecosystem} />}
      {!market.ecosystem && <p className="muted small">The large-token basket has not been read for this day yet.</p>}
      <Basket title={`Every pool launched that day: ${market.rule}`} b={market.launches} stake={stake} />
      {!compact && <Basket title="Established pools (trading before the day)" b={market.established} stake={stake} />}
      {!compact && (market.caveats?.length > 0 || market.ecosystem?.caveats?.length) && (
        <details>
          <summary className="small">Why these are only references</summary>
          <ul className="plain small" style={{ paddingLeft: 18 }}>
            {[...(market.ecosystem?.caveats ?? []), ...(market.caveats ?? [])].map((c, i) => (
              <li key={i}>{c}</li>
            ))}
          </ul>
        </details>
      )}
    </Card>
  );
}
