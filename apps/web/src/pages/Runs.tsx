import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { EXAMPLES, RUNTIMES, get, list, post, type Agent, type Meta, type Pack, type Run, type Suite, type Usage } from "../api";
import { fmtDate, fmtRel, fmtRaw, shortHash } from "../format";
import { useRole } from "../role";
import { Badge, Card, EmptyState, ErrorState, Loading, RunStateBadge, useLoad } from "../ui";

export default function Runs() {
  const [sp] = useSearchParams();
  const packFilter = sp.get("pack_id") ?? "";
  const agentFilter = sp.get("agent_id") ?? "";
  const q = new URLSearchParams();
  if (packFilter) q.set("pack_id", packFilter);
  if (agentFilter) q.set("agent_id", agentFilter);
  const runs = useLoad(() => list<Run>(`/runs${q.toString() ? `?${q}` : ""}`), [packFilter, agentFilter], 5000);
  const agents = useLoad(() => list<Agent>("/agents"), []);
  const packs = useLoad(() => list<Pack>("/packs"), []);
  const suites = useLoad(() => list<Suite>("/suites"), []);
  const meta = useLoad(() => get<Meta>("/meta"), []);
  const usage = useLoad(() => get<Usage>("/usage"), [], 10000);
  const packById = new Map((packs.data ?? []).map((p) => [p.pack_id, p]));
  const runtimes = (meta.data?.runtimes_available ?? [...RUNTIMES]) as string[];
  const hosted = Boolean(meta.data?.hosted);
  const { role } = useRole();
  const [executing, setExecuting] = useState<string | null>(null);
  const execute = async (runId: string) => {
    setExecuting(runId);
    try {
      await post(`/runs/${encodeURIComponent(runId)}/execute`);
    } finally {
      setExecuting(null);
      runs.reload();
    }
  };

  return (
    <main className="stack">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h1 style={{ margin: 0 }}>All runs</h1>
        <Link to="/new" className="btn btn-small btn-primary">
          + New run
        </Link>
      </div>
      {usage.data && (
        <p className="muted small">
          Compute used today: {usage.data.today.runs} runs · {usage.data.today.cpu_seconds.toFixed(0)} CPU-s (cap {usage.data.caps.max_runs_per_day}/day). This month: {usage.data.month.cpu_seconds.toFixed(0)} of {usage.data.caps.max_cpu_seconds_per_month.toFixed(0)} CPU-s.
          {hosted && " Hosted mode: reference participants run inside the server request; external agents connect over HTTP or MCP."}
        </p>
      )}
      {role === "admin" && <RunSuite agents={agents.data ?? []} suites={suites.data ?? []} onCreated={runs.reload} runtimes={runtimes} hosted={hosted} />}
      <Card
        title={`Runs${packFilter ? ` for pack ${shortHash(packFilter)}` : ""}${agentFilter ? ` for agent ${shortHash(agentFilter)}` : ""}`}
        actions={
          <>
            {(packFilter || agentFilter) && (
              <Link to="/runs" className="btn btn-small">
                Clear filter
              </Link>
            )}
            <button type="button" className="btn btn-small" onClick={runs.reload}>
              Refresh
            </button>
          </>
        }
      >
        {runs.error && <ErrorState error={runs.error} retry={runs.reload} />}
        {runs.loading && !runs.data && <Loading what="runs" />}
        {runs.data && runs.data.length === 0 && (
          <EmptyState title="No runs yet.">
            <Link to="/new">Start one</Link>: name your agent, pick an episode, get a token.
          </EmptyState>
        )}
        {runs.data && runs.data.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Run</th>
                  <th>State</th>
                  <th>Pack</th>
                  <th>Agent</th>
                  <th>Mode</th>
                  <th>Clock</th>
                  <th className="num">Bankroll</th>
                  <th>Created</th>
                  <th>Report</th>
                </tr>
              </thead>
              <tbody>
                {runs.data.map((r) => {
                  const p = packById.get(r.pack_id);
                  return (
                    <tr key={r.run_id}>
                      <td>
                        <Link to={`/runs/${r.run_id}`} className="mono">
                          {shortHash(r.run_id, 14)}
                        </Link>
                        {r.suite_id && <div className="muted small">suite {r.suite_id}</div>}
                      </td>
                      <td>
                        <RunStateBadge state={r.state} />
                        {r.error && <div className="small">{r.error}</div>}
                      </td>
                      <td>{r.pack_name || <span className="mono">{shortHash(r.pack_id)}</span>}</td>
                      <td className="small">{r.agent_name ?? agents.data?.find((a) => a.agent_id === r.agent_id)?.name ?? <span className="mono">{shortHash(r.agent_id)}</span>}</td>
                      <td>
                        {r.mode} <span className="muted small">{r.isolation}</span>
                      </td>
                      <td className="mono">{fmtRel(r.live?.clock_ms ?? r.clock_ms)}</td>
                      <td className="num">{p ? fmtRaw(r.bankroll_raw, p.summary?.numeraire_decimals) : r.bankroll_raw}</td>
                      <td className="small">{fmtDate(r.created_at)}</td>
                      <td>
                        {r.has_report ? (
                          <Link to={`/runs/${r.run_id}/results`}>Results</Link>
                        ) : r.launch && (r.state === "queued" || r.state === "running") && hosted ? (
                          <button type="button" className="btn btn-small" disabled={executing === r.run_id} onClick={() => execute(r.run_id)}>
                            {executing === r.run_id ? "Running…" : "Execute"}
                          </button>
                        ) : (
                          <span className="muted">n/a</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </main>
  );
}

function RunSuite({ agents, suites, onCreated, runtimes, hosted }: { agents: Agent[]; suites: Suite[]; onCreated: () => void; runtimes: string[]; hosted: boolean }) {
  const [f, setF] = useState({ suite_id: "", agent_id: "", example: "cash_only", runtime: "python", agent_seed: "" });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<unknown>(null);
  const [res, setRes] = useState<{ suite_run_id: string; run_ids: string[] } | null>(null);
  const suite = suites.find((s) => s.suite_id === f.suite_id);
  return (
    <Card title="Run suite">
      {suites.length === 0 && <p className="muted">No suites defined on the server.</p>}
      <form
        className="form"
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          setErr(null);
          setRes(null);
          const body: Record<string, unknown> = { agent_id: f.agent_id, launch: { kind: "example", name: f.example, runtime: f.runtime }, wait: false };
          if (f.agent_seed.trim()) body.agent_seed = f.agent_seed.trim();
          try {
            setRes(await post(`/suites/${encodeURIComponent(f.suite_id)}/runs`, body));
            onCreated();
          } catch (x) {
            setErr(x);
          } finally {
            setBusy(false);
          }
        }}
      >
        <label className="field">
          Suite
          <select required value={f.suite_id} onChange={(e) => setF({ ...f, suite_id: e.target.value })}>
            <option value="">select…</option>
            {suites.map((s) => (
              <option key={s.suite_id} value={s.suite_id}>
                {s.suite_id} ({s.pack_count} packs, {s.mode}){s.all_packs_imported ? "" : " . packs missing"}
              </option>
            ))}
          </select>
        </label>
        {suite && (
          <p className="small muted">
            {suite.description} · isolation {suite.isolation} · fingerprint <span className="mono">{shortHash(suite.fingerprint)}</span> · bankroll raw <span className="mono">{suite.bankroll_raw}</span>
            {!suite.all_packs_imported && (
              <>
                {" "}
                <Badge tone="warn">not all packs imported</Badge>
              </>
            )}
          </p>
        )}
        <label className="field">
          Agent
          <select required value={f.agent_id} onChange={(e) => setF({ ...f, agent_id: e.target.value })}>
            <option value="">select…</option>
            {agents.map((a) => (
              <option key={a.agent_id} value={a.agent_id}>
                {a.name} v{a.version}
              </option>
            ))}
          </select>
        </label>
        <div className="row">
          <label className="field">
            Example
            <select value={f.example} onChange={(e) => setF({ ...f, example: e.target.value })}>
              {EXAMPLES.map((x) => (
                <option key={x}>{x}</option>
              ))}
            </select>
          </label>
          <label className="field">
            Runtime
            <select value={f.runtime} onChange={(e) => setF({ ...f, runtime: e.target.value })}>
              {runtimes.map((x) => (
                <option key={x}>{x}</option>
              ))}
            </select>
          </label>
          <label className="field">
            Seed
            <input inputMode="numeric" value={f.agent_seed} onChange={(e) => setF({ ...f, agent_seed: e.target.value })} style={{ width: 90 }} />
          </label>
        </div>
        <div className="row">
          <button className="btn btn-primary" disabled={busy || !f.suite_id || !f.agent_id}>
            {busy ? "Starting…" : hosted ? "Queue suite (then Execute each run)" : "Start suite (background)"}
          </button>
          {res && (
            <span className="small">
              Suite run <span className="mono">{shortHash(res.suite_run_id)}</span>: {res.run_ids.length} runs queued.
            </span>
          )}
        </div>
      </form>
      {err !== null && <ErrorState error={err} />}
    </Card>
  );
}
