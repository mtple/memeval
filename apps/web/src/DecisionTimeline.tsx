import { useState } from "react";
import { get } from "./api";
import { fmtMs, humanize } from "./format";
import { Badge, Card, ErrorState, JsonView } from "./ui";

type Decision = {
  index: number; tool: string; started_ms: number; delivered_ms: number;
  status: string; error_code: string | null; decision_elapsed_ms: number;
  request: Record<string, unknown>;
  delivered: { evidence_basis?: string; sha256?: string; payload?: unknown; payload_omitted?: boolean };
  order: { state: string; submitted_ms: number; ready_ms: number; inclusion_time_ms: number | null; intent?: { reason?: string; exit_condition?: string } } | null;
};
type Page = { items: Decision[]; next_cursor: number | null; total: number; note: string };

export function DecisionTimeline({ runId }: { runId: string }) {
  const [page, setPage] = useState<Page | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const load = async () => {
    setBusy(true);
    setError(null);
    try {
      const next = await get<Page>(`/runs/${encodeURIComponent(runId)}/timeline?cursor=${page?.next_cursor ?? 0}&limit=25`);
      setPage(previous => ({ ...next, items: [...(previous?.items ?? []), ...next.items] }));
    } catch (e) { setError(e); } finally { setBusy(false); }
  };
  return <Card title="Decision timeline" actions={(!page || page.next_cursor !== null) && <button className="btn btn-small" disabled={busy} onClick={load}>{busy ? "Loading…" : page ? "Load more decisions" : "Load decision timeline"}</button>}>
    <p className="muted small">Follow the information delivered to the agent, its requests, and the resulting execution. Reasons and exit conditions are optional notes written by the agent before submission.</p>
    {error != null && <ErrorState error={error} retry={load} />}
    {page && <>
      <p className="small muted">Showing {page.items.length} of {page.total} requests. {page.note}</p>
      <ol className="decision-timeline">
        {page.items.map(item => <li key={item.index}>
          <div className="row"><strong>{humanize(item.tool.replace(/\./g, "_"))}</strong><span className="mono small">{fmtMs(item.started_ms)} → {fmtMs(item.delivered_ms)}</span><Badge tone={item.status === "ok" ? "neutral" : "warn"}>{item.error_code ?? item.status}</Badge></div>
          {item.order && <p className="small">Order {humanize(item.order.state)}. Ready at {fmtMs(item.order.ready_ms)}; included {item.order.inclusion_time_ms == null ? "never" : `at ${fmtMs(item.order.inclusion_time_ms)}`}.</p>}
          {item.order?.intent?.reason && <p><strong>Recorded reason:</strong> {item.order.intent.reason}</p>}
          {item.order?.intent?.exit_condition && <p><strong>Intended exit:</strong> {item.order.intent.exit_condition}</p>}
          {item.delivered.evidence_basis === "reconstructed_under_current_engine" && <p className="notice small">Legacy trace: this observation was reconstructed under the current engine. It is not an original delivery record.</p>}
          {item.delivered.payload_omitted && <p className="muted small">The delivery exceeded the recording limit. Its digest is retained.</p>}
          <details><summary>Inspect request and delivered observation</summary><JsonView value={{ request: item.request, delivered: item.delivered, execution: item.order }} open /></details>
        </li>)}
      </ol>
    </>}
  </Card>;
}
