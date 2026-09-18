import type { MarketBaseline, MarketBasket } from "./api";
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

/** What the market did on a recorded day, as a naive reference an agent's result can be read against. Never a verdict. */
export function MarketCard({ market, title = "Market that day", compact = false }: { market: MarketBaseline | null | undefined; title?: string; compact?: boolean }) {
  if (!market) return null;
  const stake = market.stake ?? "0.01";
  return (
    <Card title={title} className="stack">
      <p className="small muted" style={{ margin: 0, maxWidth: "80ch" }}>
        A reference point, not a strategy: {market.rule} Holding ETH and doing nothing returned {pct(market.numeraire_hold_return)}. Compare your final ETH return with these numbers, remembering that they pay no gas.
      </p>
      <Basket title="Every pool launched that day" b={market.launches} stake={stake} />
      {!compact && <Basket title="Established pools (trading before the day)" b={market.established} stake={stake} />}
      {!compact && market.caveats?.length > 0 && (
        <details>
          <summary className="small">Why this is only a reference</summary>
          <ul className="plain small" style={{ paddingLeft: 18 }}>
            {market.caveats.map((c, i) => (
              <li key={i}>{c}</li>
            ))}
          </ul>
        </details>
      )}
    </Card>
  );
}
