import { useState } from "react";
import type { Bar } from "./api";
const fmtClock = (ms: number) => `${String(Math.floor(ms / 3_600_000)).padStart(2, "0")}:${String(Math.floor(ms / 60_000) % 60).padStart(2, "0")}`;

export type TradeMarker = { id: string; time_ms: number; price: string; side: string; label: string; detail: string };
type Gap = { start_ms: number; end_ms: number; reason: string };
const price = (s: string | null) => s !== null && Number.isFinite(Number(s)) && Number(s) > 0 ? Number(s) : null;
export const signedPercent = (n: number) => `${n > 0 ? "+" : ""}${n.toFixed(2)}%`;

/** Numeric conversion is only for chart coordinates and relative price display, never cash math. */
export function tradeChartData(bars: Bar[], gaps: Gap[], markers: TradeMarker[], clock: number, full: boolean) {
  const fills = markers.filter(m => m.time_ms <= clock && price(m.price) !== null).sort((a, b) => a.time_ms - b.time_ms);
  const visible = bars.filter(b => b.start_ms < clock && (!b.closed || b.end_ms <= clock) && !b.synthetic_empty_bar && price(b.close) !== null).sort((a, b) => a.start_ms - b.start_ms);
  const firstBuy = fills.find(m => m.side === "buy");
  const reference = price(firstBuy?.price ?? visible[0]?.close ?? fills[0]?.price ?? null);
  const first = fills[0]?.time_ms ?? visible[0]?.start_ms ?? 0;
  const last = fills[fills.length - 1]?.time_ms ?? clock;
  const margin = Math.max(15 * 60_000, (last - first) * .2);
  const start = full ? 0 : Math.max(0, first - margin);
  const end = Math.max(start + 1, full ? clock : Math.min(clock, last + margin));
  const clippedGaps = gaps.filter(g => g.end_ms > start && g.start_ms < end).map(g => ({...g, start_ms: Math.max(start, g.start_ms), end_ms: Math.min(end, g.end_ms)}));
  const points = visible.filter(b => Math.min(b.end_ms, clock) >= start && Math.min(b.end_ms, clock) <= end).map(b => ({ time: Math.min(b.end_ms, clock), value: reference ? (Number(b.close) / reference - 1) * 100 : 0, bar: b }));
  const segments: typeof points[] = [];
  for (const point of points) {
    const segment = segments[segments.length - 1];
    const previous = segment?.[segment.length - 1];
    const broken = !previous || point.bar.start_ms > previous.bar.end_ms || clippedGaps.some(g => g.start_ms < point.time && g.end_ms > previous.time);
    if (broken) segments.push([point]); else segment.push(point);
  }
  return { fills, firstBuy, reference, start, end, gaps: clippedGaps, points, segments };
}

