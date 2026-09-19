import type { Bar } from "./api";
import { fmtClock, fmtRel } from "./format";

/*
 * Inline-SVG charts. Decimal strings are converted to Number ONLY for pixel placement;
 * every displayed value elsewhere is formatted from the original string with BigInt.
 */

const num = (s: string | null | undefined): number | null => {
  if (s === null || s === undefined) return null;
  const n = Number(s);
  return Number.isFinite(n) ? n : null;
};

type Gap = { start_ms: number; end_ms: number; reason: string };

/** A unit that makes tiny per-token prices readable: 0.0000000543 ETH becomes 54.3 nano-ETH. */
export type PriceScale = { factor: number; unitName: string; oneIs: string; fmt: (v: string | number) => string };
const SCALES: [number, string, string][] = [
  [1, "ETH", "1"],
  [1e-3, "milli-ETH", "0.001"],
  [1e-6, "micro-ETH", "0.000001"],
  [1e-9, "nano-ETH", "0.000000001"],
  [1e-12, "pico-ETH", "0.000000000001"],
  [1e-15, "femto-ETH", "0.000000000000001"],
  [1e-18, "wei", "0.000000000000000001"],
];
export function priceScale(values: (string | number)[], unit = "ETH"): PriceScale | null {
  const nums = values.map(Number).filter((v) => Number.isFinite(v) && v > 0);
  if (!nums.length) return null;
  const typical = nums.slice().sort((a, b) => a - b)[Math.floor(nums.length / 2)]!;
  const pick = SCALES.find(([f]) => typical >= f) ?? SCALES[SCALES.length - 1]!;
  const [factor, name, oneIs] = pick;
  const unitName = name === "ETH" ? unit : unit === "ETH" ? name : `${name.replace("ETH", unit)}`;
  const fmt = (v: string | number) => {
    const x = Number(v) / factor;
    if (!Number.isFinite(x)) return String(v);
    return x >= 100 ? x.toFixed(0) : x >= 10 ? x.toFixed(1) : x.toFixed(2);
  };
  return { factor, unitName, oneIs, fmt };
}

export function CandleChart({ bars, gaps, clockMs, markers = [], width = 720, height = 220, scale }: { bars: Bar[]; gaps: Gap[]; clockMs: number; markers?: { time_ms: number; price: string; label: string; side: string }[]; width?: number; height?: number; scale?: PriceScale }) {
  const tick = (v: number) => (scale ? scale.fmt(v) : v.toPrecision(5));
  const when = (t: number) => (scale ? fmtClock(t) : fmtRel(t));
  // Hard guard: never render anything at or beyond the virtual clock.
  const visible = bars.filter((b) => b.start_ms < clockMs && !b.synthetic_empty_bar && b.close !== null);
  if (visible.length === 0) {
    return (
      <div className="chart-empty" role="img" aria-label="No observed bars yet">
        No observed bars before the current virtual clock.
      </div>
    );
  }
  const pad = { l: 56, r: 8, t: 8, b: 22 };
  const first = visible[0]!;
  const last = visible[visible.length - 1]!;
  const eligibleMarkers = markers.filter(m => m.time_ms <= clockMs && num(m.price) !== null);
  const t0 = eligibleMarkers.reduce((v, m) => Math.min(v, m.time_ms), first.start_ms);
  const t1 = eligibleMarkers.reduce((v, m) => Math.max(v, m.time_ms), Math.min(clockMs, last.end_ms));
  const xs = (t: number) => pad.l + ((t - t0) / Math.max(1, t1 - t0)) * (width - pad.l - pad.r);
  let lo = Infinity;
  let hi = -Infinity;
  for (const b of visible) {
    for (const v of [num(b.low), num(b.high), num(b.close), num(b.open)]) {
      if (v !== null) {
        lo = Math.min(lo, v);
        hi = Math.max(hi, v);
      }
    }
  }
  const visibleMarkers = markers.filter(m => m.time_ms <= clockMs && m.time_ms >= t0 && m.time_ms <= t1 && num(m.price) !== null);
  for (const m of visibleMarkers) { lo = Math.min(lo, Number(m.price)); hi = Math.max(hi, Number(m.price)); }
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) return <div className="chart-empty">Prices not numeric.</div>;
  if (hi === lo) {
    hi += 1;
    lo -= 1;
  }
  const ys = (v: number) => pad.t + (1 - (v - lo) / (hi - lo)) * (height - pad.t - pad.b);
  const bw = Math.max(1, Math.min(10, ((width - pad.l - pad.r) / visible.length) * 0.7));
  const ticks = [lo, (lo + hi) / 2, hi];
  return (
    <svg className="chart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`Observed price bars for ${visible.length} bars up to ${fmtRel(clockMs)}`}>
      {ticks.map((v, i) => (
        <g key={i}>
          <line x1={pad.l} x2={width - pad.r} y1={ys(v)} y2={ys(v)} className="grid" />
          <text x={pad.l - 6} y={ys(v) + 4} textAnchor="end" className="axis">
            {tick(v)}
          </text>
        </g>
      ))}
      {gaps
        .filter((g) => g.start_ms < clockMs)
        .map((g, i) => {
          const x1 = xs(Math.max(g.start_ms, t0));
          const x2 = xs(Math.min(g.end_ms, clockMs));
          return <rect key={i} x={x1} y={pad.t} width={Math.max(2, x2 - x1)} height={height - pad.t - pad.b} className="gap" aria-label={`gap: ${g.reason}`} />;
        })}
      {visible.map((b) => {
        const o = num(b.open) ?? num(b.close)!;
        const c = num(b.close)!;
        const h = num(b.high) ?? Math.max(o, c);
        const l = num(b.low) ?? Math.min(o, c);
        const x = xs((b.start_ms + Math.min(b.end_ms, clockMs)) / 2);
        const up = c >= o;
        const partial = (b.completeness !== undefined && b.completeness !== "complete") || b.closed === false;  // stored series omit the defaults
        return (
          <g key={b.start_ms} className={`candle ${up ? "up" : "down"} ${partial ? "partial" : ""}`}>
            <line x1={x} x2={x} y1={ys(h)} y2={ys(l)} />
            <rect x={x - bw / 2} y={ys(Math.max(o, c))} width={bw} height={Math.max(1, Math.abs(ys(o) - ys(c)))} />
          </g>
        );
      })}
      {visibleMarkers.map((m, i) => <g key={`${m.time_ms}-${i}`} role="img" aria-label={`${m.label} at ${when(m.time_ms)}, price ${tick(Number(m.price))}`}>
        <title>{`${m.label} at ${when(m.time_ms)} for ${tick(Number(m.price))}${scale ? ` ${scale.unitName}` : ""} per token`}</title>
        <circle cx={xs(m.time_ms)} cy={ys(Number(m.price))} r={5} fill={m.side === "buy" ? "#168047" : "#be4535"} stroke="white" />
        <text x={xs(m.time_ms)} y={Math.max(12, ys(Number(m.price)) - 9)} textAnchor="middle" fill="currentColor" fontSize="11">{m.side === "buy" ? "B" : "S"}</text>
      </g>)}
      <line x1={xs(t1)} x2={xs(t1)} y1={pad.t} y2={height - pad.b} className="clock-line" />
      <text x={pad.l} y={height - 6} className="axis">
        {when(t0)}
      </text>
      <text x={width - pad.r} y={height - 6} textAnchor="end" className="axis">
        {when(t1)}{scale ? "" : " (clock)"}
      </text>
    </svg>
  );
}

