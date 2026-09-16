import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { get, list, post, type Agent, type Comparison, type Stats, type Suite } from "../api";
import { fmtDate, fmtPct, fmtReturn, shortHash } from "../format";
import { Badge, Card, EmptyState, ErrorState, JsonView, KV, Loading, RunStateBadge, useLoad } from "../ui";

export default function Compare() {
  const { id } = useParams();
  const nav = useNavigate();
  const comps = useLoad(() => list<Comparison>("/comparisons"), []);
  const agents = useLoad(() => list<Agent>("/agents"), []);
  const suites = useLoad(() => list<Suite>("/suites"), []);
  const detail = useLoad(() => (id ? get<Comparison>(`/comparisons/${id}`) : Promise.resolve(null)), [id]);

  return (
    <main className="stack">
      <h1>Compare</h1>
      <div className="grid-2">
        <NewComparison agents={agents.data ?? []} suites={suites.data ?? []} onCreated={(c) => { comps.reload(); nav(`/compare/${c.comparison_id}`); }} />
        <Card title="Previous comparisons" actions={<button type="button" className="btn btn-small" onClick={comps.reload}>Refresh</button>}>
          {comps.error && <ErrorState error={comps.error} retry={comps.reload} />}
          {comps.loading && !comps.data && <Loading what="comparisons" />}
          {comps.data && comps.data.length === 0 && <EmptyState title="No comparisons yet.">Create one with two agents that have runs on the same suite or packs.</EmptyState>}
          {comps.data && comps.data.length > 0 && (
            <ul className="gate-list">
              {comps.data.map((c) => (
                <li key={c.comparison_id}>
                  <Link to={`/compare/${c.comparison_id}`} className="mono">
                    {shortHash(c.comparison_id, 14)}
                  </Link>{" "}
                  {c.agents?.a?.name} vs {c.agents?.b?.name} {c.suite_id && <span className="muted">on {c.suite_id}</span>} <span className="muted small">{fmtDate(c.created_at)}</span>{" "}
                  {c.warnings?.length > 0 && <Badge tone="warn">{c.warnings.length} warnings</Badge>}
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
      {id && detail.error && <ErrorState error={detail.error} retry={detail.reload} />}
      {id && detail.loading && !detail.data && <Loading what="comparison" />}
      {detail.data && <ComparisonView c={detail.data} />}
    </main>
  );
}

function NewComparison({ agents, suites, onCreated }: { agents: Agent[]; suites: Suite[]; onCreated: (c: Comparison) => void }) {
  const [f, setF] = useState({ suite_id: "", agent_a: "", agent_b: "" });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<unknown>(null);
  return (
    <Card title="New comparison">
      {agents.length < 2 && (
        <p className="notice">
          Two registered agents are required. <Link to="/agents">Agent setup</Link>.
        </p>
      )}
      <form
        className="form"
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          setErr(null);
          try {
            const body: Record<string, string> = { agent_a: f.agent_a, agent_b: f.agent_b };
            if (f.suite_id) body.suite_id = f.suite_id;
            onCreated(await post<Comparison>("/comparisons", body));
          } catch (x) {
            setErr(x);
          } finally {
            setBusy(false);
          }
        }}
      >
        <label className="field">
          Suite (optional; otherwise all shared episodes)
          <select value={f.suite_id} onChange={(e) => setF({ ...f, suite_id: e.target.value })}>
            <option value="">any</option>
            {suites.map((s) => (
              <option key={s.suite_id} value={s.suite_id}>
                {s.suite_id}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          Agent A
          <select required value={f.agent_a} onChange={(e) => setF({ ...f, agent_a: e.target.value })}>
            <option value="">select…</option>
            {agents.map((a) => (
              <option key={a.agent_id} value={a.agent_id}>
                {a.name} v{a.version}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          Agent B
          <select required value={f.agent_b} onChange={(e) => setF({ ...f, agent_b: e.target.value })}>
            <option value="">select…</option>
            {agents.map((a) => (
              <option key={a.agent_id} value={a.agent_id}>
                {a.name} v{a.version}
              </option>
            ))}
          </select>
        </label>
        <div>
          <button className="btn btn-primary" disabled={busy || !f.agent_a || !f.agent_b || f.agent_a === f.agent_b}>
            {busy ? "Comparing…" : "Compare"}
          </button>
          {f.agent_a && f.agent_a === f.agent_b && <span className="muted small"> choose two different agents</span>}
        </div>
      </form>
      {err !== null && <ErrorState error={err} />}
    </Card>
  );
}

const diffStr = (v: string | number | null | undefined, pct = true) => (v === null || v === undefined ? "n/a" : pct ? fmtPct(v, 3) : String(v));

function RunCells({ runs }: { runs: Comparison["per_episode"][number]["runs_a"] }) {
  if (!runs?.length) return <span className="muted">no run</span>;
  return (
    <ul className="plain" style={{ paddingLeft: 0, listStyle: "none" }}>
      {runs.map((r) => (
        <li key={r.run_id} className="small">
          <Link to={`/runs/${r.run_id}`} className="mono">
            {shortHash(r.run_id, 8)}
          </Link>{" "}
          <RunStateBadge state={r.state} />{" "}
          <span className="mono">{r.headline_return === null ? "null" : fmtReturn(r.headline_return)}</span>
          {!r.valuation_complete && <Badge tone="warn">valuation incomplete</Badge>}
          {r.unresolved_orders > 0 && <Badge tone="warn">{r.unresolved_orders} unresolved</Badge>}
          {r.unpriced_inventory > 0 && <Badge tone="bad">{r.unpriced_inventory} unpriced</Badge>}
          {r.error && <div className="muted">{r.error}</div>}
        </li>
      ))}
    </ul>
  );
}

function StatsRow({ label, s, pct }: { label: string; s: Stats | undefined; pct: boolean }) {
  if (!s) return <p className="muted small">{label}: not computed (no paired episodes).</p>;
  return (
    <KV
      rows={[
        [label, `median ${diffStr(s.median, pct)} · mean ${diffStr(s.mean, pct)} · min ${diffStr(s.min, pct)} · max ${diffStr(s.max, pct)}`],
        ["A better / B better", `${s.a_better_count} / ${s.b_better_count}`],
      ]}
    />
  );
}

function ComparisonView({ c }: { c: Comparison }) {
  return (
    <>
      {c.warnings?.length > 0 && (
        <div className="notice" role="alert">
          <strong>Warnings ({c.warnings.length})</strong>
          <ul className="plain">
            {c.warnings.map((w, i) => (
              <li key={i} className="mono">
                {w}
              </li>
            ))}
          </ul>
        </div>
      )}
      <Card title={`Per episode: ${c.agents.a.name} (A) vs ${c.agents.b.name} (B)`}>
        <p className="muted small">All attempted runs are shown, including failed ones. Diffs are A − B and only exist for paired episodes.</p>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Episode</th>
                <th>A runs (state, return)</th>
                <th>B runs (state, return)</th>
                <th>Paired</th>
                <th className="num">Return diff A−B</th>
                <th className="num">Gas diff A−B (raw)</th>
                <th className="num">Drawdown diff A−B</th>
              </tr>
            </thead>
            <tbody>
              {c.per_episode.map((e) => (
                <tr key={e.episode_label}>
                  <td>{e.episode_label}</td>
                  <td>
                    <RunCells runs={e.runs_a} />
                  </td>
                  <td>
                    <RunCells runs={e.runs_b} />
                  </td>
                  <td>{e.paired ? <Badge tone="ok">paired</Badge> : <Badge tone="warn">unpaired</Badge>}</td>
                  <td className="num">{diffStr(e.return_diff_a_minus_b)}</td>
                  <td className="num">{e.gas_diff_a_minus_b_raw ?? "n/a"}</td>
                  <td className="num">{diffStr(e.drawdown_diff_a_minus_b)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
      <div className="grid-2">
        <Card title="Summary">
          <KV
            rows={[
              ["Episodes total / paired", `${c.summary.episodes_total} / ${c.summary.episodes_paired}`],
              ["Runs attempted A / B", `${c.summary.runs_attempted_a} / ${c.summary.runs_attempted_b}`],
              ["Runs not completed A / B", `${c.summary.runs_not_completed_a} / ${c.summary.runs_not_completed_b}`],
            ]}
          />
          <StatsRow label="Return diff" s={c.summary.return_diff} pct />
          <StatsRow label="Drawdown diff" s={c.summary.drawdown_diff} pct />
          {c.summary.gas_diff_raw ? <KV rows={[["Gas diff (raw)", Object.entries(c.summary.gas_diff_raw).map(([k, v]) => `${k} ${v}`).join(" · ")]]} /> : <p className="muted small">Gas diff: not computed.</p>}
        </Card>
        <Card title="Evidence and identity">
          <KV
            rows={[
              ["Unique calendar periods", c.evidence_counts?.unique_calendar_periods],
              ["Chains", c.evidence_counts?.chains],
              ["Stochastic trials A / B", `${c.evidence_counts?.stochastic_trials_a} / ${c.evidence_counts?.stochastic_trials_b}`],
              ["Suite", c.suite_id ?? "n/a"],
              ["Suite fingerprint", <span className="mono">{c.suite_fingerprint ?? "n/a"}</span>],
              ["Agent A", `${c.agents.a.name} v${c.agents.a.version} (${c.agents.a.runtime})`],
              ["A fingerprint", <span className="mono">{c.agents.a.fingerprint}</span>],
              ["Agent B", `${c.agents.b.name} v${c.agents.b.version} (${c.agents.b.runtime})`],
              ["B fingerprint", <span className="mono">{c.agents.b.fingerprint}</span>],
              ["Created", fmtDate(c.created_at)],
            ]}
          />
        </Card>
      </div>
      <Card title="Statement">
        <p className="statement">{c.statement}</p>
      </Card>
      <JsonView value={c} />
    </>
  );
}
