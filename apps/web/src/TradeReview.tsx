import { useState } from "react";
import { get, type Bar, type Observed } from "./api";
import { fmtClock, fmtRaw, unitLabel } from "./format";
import { TradePriceChart } from "./TradePriceChart";
import { Card, ErrorState, Loading, useLoad } from "./ui";

type Token = { asset_id: string; decimals: number; eth_spent_raw: string; eth_recovered_raw: string; gas_raw: string; net_cash_raw: string; remaining_raw: string; pending_raw: string; buys: number; sells: number; first_buy_ms: number | null; last_sell_ms: number | null; average_hold_ms: number | null };
type Event = { order_id: string; pool_id: string; asset_id: string; side: string; state: string; submitted_ms: number; fill_time_ms: number | null; confirm_time_ms: number | null; quantity_raw: string | null; cash_raw: string | null; gas_raw: string; price: string | null; reason: string | null };
type Series = { pool_id: string; interval_ms: number; items: Bar[]; gaps: { start_ms: number; end_ms: number; reason: string }[]; as_of_ms: number };
type Review = { clock_ms: number; numeraire: string; numeraire_decimals: number; tokens: Token[]; events: Event[]; rejected_calls: { time_ms: number; error_code: string | null }[]; note: string; series?: Record<string, Series> };

export function TradeReview({ runId }: { runId: string }) {
  const review = useLoad(() => get<Review>(`/runs/${runId}/trade-review`), [runId]);
  return <Card title="Which trades changed the balance?">
    {review.error && <ErrorState error={review.error} retry={review.reload} />}
    {!review.data && !review.error && <Loading what="trades" />}
    {review.data && <TradeBreakdown r={review.data} runId={runId} />}
  </Card>;
}

function duration(ms: number) {
  const minutes = Math.floor(ms / 60_000), seconds = Math.floor(ms / 1000) % 60;
  return `${minutes >= 60 ? `${Math.floor(minutes/60)}h ` : ""}${minutes % 60}m ${seconds}s`;
}
const signClass = (raw: string) => BigInt(raw) > 0n ? "up" : BigInt(raw) < 0n ? "down" : "";

export function TradeBreakdown({ r, runId }: { r: Review; runId: string }) {
  const [chosen, setChosen] = useState("");
  const [page, setPage] = useState(0);
  const unit = unitLabel(r.numeraire);
  if (!r.tokens.length) return <p>No token trades were recorded. {r.rejected_calls.length ? `${r.rejected_calls.length} order requests were refused.` : "The agent did not buy or sell a token."}</p>;
  const selected = r.tokens.find(t => t.asset_id === chosen) ?? r.tokens[0]!;
  const events = r.events.filter(e => e.asset_id === selected.asset_id).sort((a,b) => (a.fill_time_ms ?? a.submitted_ms) - (b.fill_time_ms ?? b.submitted_ms));
  const cash = (raw: string) => `${fmtRaw(raw, r.numeraire_decimals, 9)} ${unit}`;
  const signedCash = (raw: string) => `${BigInt(raw) > 0n ? "+" : ""}${cash(raw)}`;
  const name = (t: Token) => `Token ${r.tokens.indexOf(t) + 1}`;
  const unsold = BigInt(selected.remaining_raw) !== 0n || BigInt(selected.pending_raw) !== 0n;
  const loss = r.tokens.reduce((a,b) => BigInt(a.net_cash_raw) < BigInt(b.net_cash_raw) ? a : b);
  const wins = r.tokens.filter(t => BigInt(t.net_cash_raw) > 0n).length;
  const losses = r.tokens.filter(t => BigInt(t.net_cash_raw) < 0n).length;
  const countBySide: Record<string, number> = {};
  const labels = new Map(events.map(e => {const side = e.side === "buy" ? "Buy" : "Sell"; countBySide[side] = (countBySide[side] ?? 0) + 1; return [e.order_id, `${side} ${countBySide[side]}`];}));
  return <div className="stack trade-breakdown">
    <p className="trade-overview">{wins} token{wins === 1 ? "" : "s"} added cash; {losses} reduced it.
      {BigInt(loss.net_cash_raw) < 0n && <> The largest reduction came from <button className="rowbtn" onClick={() => {setChosen(loss.asset_id); setPage(0);}}>{name(loss)}</button> ({signedCash(loss.net_cash_raw)}).</>}
    </p>
    <div className="token-picker" aria-label="Choose a token to review">
      {r.tokens.map(t => <button key={t.asset_id} className="token-choice" aria-pressed={selected.asset_id === t.asset_id} onClick={() => {setChosen(t.asset_id); setPage(0);}}>
        <span className="token-choice-title">{name(t)} <span>{selected.asset_id === t.asset_id ? "Viewing" : "View trades →"}</span></span>
        <strong className={signClass(t.net_cash_raw)}>{signedCash(t.net_cash_raw)}</strong>
        <span>{t.buys} {t.buys === 1 ? "buy" : "buys"} · {t.sells} {t.sells === 1 ? "sell" : "sells"} · {BigInt(t.remaining_raw) || BigInt(t.pending_raw) ? "Tokens remain" : "No tokens left"}</span>
      </button>)}
    </div>
    <p className="small muted">Select a token above. Names are anonymized. Cash change includes confirmed trades and gas; money still invested reduces cash without necessarily being a realized loss.</p>
    {r.rejected_calls.length > 0 && <p className="notice">{r.rejected_calls.length} order requests were refused: {[...new Set(r.rejected_calls.map(c => c.error_code))].join(", ")}. They are separate from accepted orders.</p>}
    <div className="token-outcome">
      <div><span className="lbl">{name(selected)} · {unsold ? "Cash change so far" : "Profit / loss after costs"}</span><strong className={signClass(selected.net_cash_raw)}>{signedCash(selected.net_cash_raw)}</strong>
        <p>{selected.buys} confirmed {selected.buys === 1 ? "buy" : "buys"}, {selected.sells} confirmed {selected.sells === 1 ? "sell" : "sells"}.{selected.average_hold_ms !== null && ` Sold tokens were held for ${duration(selected.average_hold_ms)} on average.`}</p>
      </div>
      <div className="cash-calculation" aria-label={`${name(selected)} cash calculation`}>
        <div><span>Received from sells</span><b>{cash(selected.eth_recovered_raw)}</b></div>
        <div><span>− Paid for buys</span><b>{cash(selected.eth_spent_raw)}</b></div>
        <div><span>− Gas for orders</span><b>{cash(selected.gas_raw)}</b></div>
        <div className="calculation-total"><span>= Cash change</span><b className={signClass(selected.net_cash_raw)}>{signedCash(selected.net_cash_raw)}</b></div>
      </div>
    </div>
    <p className="small muted">Pool fees are already reflected in the amounts paid and received. Gas is subtracted separately.</p>
    {unsold && <p className="notice">{fmtRaw(selected.remaining_raw, selected.decimals)} tokens remain, including {fmtRaw(selected.pending_raw, selected.decimals)} pending confirmation. Unsold tokens are excluded from final cash return.</p>}
    <PoolReview key={selected.asset_id} runId={runId} events={events} labels={labels} clockMs={r.clock_ms} unit={unit} decimals={r.numeraire_decimals} token={name(selected)} series={r.series} />
    <details className="more order-details"><summary>Order-by-order details · {events.length} orders for {name(selected)}</summary>
      <p className="small muted">Times are elapsed since the replay started. Amounts below are actual simulated fills.</p>
      <div className="table-wrap"><table><thead><tr><th>Time</th><th>Order</th><th>Result</th><th className="num">Tokens filled</th><th className="num">Cash paid / received</th><th className="num">Gas</th></tr></thead><tbody>
        {events.slice(page * 100, (page + 1) * 100).map(e => <tr key={e.order_id}><td>{fmtClock(e.fill_time_ms ?? e.submitted_ms)}</td><td>{labels.get(e.order_id)}</td><td>{outcomeWords(e)}</td><td className="num">{e.quantity_raw === null ? "—" : fmtRaw(e.quantity_raw, selected.decimals)}</td><td className="num">{e.cash_raw === null ? "—" : `${e.side === "buy" ? "Paid" : "Received"} ${cash(e.cash_raw)}`}</td><td className="num">{cash(e.gas_raw)}</td></tr>)}
      </tbody></table></div>
      {events.length > 100 && <div className="row"><button className="btn" disabled={page === 0} onClick={() => setPage(p => p - 1)}>Previous</button><span>{page*100+1}–{Math.min((page+1)*100,events.length)} of {events.length}</span><button className="btn" disabled={(page+1)*100 >= events.length} onClick={() => setPage(p => p + 1)}>Next</button></div>}
    </details>
  </div>;
}

