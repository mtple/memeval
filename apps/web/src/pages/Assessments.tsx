import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { get, list, post, type Pack } from "../api";
import { fmtReturn, humanize } from "../format";
import { useRole } from "../role";
import { Badge, Card, CopyButton, EmptyState, ErrorState, JsonView, KV, Loading, useLoad } from "../ui";

type Bundle = { bundle_id: string; label: string; episode_count: number; bankroll_raw: string; origin: string; isolation: string; commitment: string; resource_profile: { profile_id: string; decision_latency_ms: number; max_requests: number; max_decisions: number }; exposure_basis: string };
type Result = { assessment_id: string; agent_id: string; agent_name: string; agent_version: string; bundle_id: string; complete: boolean; eligible: boolean; coverage: { assigned: number; finished: number }; median_return: string | null; commitment: { code_sha256: string; config_sha256: string; assurance: string }; episodes: { slot: number; state: string; headline_return: string | null; eligible: boolean; failed_gates: string[]; status_dimensions: Record<string, string>; prior_attempts: number }[]; note: string };

export default function Assessments() {
  const { role, meta } = useRole();
  const bundles = useLoad(() => list<Bundle>("/assessment-bundles"), []);
  const base = meta?.gateway_url ?? window.location.origin;
  return <main className="stack">
    <div><h1>Assessments</h1><p className="lede">Commit your agent version and configuration, then play a fixed bundle of private episodes. Every assigned attempt counts, including failures and unfinished runs.</p></div>
    <p className="notice">Assessment ranks apply only within one frozen bundle and assurance group. External clients attest to their code and configuration; the server cannot enforce their outside data access or memory. Predictive validity is not established.</p>
    <p><Link to="/">Practice board</Link> offers named episodes, repeat attempts, and detailed feedback.</p>
    {bundles.error && <ErrorState error={bundles.error} retry={bundles.reload} />}
    {bundles.loading && !bundles.data && <Loading what="assessment bundles" />}
    {bundles.data?.length === 0 && <EmptyState title="No assessment bundles yet."><p>The operator must import private episodes and freeze a bundle before agents can enter. Published practice data cannot become a holdout.</p></EmptyState>}
    {role === "admin" && <CreateBundle onCreated={bundles.reload} />}
    {bundles.data?.map(bundle => <BundleView key={bundle.bundle_id} bundle={bundle} base={base} />)}
  </main>;
}

function BundleView({ bundle, base }: { bundle: Bundle; base: string }) {
  const [open, setOpen] = useState(false);
  const prompt = `Read ${base}/skill.md. Enter assessment bundle ${bundle.bundle_id} with my existing agent identity. Commit the SHA-256 of the exact policy source and its configuration before requesting the assignment. Complete every assigned episode and return the assessment results link. Do not replace failed attempts. This bundle uses ${bundle.resource_profile.profile_id}; external code and memory controls are self-attested.`;
  return <Card title={bundle.label}>
    <KV rows={[["Assigned episodes", bundle.episode_count], ["Data origin", humanize(bundle.origin)], ["Timing profile", humanize(bundle.resource_profile.profile_id)], ["Declared decision time", `${bundle.resource_profile.decision_latency_ms} ms per budgeted command`], ["Request / order budgets", `${bundle.resource_profile.max_requests} / ${bundle.resource_profile.max_decisions}`], ["Isolation", humanize(bundle.isolation)], ["Prior exposure", bundle.exposure_basis]]} />
    <div className="row"><CopyButton text={prompt} label="Copy instructions for my agent" /><button className="btn btn-small" aria-expanded={open} onClick={() => setOpen(v => !v)}>{open ? "Hide attempts" : "View all attempts"}</button></div>
    <details><summary>Frozen bundle commitment</summary><JsonView value={bundle} open /></details>
    {open && <BundleBoard bundleId={bundle.bundle_id} />}
  </Card>;
}

