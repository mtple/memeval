import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { ApiError } from "./api";

export type Tone = "neutral" | "ok" | "warn" | "bad" | "info" | "env" | "muted";

export function Badge({ tone = "neutral", children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  return (
    <span className={`badge badge-${tone}`} title={title}>
      {children}
    </span>
  );
}

export function toneForStatus(s: string | null | undefined): Tone {
  switch (s) {
    case "passed":
    case "completed":
    case "qualified_for_named_suite":
    case "confirmed":
      return "ok";
    case "warning":
    case "paused":
    case "research":
    case "budget_exhausted":
    case "aborted":
    case "demo":
      return "warn";
    case "failed":
    case "agent_failed":
    case "rejected":
      return "bad";
    case "environment_failed":
      return "env";
    case "running":
    case "queued":
      return "info";
    case "diagnostic_only":
    case "not_applicable":
      return "muted";
    default:
      return "neutral";
  }
}

export function RunStateBadge({ state }: { state: string }) {
  const label = state === "agent_failed" ? "agent error" : state === "environment_failed" ? "environment/data error" : state.replace(/_/g, " ");
  return <Badge tone={toneForStatus(state)}>{label}</Badge>;
}

export function Card({ title, children, actions, className = "" }: { title?: ReactNode; children: ReactNode; actions?: ReactNode; className?: string }) {
  return (
    <section className={`card ${className}`}>
      {(title || actions) && (
        <header className="card-head">
          {title && <h2>{title}</h2>}
          {actions && <div className="card-actions">{actions}</div>}
        </header>
      )}
      {children}
    </section>
  );
}

export function KV({ rows }: { rows: [ReactNode, ReactNode][] }) {
  return (
    <dl className="kv">
      {rows.map(([k, v], i) => (
        <div key={i} className="kv-row">
          <dt>{k}</dt>
          <dd>{v ?? "—"}</dd>
        </div>
      ))}
    </dl>
  );
}

export function JsonView({ value, open = false }: { value: unknown; open?: boolean }) {
  return (
    <details className="json" open={open}>
      <summary>JSON</summary>
      <pre>{JSON.stringify(value, null, 2)}</pre>
    </details>
  );
}

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="state state-empty" role="status">
      <strong>{title}</strong>
      {children && <div>{children}</div>}
    </div>
  );
}

export function ErrorState({ error, retry }: { error: unknown; retry?: () => void }) {
  const e = error instanceof ApiError ? error : null;
  const msg = error instanceof Error ? error.message : String(error);
  return (
    <div className="state state-error" role="alert">
      {e?.isAuth ? (
        <>
          <strong>Not authorised ({e.status}).</strong>
          <p>
            The control plane requires an admin token. Paste it in the <a href="#settings">settings bar</a> at the top of the page (or open the app with{" "}
            <code>?token=…</code>).
          </p>
        </>
      ) : (
        <>
          <strong>Request failed{e ? ` (${e.status})` : ""}.</strong>
          <p>{msg}</p>
          {!e && <p>Is the Market Replay server running on this origin (dev: proxied to 127.0.0.1:8000)?</p>}
        </>
      )}
      {retry && (
        <button type="button" className="btn" onClick={retry}>
          Retry
        </button>
      )}
    </div>
  );
}

export function Loading({ what = "" }: { what?: string }) {
  return (
    <p className="muted" role="status">
      Loading {what}…
    </p>
  );
}

export function LinkBtn({ to, children }: { to: string; children: ReactNode }) {
  return (
    <Link to={to} className="btn btn-link">
      {children}
    </Link>
  );
}

/** Data loader with manual refresh and optional polling. */
export function useLoad<T>(fn: () => Promise<T>, deps: unknown[], pollMs?: number | null) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const [tick, setTick] = useState(0);
  const reload = useCallback(() => setTick((t) => t + 1), []);
  useEffect(() => {
    let alive = true;
    setLoading(true);
    fnRef
      .current()
      .then((d) => {
        if (alive) {
          setData(d);
          setError(null);
        }
      })
      .catch((e) => alive && setError(e))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);
  useEffect(() => {
    if (!pollMs) return;
    const id = setInterval(reload, pollMs);
    return () => clearInterval(id);
  }, [pollMs, reload]);
  return { data, error, loading, reload, setData };
}

export function downloadJson(name: string, value: unknown) {
  const blob = new Blob([JSON.stringify(value, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      type="button"
      className="btn btn-small"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setDone(true);
          setTimeout(() => setDone(false), 1500);
        } catch {
          window.prompt("Copy this value", text);
        }
      }}
    >
      {done ? "Copied" : label}
    </button>
  );
}

export function GateSummary({ gates }: { gates: { status: string }[] | undefined }) {
  const c = { passed: 0, warning: 0, failed: 0, other: 0 };
  for (const g of gates ?? []) {
    if (g.status === "passed") c.passed++;
    else if (g.status === "warning") c.warning++;
    else if (g.status === "failed") c.failed++;
    else c.other++;
  }
  return (
    <span className="gates" title={`${c.passed} passed, ${c.warning} warning, ${c.failed} failed, ${c.other} n/a`}>
      <Badge tone="ok">{c.passed} ✓</Badge> <Badge tone="warn">{c.warning} !</Badge> <Badge tone="bad">{c.failed} ✕</Badge>
      {c.other > 0 && <Badge tone="muted">{c.other} n/a</Badge>}
    </span>
  );
}

export function GateList({ gates }: { gates: { gate: string; status: string; detail?: string }[] | undefined }) {
  if (!gates?.length) return <p className="muted">No gates reported.</p>;
  return (
    <ul className="gate-list">
      {gates.map((g) => (
        <li key={g.gate}>
          <Badge tone={toneForStatus(g.status)}>{g.status.replace(/_/g, " ")}</Badge> <strong>{g.gate}</strong>
          {g.detail && <span className="muted"> — {g.detail}</span>}
        </li>
      ))}
    </ul>
  );
}

export function StrList({ items, empty = "None" }: { items: (string | ReactNode)[] | undefined | null; empty?: string }) {
  if (!items || items.length === 0) return <p className="muted">{empty}</p>;
  return (
    <ul className="plain">
      {items.map((s, i) => (
        <li key={i}>{s}</li>
      ))}
    </ul>
  );
}
