import { useState } from "react";
import { get, type Bar, type Observed } from "./api";
import { CandleChart, priceScale } from "./charts";
import { fmtClock, fmtRaw, fmtRel, unitLabel } from "./format";
import { Card, ErrorState, Loading, useLoad } from "./ui";

type Token = { asset_id: string; decimals: number; eth_spent_raw: string; eth_recovered_raw: string; gas_raw: string; net_cash_raw: string; remaining_raw: string; pending_raw: string; buys: number; sells: number; first_buy_ms: number | null; last_sell_ms: number | null; average_hold_ms: number | null };
type Event = { order_id: string; pool_id: string; asset_id: string; side: string; state: string; submitted_ms: number; fill_time_ms: number | null; confirm_time_ms: number | null; quantity_raw: string | null; cash_raw: string | null; gas_raw: string; price: string | null; reason: string | null };
type Series = { pool_id: string; interval_ms: number; items: Bar[]; gaps: { start_ms: number; end_ms: number; reason: string }[]; as_of_ms: number };
type Review = { clock_ms: number; numeraire: string; numeraire_decimals: number; tokens: Token[]; events: Event[]; rejected_calls: { time_ms: number; error_code: string | null }[]; note: string; series?: Record<string, Series> };

/** Every trade the agent made, token by token, with the price it traded against. Shown without asking. */
export function TradeReview({ runId }: { runId: string }) {
  const review = useLoad(() => get<Review>(`/runs/${runId}/trade-review`), [runId]);
  const [chosen, setChosen] = useState("");
  const [page, setPage] = useState(0);
  return (
    <Card title="The trades">
      {review.error && <ErrorState error={review.error} retry={review.reload} />}
      {!review.data && !review.error && <Loading what="trades" />}
      {review.data && <Loaded r={review.data} runId={runId} chosen={chosen} setChosen={setChosen} page={page} setPage={setPage} />}
    </Card>
  );
}