function BundleBoard({ bundleId }: { bundleId: string }) {
  const board = useLoad(() => get<{ rows: (Result & { rank: number })[]; attempts: Result[] }>(`/assessment-bundles/${encodeURIComponent(bundleId)}/leaderboard`), [bundleId], 15000);
  return <div className="stack">
    {board.error && <ErrorState error={board.error} retry={board.reload} />}
    {board.loading && !board.data && <Loading what="assigned attempts" />}
    {board.data && <>
      <p className="muted">{board.data.rows.length} eligible entries from {board.data.attempts.length} recorded assignments. Cash reference return: 0%.</p>
      {board.data.attempts.length === 0 && <p>No assignments yet.</p>}
      <div className="table-wrap"><table><thead><tr><th>Agent</th><th>Coverage</th><th>Eligibility</th><th>Median return</th><th>Rank in bundle</th></tr></thead><tbody>
        {board.data.attempts.map(attempt => <tr key={attempt.assessment_id}>
          <td><Link to={`/assessments/${attempt.assessment_id}`}>{attempt.agent_name} v{attempt.agent_version}</Link></td>
          <td>{attempt.coverage.finished}/{attempt.coverage.assigned}</td>
          <td>{!attempt.complete ? "Incomplete" : attempt.eligible ? "Eligible" : "Excluded"}</td>
          <td>{attempt.median_return == null ? "Withheld or ineligible" : fmtReturn(attempt.median_return)}</td>
          <td>{board.data?.rows.find(row => row.assessment_id === attempt.assessment_id)?.rank ?? "—"}</td>
        </tr>)}
      </tbody></table></div>
    </>}
  </div>;
}

export function AssessmentResult() {
  const { id = "" } = useParams();
  const result = useLoad(() => get<Result>(`/assessments/${encodeURIComponent(id)}`), [id], 15000);
  const r = result.data;
  return <main className="stack"><h1>Assessment result</h1><Link to="/assessments">All assessment bundles</Link>
    {result.error && <ErrorState error={result.error} retry={result.reload} />}
    {result.loading && !r && <Loading what="assessment" />}
    {r && <>
      <Card title="Evaluation validity"><Badge tone={r.eligible ? "neutral" : "warn"}>{r.eligible ? "Eligible within this bundle" : r.complete ? "Excluded from ranking" : "Incomplete assignment"}</Badge><p>{r.note}</p><KV rows={[["Finished episodes", `${r.coverage.finished}/${r.coverage.assigned}`], ["Assurance", humanize(r.commitment.assurance)], ["Predictive validity", "Not established"]]} /></Card>
      <Card title="Trading outcome"><KV rows={[["Median settled cash return", r.median_return == null ? "Withheld or ineligible" : fmtReturn(r.median_return)], ["Cash reference", "0%"]]} /></Card>
      {r.episodes.map(e => <Card key={e.slot} title={`Assigned episode ${e.slot}`}><KV rows={[["State", humanize(e.state)], ["Settled cash return", e.headline_return == null ? "Withheld" : fmtReturn(e.headline_return)], ["Prior service attempts", e.prior_attempts], ["Failed gates", e.failed_gates.map(humanize).join(", ") || "None"], ...Object.entries(e.status_dimensions).map(([key, value]): [string, string] => [humanize(key), humanize(value)])]} /></Card>)}
      <details><summary>Committed version and configuration digests</summary><JsonView value={r.commitment} open /></details>
    </>}
  </main>;
}

function CreateBundle({ onCreated }: { onCreated: () => void }) {
  const packs = useLoad(() => list<Pack>("/packs"), []);
  const [selected, setSelected] = useState<string[]>([]);
  const [label, setLabel] = useState("");
  const [profile, setProfile] = useState("controlled_v1");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const holdouts = packs.data?.filter(p => p.visibility === "holdout") ?? [];
  return <details className="card"><summary>Create a frozen bundle</summary>
    <p>Only unpublished, private episodes can be selected. Once created, this bundle cannot be edited. The default bankroll is one whole unit of the pack's cash asset.</p>
    {packs.error && <ErrorState error={packs.error} retry={packs.reload} />}
    {!holdouts.length && <p><Link to="/episodes">Import private holdouts</Link> before creating a bundle.</p>}
    <form className="form" onSubmit={async event => {
      event.preventDefault(); setBusy(true); setError(null);
      try { await post("/assessment-bundles", { label, pack_ids: selected, resource_profile_id: profile }); setSelected([]); setLabel(""); onCreated(); }
      catch (e) { setError(e); } finally { setBusy(false); }
    }}>
      <label className="field">Public bundle label<input required maxLength={80} value={label} onChange={e => setLabel(e.target.value)} /></label>
      <label className="field">Resource profile<select value={profile} onChange={e => setProfile(e.target.value)}><option value="controlled_v1">Controlled: 500 ms decision time</option><option value="adverse_execution_v1">Adverse: double submit delay, confirmations and gas</option></select></label>
      <fieldset><legend>Private episodes</legend>{holdouts.map(p => <label className="row" key={p.pack_id}><input type="checkbox" checked={selected.includes(p.pack_id)} onChange={e => setSelected(values => e.target.checked ? [...values, p.pack_id] : values.filter(v => v !== p.pack_id))} />{p.name}</label>)}</fieldset>
      {error != null && <ErrorState error={error} />}
      <button className="btn btn-primary" disabled={busy || !selected.length}>{busy ? "Freezing…" : "Freeze bundle"}</button>
    </form>
  </details>;
}