export function EquitySparkline({ points, clockMs, width = 720, height = 90 }: { points: { time_ms: number; equity_raw: string | null; complete: boolean }[]; clockMs: number; width?: number; height?: number }) {
  const pts = points.filter((p) => p.time_ms <= clockMs);
  const vals = pts.map((p) => (p.equity_raw === null ? null : num(p.equity_raw)));
  const present = vals.filter((v): v is number => v !== null);
  if (pts.length === 0 || present.length === 0) {
    return <div className="chart-empty">No equity observations up to the current virtual clock.</div>;
  }
  const pad = { l: 8, r: 8, t: 6, b: 6 };
  const t0 = pts[0]!.time_ms;
  const t1 = Math.max(pts[pts.length - 1]!.time_ms, t0 + 1);
  let lo = Math.min(...present);
  let hi = Math.max(...present);
  if (hi === lo) {
    hi += 1;
    lo -= 1;
  }
  const xs = (t: number) => pad.l + ((t - t0) / (t1 - t0)) * (width - pad.l - pad.r);
  const ys = (v: number) => pad.t + (1 - (v - lo) / (hi - lo)) * (height - pad.t - pad.b);
  // Break the line at any null / incomplete point so gaps are drawn as gaps.
  const segments: string[] = [];
  let cur: string[] = [];
  const breaks: number[] = [];
  pts.forEach((p, i) => {
    const v = vals[i];
    if (v === null || v === undefined || !p.complete) {
      if (cur.length) segments.push(cur.join(" "));
      cur = [];
      breaks.push(p.time_ms);
    } else {
      cur.push(`${xs(p.time_ms).toFixed(1)},${ys(v).toFixed(1)}`);
    }
  });
  if (cur.length) segments.push(cur.join(" "));
  const gapCount = breaks.length;
  return (
    <svg className="chart spark" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`Model equity sparkline, ${present.length} points, ${gapCount} gaps`}>
      {breaks.map((t, i) => (
        <line key={i} x1={xs(t)} x2={xs(t)} y1={pad.t} y2={height - pad.b} className="gap-line" />
      ))}
      {segments.map((d, i) => (
        <polyline key={i} points={d} fill="none" className="equity-line" />
      ))}
    </svg>
  );
}

