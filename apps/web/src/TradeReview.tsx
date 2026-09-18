import { useState } from "react";
import { get, type Observed } from "./api";
import { CandleChart } from "./charts";
import { fmtRaw, fmtRel } from "./format";
import { Card, ErrorState, Loading, useLoad } from "./ui";

type Token = { asset_id: string; decimals: number; eth_spent_raw: string; eth_recovered_raw: string; gas_raw: string; net_cash_raw: string; remaining_raw: string; pending_raw: string; buys: number; sells: number; first_buy_ms: number | null; last_sell_ms: number | null; average_hold_ms: number | null };
type Event = { order_id: string; pool_id: string; asset_id: string; side: string; state: string; submitted_ms: number; fill_time_ms: number | null; confirm_time_ms: number | null; quantity_raw: string | null; cash_raw: string | null; gas_raw: string; price: string | null; reason: string | null };
type Review = { clock_ms: number; numeraire: string; numeraire_decimals: number; tokens: Token[]; events: Event[]; rejected_calls: { time_ms: number; error_code: string | null }[]; note: string };

export function TradeReview({ runId }: { runId: string }) {
  const [open, setOpen] = useState(false);
  return <Card title="Trade review">
    <p>See entries, exits, missed exits, and each token's contribution to the final cash balance.</p>
    {open ? <LoadedReview runId={runId} /> : <button type="button" className="btn" onClick={() => setOpen(true)}>Load trade review</button>}
  </Card>;
}

function LoadedReview({ runId }: { runId: string }) {
  const review = useLoad(() => get<Review>(`/runs/${runId}/trade-review`), [runId]);
  const [chosen, setChosen] = useState("");
  const [page, setPage] = useState(0);
  if (review.error) return <ErrorState error={review.error} retry={review.reload} />;
  if (!review.data) return <Loading what="trade review" />;
  const r = review.data;
  if (!r.events.length) return <p>No accepted orders. {r.rejected_calls.length} submit calls returned errors{r.rejected_calls.length ? `: ${[...new Set(r.rejected_calls.map(c => c.error_code))].join(", ")}` : ". The agent did not submit an order"}.</p>;
  const selected = r.tokens.find(t => t.asset_id === chosen) ?? r.tokens[0]!;
  const events = r.events.filter(e => e.asset_id === selected.asset_id);
  const cash = (v: string | null) => `${fmtRaw(v, r.numeraire_decimals)} ${r.numeraire}`;
  return <div className="stack">
    <p className="small muted">{r.note}</p>
    {r.rejected_calls.length > 0 && <p className="notice">{r.rejected_calls.length} submit calls returned errors: {[...new Set(r.rejected_calls.map(c => c.error_code))].join(", ")}. These calls are separate from the recorded orders below.</p>}
    <div className="table-wrap"><table><caption>Token contributions, highest to lowest. A negative cash contribution can include money still invested.</caption><thead><tr><th>Token</th><th>Spent</th><th>Recovered</th><th>Gas</th><th>Net cash contribution</th><th>Unsold tokens</th></tr></thead><tbody>
      {r.tokens.map(t => <tr key={t.asset_id}><td><button type="button" className="btn btn-small" aria-pressed={selected.asset_id === t.asset_id} onClick={() => { setChosen(t.asset_id); setPage(0); }}>{t.asset_id}</button></td><td>{cash(t.eth_spent_raw)}</td><td>{cash(t.eth_recovered_raw)}</td><td>{cash(t.gas_raw)}</td><td>{cash(t.net_cash_raw)}</td><td>{fmtRaw(t.remaining_raw, t.decimals)}</td></tr>)}
    </tbody></table></div>
    <h3>{selected.asset_id}</h3>
    <p>{selected.buys} confirmed buys · {selected.sells} confirmed sells · Average holding time of sold tokens: {selected.average_hold_ms === null ? "no matched sales" : fmtRel(selected.average_hold_ms)}.</p>
    {BigInt(selected.remaining_raw) > 0n && <p className="notice">{fmtRaw(selected.remaining_raw, selected.decimals)} tokens remained unsold, including {fmtRaw(selected.pending_raw, selected.decimals)} pending confirmation. These tokens do not count toward final ETH/cash return.</p>}
    <PoolReview key={selected.asset_id} runId={runId} events={events} clockMs={r.clock_ms} unit={r.numeraire} />
    <h3>Order timeline</h3>
    <div className="table-wrap"><table><thead><tr><th>Submitted / filled</th><th>Action</th><th>Status</th><th>Tokens filled</th><th>ETH/cash exchanged</th><th>Gas</th></tr></thead><tbody>
      {events.slice(page * 100, (page + 1) * 100).map(e => <tr key={e.order_id}><td>{fmtRel(e.submitted_ms)} / {e.fill_time_ms === null ? "not filled" : fmtRel(e.fill_time_ms)}</td><td>{e.side}</td><td>{e.state}{e.reason ? `: ${e.reason}` : ""}</td><td>{e.quantity_raw === null ? "—" : fmtRaw(e.quantity_raw, selected.decimals)}</td><td>{e.cash_raw === null ? "—" : cash(e.cash_raw)}</td><td>{cash(e.gas_raw)}</td></tr>)}
    </tbody></table></div>
    <div className="row"><button className="btn" type="button" disabled={page === 0} onClick={() => setPage(p => p - 1)}>Previous</button><span>Orders {page * 100 + 1}–{Math.min((page + 1) * 100, events.length)} of {events.length}</span><button className="btn" type="button" disabled={(page + 1) * 100 >= events.length} onClick={() => setPage(p => p + 1)}>Next</button></div>
  </div>;
}

function PoolReview({ runId, events, clockMs, unit }: { runId: string; events: Event[]; clockMs: number; unit: string }) {
  const pools = [...new Set(events.map(e => e.pool_id))];
  const [pool, setPool] = useState(pools[0]!);
  // The existing read-only observations endpoint accepts arbitrary aggregation intervals.
  // Cover the full episode, rather than its default trailing 400 minutes.
  const interval = Math.max(60_000, Math.ceil(clockMs / 300 / 60_000) * 60_000);
  const observed = useLoad(() => get<Observed>(`/runs/${runId}/observed?pool_id=${encodeURIComponent(pool)}&interval_ms=${interval}`), [runId, pool, interval]);
  const markers = events.filter(e => e.pool_id === pool && e.fill_time_ms !== null && e.price !== null).map(e => ({ time_ms: e.fill_time_ms!, price: e.price!, label: `${e.side === "buy" ? "B" : "S"} · ${e.state}`, side: e.side }));
  return <div>
    <label>Pool <select value={pool} onChange={e => setPool(e.target.value)}>{pools.map(p => <option key={p} value={p}>{p}</option>)}</select></label>
    <p className="small muted">{unit} per token. B = buy, S = sell. Markers show fill prices; hover for time and status. Candles summarize the observed simulated market, including activity after an exit. Gaps are missing observations.</p>
    {observed.error && <ErrorState error={observed.error} retry={observed.reload} />}
    {observed.loading ? <Loading what="price chart" /> : observed.data?.series ? <CandleChart bars={observed.data.series.items} gaps={observed.data.series.gaps} clockMs={observed.data.clock_ms} markers={markers} /> : <p>No observed price history for this pool.</p>}
  </div>;
}