function Loaded({ r, runId, chosen, setChosen, page, setPage }: { r: Review; runId: string; chosen: string; setChosen: (v: string) => void; page: number; setPage: (f: (p: number) => number) => void }) {
  const unit = unitLabel(r.numeraire);
  if (!r.events.length) {
    return (
      <p>
        The agent placed no orders that were accepted.
        {r.rejected_calls.length ? ` ${r.rejected_calls.length} order calls were refused: ${[...new Set(r.rejected_calls.map((c) => c.error_code))].join(", ")}.` : " It never submitted an order."}
      </p>
    );
  }
  const selected = r.tokens.find((t) => t.asset_id === chosen) ?? r.tokens[0]!;
  const events = r.events.filter((e) => e.asset_id === selected.asset_id);
  const cash = (v: string | null) => `${fmtRaw(v, r.numeraire_decimals)} ${unit}`;
  const tokenName = (_id: string, i: number) => `Token ${i + 1}`;
  const idx = r.tokens.findIndex((t) => t.asset_id === selected.asset_id);
  return (
    <div className="stack">
      <p className="small muted" style={{ margin: 0 }}>
        Tokens inside a session carry generic names so an agent cannot look them up, so they are numbered here. Money in and out is in {unit}; unsold tokens count for nothing.
      </p>
      {r.rejected_calls.length > 0 && <p className="notice">{r.rejected_calls.length} order calls were refused ({[...new Set(r.rejected_calls.map((c) => c.error_code))].join(", ")}). These are separate from the orders below.</p>}
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Token</th>
              <th className="num">Buys / sells</th>
              <th className="num">Spent</th>
              <th className="num">Got back</th>
              <th className="num">Gas</th>
              <th className="num">Net</th>
              <th className="num">Left unsold</th>
            </tr>
          </thead>
          <tbody>
            {r.tokens.map((t, i) => {
              const net = BigInt(t.net_cash_raw);
              return (
                <tr key={t.asset_id} className={selected.asset_id === t.asset_id ? "selected" : ""}>
                  <td>
                    <button type="button" className="rowbtn" aria-pressed={selected.asset_id === t.asset_id} onClick={() => { setChosen(t.asset_id); setPage(() => 0); }}>
                      {tokenName(t.asset_id, i)}
                    </button>
                  </td>
                  <td className="num">{t.buys} / {t.sells}</td>
                  <td className="num">{cash(t.eth_spent_raw)}</td>
                  <td className="num">{cash(t.eth_recovered_raw)}</td>
                  <td className="num">{cash(t.gas_raw)}</td>
                  <td className={`num ret ${net > 0n ? "up" : net < 0n ? "down" : ""}`}>{cash(t.net_cash_raw)}</td>
                  <td className="num">{BigInt(t.remaining_raw) > 0n ? fmtRaw(t.remaining_raw, t.decimals) : "none"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <h3>{tokenName(selected.asset_id, idx)}</h3>
      <p style={{ marginTop: 0 }}>
        {selected.buys} confirmed buy{selected.buys === 1 ? "" : "s"} and {selected.sells} confirmed sell{selected.sells === 1 ? "" : "s"}.
        {selected.average_hold_ms !== null && ` Held for ${fmtRel(selected.average_hold_ms).replace(/^0d /, "")} on average.`}
        {BigInt(selected.remaining_raw) > 0n && ` ${fmtRaw(selected.remaining_raw, selected.decimals)} tokens were never sold and count for nothing.`}
      </p>
      <PoolReview key={selected.asset_id} runId={runId} events={events} clockMs={r.clock_ms} unit={unit} series={r.series} />
      <h3>Every order for this token</h3>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>When</th>
              <th>Action</th>
              <th>Outcome</th>
              <th className="num">Tokens</th>
              <th className="num">{unit}</th>
              <th className="num">Gas</th>
            </tr>
          </thead>
          <tbody>
            {events.slice(page * 100, (page + 1) * 100).map((e) => (
              <tr key={e.order_id}>
                <td className="small">{fmtClock(e.fill_time_ms ?? e.submitted_ms)}</td>
                <td>{e.side === "buy" ? "Bought" : "Sold"}</td>
                <td className="small">{outcomeWords(e)}</td>
                <td className="num small">{e.quantity_raw === null ? "—" : fmtRaw(e.quantity_raw, selected.decimals)}</td>
                <td className="num small">{e.cash_raw === null ? "—" : fmtRaw(e.cash_raw, r.numeraire_decimals)}</td>
                <td className="num small">{fmtRaw(e.gas_raw, r.numeraire_decimals)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {events.length > 100 && (
        <div className="row">
          <button className="btn" type="button" disabled={page === 0} onClick={() => setPage((p) => p - 1)}>Previous</button>
          <span>Orders {page * 100 + 1} to {Math.min((page + 1) * 100, events.length)} of {events.length}</span>
          <button className="btn" type="button" disabled={(page + 1) * 100 >= events.length} onClick={() => setPage((p) => p + 1)}>Next</button>
        </div>
      )}
    </div>
  );
}

function outcomeWords(e: Event): string {
  switch (e.state) {
    case "confirmed":
      return "filled and confirmed";
    case "filled":
      return "filled, confirmation pending";
    case "reverted":
      return `reverted${e.reason ? `: ${e.reason}` : ""}`;
    case "expired":
      return "expired before it could fill";
    case "rejected":
      return `refused${e.reason ? `: ${e.reason}` : ""}`;
    default:
      return e.reason ? `${e.state}: ${e.reason}` : e.state;
  }
}

function PoolReview({ runId, events, clockMs, unit, series }: { runId: string; events: Event[]; clockMs: number; unit: string; series?: Record<string, Series> }) {
  const pools = [...new Set(events.map((e) => e.pool_id))];
  const [pool, setPool] = useState(pools[0]!);
  const stored = series?.[pool] ?? null;
  const interval = Math.max(60_000, Math.ceil(clockMs / 300 / 60_000) * 60_000);
  const observed = useLoad(() => (stored ? Promise.resolve(null) : get<Observed>(`/runs/${runId}/observed?pool_id=${encodeURIComponent(pool)}&interval_ms=${interval}`)), [runId, pool, interval, stored !== null]);
  const data = stored ?? observed.data?.series ?? null;
  const fills = events.filter((e) => e.pool_id === pool && e.fill_time_ms !== null && e.price !== null);
  const markers = fills.map((e) => ({ time_ms: e.fill_time_ms!, price: e.price!, label: e.side === "buy" ? "bought" : "sold", side: e.side }));
  const scale = data ? priceScale(data.items.map((b) => b.close).filter((v): v is string => v !== null).concat(fills.map((e) => e.price!))) : null;
  const buys = fills.filter((e) => e.side === "buy");
  const sells = fills.filter((e) => e.side === "sell");
  const firstBuy = buys[0];
  const lastSell = sells[sells.length - 1];
  const move = firstBuy && lastSell ? (Number(lastSell.price) / Number(firstBuy.price) - 1) * 100 : null;
  return (
    <div>
      {pools.length > 1 && (
        <label>
          Pool{" "}
          <select value={pool} onChange={(e) => setPool(e.target.value)}>
            {pools.map((p, i) => (
              <option key={p} value={p}>Pool {i + 1}</option>
            ))}
          </select>
        </label>
      )}
      {firstBuy && (
        <p style={{ margin: "4px 0 8px" }}>
          {buys.length === 1 ? "Bought" : `First bought`} at {fmtClock(firstBuy.fill_time_ms!)} for {scale ? scale.fmt(firstBuy.price!) : firstBuy.price} {scale?.unitName ?? unit} per token
          {lastSell ? `, ${sells.length === 1 ? "sold" : "last sold"} at ${fmtClock(lastSell.fill_time_ms!)} for ${scale ? scale.fmt(lastSell.price!) : lastSell.price}: the price moved ${move! >= 0 ? "+" : ""}${move!.toFixed(1)}% between the two, before gas and fees.` : ", never sold."}
        </p>
      )}
      <p className="small muted">
        The price of this token in {scale?.unitName ?? unit} through the day{data ? `, one bar per ${Math.round(data.interval_ms / 60_000)} minutes` : ""}. A green bar means the price ended higher than it started in that span, red lower. B marks where the agent bought, S where it sold. Shaded areas are minutes with no observed trades.
        {scale && scale.unitName !== unit && ` 1 ${scale.unitName} = ${scale.oneIs} ${unit}.`}
      </p>
      {observed.error && <ErrorState error={observed.error} retry={observed.reload} />}
      {!data && !observed.error && <Loading what="price history" />}
      {data && <CandleChart bars={data.items} gaps={data.gaps} clockMs={clockMs} markers={markers} scale={scale ?? undefined} />}
    </div>
  );
}