export function TradePriceChart({ bars, gaps, markers, clockMs, token }: { bars: Bar[]; gaps: Gap[]; markers: TradeMarker[]; clockMs: number; token: string }) {
  const [full, setFull] = useState(false);
  const [active, setActive] = useState<string | null>(null);
  const [hover, setHover] = useState<number | null>(null);
  const d = tradeChartData(bars, gaps, markers, clockMs, full);
  if (!d.reference) return <p className="chart-empty">No usable prices were recorded for this token.</p>;
  const relative = (p: string) => (Number(p) / d.reference! - 1) * 100;
  const values = [...d.points.map(p => p.value), ...d.fills.map(m => relative(m.price)), 0];
  const low = Math.min(...values), high = Math.max(...values), padding = Math.max((high - low) * .16, .25);
  const roughStep = (high - low + 2 * padding) / 4;
  const magnitude = 10 ** Math.floor(Math.log10(roughStep));
  const step = ([1, 2, 2.5, 5, 10].find(n => n * magnitude >= roughStep) ?? 10) * magnitude;
  const lo = Math.floor((low - padding) / step) * step, hi = Math.ceil((high + padding) / step) * step;
  const w = 900, h = 350, left = 78, right = 24, top = 40, bottom = 70;
  const x = (t: number) => left + (t - d.start) / (d.end - d.start) * (w - left - right);
  const y = (v: number) => top + (hi - v) / (hi - lo) * (h - top - bottom);
  const selected = d.fills.find(m => m.id === active) ?? d.firstBuy ?? d.fills[0];
  const point = hover === null ? null : d.points[hover];
  const ticks = Array.from({length: Math.round((hi - lo) / step) + 1}, (_, i) => lo + step * i);
  const referenceLabel = d.firstBuy ? "first buy price" : "first observed price";
  return <section className="trade-chart" aria-label={`${token} price and trades`}>
    <div className="trade-chart-heading">
      <div><h3>Where the agent bought and sold</h3><p className="muted small">Token price change from its {referenceLabel}. This is price movement, not your account return.</p></div>
      <div className="chart-toggle" aria-label="Chart time range"><button type="button" aria-pressed={!full} onClick={() => {setFull(false); setHover(null);}}>Around trades</button><button type="button" aria-pressed={full} onClick={() => {setFull(true); setHover(null);}}>Full replay</button></div>
    </div>
    <div className="chart-legend"><span><i className="legend-line" />Observed price</span><span><i className="legend-buy" />Buy</span><span><i className="legend-sell" />Sell</span><span>Dashed line = {referenceLabel} (0%)</span></div>
    <p className="chart-mobile-hint small muted">Swipe the chart horizontally to see the whole time range.</p>
    <div className="trade-chart-scroll">
      <svg viewBox={`0 0 ${w} ${h}`} className="trade-price-svg" role="group" aria-label={`${token}: price change in percent; time elapsed since replay start`}
        onMouseMove={event => {
          if ((event.target as Element).closest(".trade-marker")) {setHover(null); return;}
          const bounds = event.currentTarget.getBoundingClientRect();
          const t = d.start + ((event.clientX - bounds.left) / bounds.width * w - left) / (w - left - right) * (d.end - d.start);
          let nearest = -1, distance = Infinity;
          d.points.forEach((p, i) => { if (Math.abs(p.time - t) < distance) { nearest = i; distance = Math.abs(p.time - t); } });
          setHover(nearest < 0 ? null : nearest);
        }} onMouseLeave={() => setHover(null)}>
        <text x={left} y={18} className="axis">PRICE CHANGE (%)</text>
        {ticks.map((v, i) => <g key={i}><line x1={left} x2={w - right} y1={y(v)} y2={y(v)} className="grid"/><text x={left - 12} y={y(v) + 4} textAnchor="end" className="axis">{signedPercent(v)}</text></g>)}
        {d.gaps.map((g, i) => <rect key={i} x={x(g.start_ms)} y={top} width={Math.max(0, x(g.end_ms) - x(g.start_ms))} height={h - top - bottom} className="price-gap"><title>{`No observed prices: ${fmtClock(g.start_ms)} to ${fmtClock(g.end_ms)}`}</title></rect>)}
        <line x1={left} x2={w-right} y1={y(0)} y2={y(0)} className="price-reference" />
        {d.segments.map((segment, i) => segment.length > 1 ? <polyline key={i} points={segment.map(p => `${x(p.time)},${y(p.value)}`).join(" ")} className="observed-price-line"/> : <circle key={i} cx={x(segment[0]!.time)} cy={y(segment[0]!.value)} r={2} className="observed-price-point"/>)}
        {d.fills.map((m, i) => {
          const px = x(m.time_ms), py = y(relative(m.price)), buy = m.side === "buy";
          const labelY = buy ? h - bottom + 18 + (i % 2) * 14 : top - 10;
          return <g key={m.id} role="button" tabIndex={0} aria-label={`${m.label} at ${fmtClock(m.time_ms)}, ${signedPercent(relative(m.price))}. ${m.detail}`} aria-pressed={selected?.id === m.id}
            className={`trade-marker ${buy ? "buy-marker" : "sell-marker"} ${selected?.id === m.id ? "active" : ""}`}
            onFocus={() => {setActive(m.id); setHover(null);}} onClick={() => {setActive(m.id); setHover(null);}}
            onKeyDown={e => {if (e.key === "Enter" || e.key === " ") {e.preventDefault(); setActive(m.id); setHover(null);}}}>
            <title>{`${m.label}: ${fmtClock(m.time_ms)} · ${signedPercent(relative(m.price))} · ${m.detail}`}</title>
            <line x1={px} x2={px} y1={py} y2={buy ? h-bottom : top} className="trade-guide" />
            <circle cx={px} cy={py} r={selected?.id === m.id ? 8 : 6}/>
            <text x={Math.max(left+20,Math.min(w-right-22,px))} y={labelY} textAnchor="middle">{m.label}</text>
          </g>;
        })}
        {point && <g pointerEvents="none"><line x1={x(point.time)} x2={x(point.time)} y1={top} y2={h-bottom} className="price-reference"/><circle cx={x(point.time)} cy={y(point.value)} r={4} className="observed-price-point"/></g>}
        {Array.from({length:5},(_,i) => {const t=d.start+(d.end-d.start)*i/4; return <text key={`time-${i}`} x={x(t)} y={h-7} className="axis" textAnchor={i===0 ? "start" : i===4 ? "end" : "middle"}>{fmtClock(t)}</text>;})}
      </svg>
    </div>
    <div className="chart-axis-caption">Time elapsed since replay start (hours:minutes)</div>
    <div className="trade-chart-readout" aria-live="polite">
      {point ? <><strong>Observed price · {fmtClock(point.time)}</strong><span>{signedPercent(point.value)} vs {referenceLabel}</span><span className="small muted">{point.bar.closed ? "End of" : "Partial"} observation interval · {point.bar.trade_count} observed trades</span></> : selected ? <><strong>{selected.label} · {fmtClock(selected.time_ms)}</strong><span>{signedPercent(relative(selected.price))} vs {referenceLabel}</span><span className="small">{selected.detail}</span></> : <span>Hover over the line to inspect a price observation.</span>}
    </div>
    <p className="small muted chart-help">Select a labeled trade or hover over the line. Keyboard: Tab to a trade, then Enter. {d.points.length ? "The line joins observed interval closes; blank stretches have no observed prices. Fill prices can differ from the line." : "No observed market history is available. Only recorded fills are shown."}</p>
  </section>;
}