function outcomeWords(e: Event): string {
  if (e.state === "confirmed") return "Filled and confirmed";
  if (e.state === "filled" || e.state === "filled_pending_confirmation") return "Filled; confirmation pending";
  return e.state.replace(/_/g, " ") + (e.reason ? `: ${e.reason}` : "");
}

function PoolReview({runId, events, labels, clockMs, unit, decimals, token, series}: {runId: string; events: Event[]; labels: Map<string,string>; clockMs: number; unit: string; decimals: number; token: string; series?: Record<string,Series>}) {
  const pools = [...new Set(events.map(e => e.pool_id))];
  const [pool, setPool] = useState(pools[0]!);
  const stored = series?.[pool] ?? null;
  const interval = Math.max(60_000, Math.ceil(clockMs / 300 / 60_000) * 60_000);
  const observed = useLoad(() => stored ? Promise.resolve(null) : get<Observed>(`/runs/${runId}/observed?pool_id=${encodeURIComponent(pool)}&interval_ms=${interval}`), [runId,pool,interval,stored !== null]);
  const data = stored ?? observed.data?.series ?? null;
  const fills = events.filter(e => e.pool_id === pool && e.fill_time_ms !== null && e.price !== null);
  const markers = fills.map(e => ({id:e.order_id,time_ms:e.fill_time_ms!,price:e.price!,side:e.side,label:labels.get(e.order_id)!,
    detail:`${e.side === "buy" ? "Paid" : "Received"} ${fmtRaw(e.cash_raw,decimals,9)} ${unit} · Gas ${fmtRaw(e.gas_raw,decimals,9)} ${unit} · ${outcomeWords(e)}`}));
  return <div>
    {pools.length > 1 && <label>Trading pool <select value={pool} onChange={e => setPool(e.target.value)}>{pools.map((p,i) => <option key={p} value={p}>Pool {i+1}</option>)}</select></label>}
    {observed.error && <ErrorState error={observed.error} retry={observed.reload} />}
    {!data && !observed.error ? <Loading what="price history" /> : <TradePriceChart key={pool} bars={data?.items ?? []} gaps={data?.gaps ?? []} clockMs={clockMs} markers={markers} token={token} />}
  </div>;
}
