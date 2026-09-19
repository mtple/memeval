/** Formatting helpers. Raw quantities are decimal strings in atomic units and are never parsed to Number. */

const RAW_RE = /^-?\d+$/;

/** Format a raw atomic-unit decimal string using BigInt division by 10**decimals. */
export function fmtRaw(raw: string | null | undefined, decimals: number | null | undefined, places = 6): string {
  if (raw === null || raw === undefined) return "—";
  const s = String(raw).trim();
  if (!RAW_RE.test(s)) return s;
  if (decimals === null || decimals === undefined || decimals < 0) return s;
  let v = BigInt(s);
  const neg = v < 0n;
  if (neg) v = -v;
  const base = 10n ** BigInt(decimals);
  const whole = v / base;
  let frac = (v % base).toString().padStart(decimals, "0");
  if (places < decimals) frac = frac.slice(0, places);
  frac = frac.replace(/0+$/, "");
  const wholeStr = whole.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return `${neg ? "-" : ""}${wholeStr}${frac ? "." + frac : ""}`;
}

/** The unit people know: a recorded pack calls its wrapped native coin NATIVE (the generic name inside a session); on the site that is ETH. */
export function unitLabel(unit: string | null | undefined): string {
  if (!unit) return "";
  return unit === "NATIVE" ? "ETH" : unit;
}

/** Format a raw amount and append the numeraire alias when known. */
export function fmtAmount(raw: string | null | undefined, decimals: number | null | undefined, unit?: string | null, places = 6): string {
  const v = fmtRaw(raw, decimals, places);
  return unit && v !== "—" ? `${v} ${unitLabel(unit)}` : v;
}

/** Relative virtual time: "2d 03:14:07", negative prehistory as "-1d 00:00:00". */
export function fmtRel(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return "—";
  const neg = ms < 0;
  let s = Math.floor(Math.abs(ms) / 1000);
  const d = Math.floor(s / 86400);
  s -= d * 86400;
  const h = Math.floor(s / 3600);
  s -= h * 3600;
  const m = Math.floor(s / 60);
  s -= m * 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${neg ? "-" : ""}${d}d ${pad(h)}:${pad(m)}:${pad(s)}`;
}

/** Duration label for a pack: "7d full week" only when is_full_week; otherwise hours/minutes. */
export function fmtDuration(ms: number, isFullWeek: boolean): string {
  if (isFullWeek) return "7d full week";
  const hours = ms / 3_600_000;
  if (hours >= 48) return `${(hours / 24).toFixed(1)}d (${Math.round(hours)}h, not a full week)`;
  if (hours >= 1) return `${hours % 1 === 0 ? hours : hours.toFixed(1)}h`;
  return `${Math.round(ms / 60_000)}m`;
}

export function fmtMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  if (ms >= 1000) return `${(ms / 1000).toFixed(ms % 1000 === 0 ? 0 : 1)}s`;
  return `${ms}ms`;
}

export function fmtDate(iso: string | number | null | undefined): string {
  if (iso === null || iso === undefined || iso === "") return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  return d.toISOString().replace("T", " ").replace(/\.\d+Z$/, "Z");
}

/** Decimal-string return (e.g. "-0.0123") as a percentage; null stays explicit. */
export function fmtReturn(v: string | number | null | undefined): string {
  if (v === null || v === undefined || v === "") return "null";
  const n = Number(v);
  if (!Number.isFinite(n)) return String(v);
  return `${(n * 100).toFixed(3)}%`;
}

export function fmtPct(v: number | string | null | undefined, digits = 2): string {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return String(v);
  return `${(n * 100).toFixed(digits)}%`;
}

export function shortHash(h: string | null | undefined, n = 12): string {
  if (!h) return "—";
  return h.length > n ? `${h.slice(0, n)}…` : h;
}

export function humanize(s: string | null | undefined): string {
  if (!s) return "—";
  return s.replace(/_/g, " ");
}

/** Legacy API modes describe whether the operator can reveal dates and mappings. */
export function exportPolicy(mode: string): string {
  return mode === "practice" ? "Revealable" : mode === "sealed" ? "Sealed" : mode;
}
