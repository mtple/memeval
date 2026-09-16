import type { Bar } from "./api";
import { fmtRel } from "./format";

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

export function CandleChart({ bars, gaps, clockMs, width = 720, height = 220 }: { bars: Bar[]; gaps: Gap[]; clockMs: number; width?: number; height?: number }) {
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
  const t0 = first.start_ms;
  const t1 = Math.min(clockMs, last.end_ms);
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
            {v.toPrecision(5)}
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
        const partial = b.completeness !== "complete" || !b.closed;
        return (
          <g key={b.start_ms} className={`candle ${up ? "up" : "down"} ${partial ? "partial" : ""}`}>
            <line x1={x} x2={x} y1={ys(h)} y2={ys(l)} />
            <rect x={x - bw / 2} y={ys(Math.max(o, c))} width={bw} height={Math.max(1, Math.abs(ys(o) - ys(c)))} />
          </g>
        );
      })}
      <line x1={xs(t1)} x2={xs(t1)} y1={pad.t} y2={height - pad.b} className="clock-line" />
      <text x={pad.l} y={height - 6} className="axis">
        {fmtRel(t0)}
      </text>
      <text x={width - pad.r} y={height - 6} textAnchor="end" className="axis">
        {fmtRel(t1)} (clock)
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
