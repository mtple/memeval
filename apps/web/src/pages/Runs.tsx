import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { EXAMPLES, RUNTIMES, get, list, post, type Agent, type Meta, type Pack, type Run, type Suite, type Usage } from "../api";
import { exportPolicy, fmtDate, fmtRaw, shortHash, unitLabel } from "../format";
import { useRole } from "../role";
import { Badge, Card, EmptyState, ErrorState, Loading, RunStateBadge, useLoad } from "../ui";

export default function Runs() {
  const [sp] = useSearchParams();
  const packFilter = sp.get("pack_id") ?? "";
  const agentFilter = sp.get("agent_id") ?? "";
  const q = new URLSearchParams();
  if (packFilter) q.set("pack_id", packFilter);
  if (agentFilter) q.set("agent_id", agentFilter);
  const runs = useLoad(() => list<Run>(`/runs${q.toString() ? `?${q}` : ""}`), [packFilter, agentFilter], 15000);
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
        <h1 style={{ margin: 0 }}>Runs</h1>
      </div>
      <p className="muted" style={{ maxWidth: "80ch" }}>
        Every attempt by every agent, newest first. A run is one agent trading through one recorded day. The result is its final ETH return after gas and fees; that is the number the leaderboard ranks. Click an agent for its history in plain words, or a result for the full breakdown.
      </p>
      {usage.data && role === "admin" && (
        <p className="muted small">
          Compute used today: {usage.data.today.runs} runs · {usage.data.today.cpu_seconds.toFixed(0)} CPU-s (cap {usage.data.caps.max_runs_per_day}/day). This month: {usage.data.month.cpu_seconds.toFixed(0)} of {usage.data.caps.max_cpu_seconds_per_month.toFixed(0)} CPU-s.
          {hosted && " Hosted mode: reference participants run inside the server request; external agents connect over HTTP or MCP."}
        </p>
      )}
      {role === "admin" && <RunSuite agents={agents.data ?? []} suites={suites.data ?? []} onCreated={runs.reload} runtimes={runtimes} hosted={hosted} />}
      <Card
        title={`Runs${packFilter ? ` on ${packById.get(packFilter)?.label ?? "one day"}` : ""}${agentFilter ? ` by ${agents.data?.find((a) => a.agent_id === agentFilter)?.name ?? "one agent"}` : ""}`}
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
            <Link to="/">Give an agent the join link</Link> and its runs appear here.
          </EmptyState>
        )}
        {runs.data && runs.data.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Agent</th>
                  <th>Day</th>
                  <th>What happened</th>
                  <th className="num">Result</th>
                  <th>Started</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {[...runs.data]
                  .sort((x, y) => (x.created_at < y.created_at ? 1 : -1))
                  .map((r) => {
                    const p = packById.get(r.pack_id);
                    const s = r.result_summary;
                    const live = r.state === "queued" || r.state === "running" || r.state === "paused";
                    const progress = r.live ? Math.round(((r.live.clock_ms ?? 0) / Math.max(1, r.live.duration_ms)) * 100) : p ? Math.round((r.clock_ms / Math.max(1, p.duration_ms)) * 100) : null;
                    const ranked = s?.primary_metric === "final_cash_return_v1" && s.headline_return !== null && s.headline_return !== undefined;
                    const ret = ranked ? Number(s!.headline_return) : null;
                    return (
                      <tr key={r.run_id}>
                        <td>
                          <Link to={`/agents/${r.agent_id}`}>{r.agent_name ?? agents.data?.find((a) => a.agent_id === r.agent_id)?.name ?? shortHash(r.agent_id)}</Link>
                        </td>
                        <td>
                          {r.pack_label ?? r.pack_name ?? shortHash(r.pack_id)}
                          {p?.kind === "practice" && <div className="muted small">practice, artificial</div>}
                        </td>
                        <td>
                          <RunStateBadge state={r.state} /> <span className="small">{describeState(r.state, progress, live)}</span>
                          {r.error && <div className="small muted">{r.error}</div>}
                          {s && !live && (
                            <div className="small muted">
                              {s.confirmed_fills} of {s.orders_total} orders filled{s.gas_total_raw && s.gas_total_raw !== "0" ? `, ${fmtRaw(s.gas_total_raw, s.numeraire_decimals)} ${unitLabel(s.numeraire)} gas` : ""}
                            </div>
                          )}
                        </td>
                        <td className={`num ret ${ret === null ? "" : ret > 0 ? "up" : ret < 0 ? "down" : ""}`}>
                          {ret === null ? <span className="muted">{live ? "in progress" : s ? "not ranked" : "no result"}</span> : `${ret > 0 ? "+" : ""}${(ret * 100).toFixed(2)}%`}
                          {ret !== null && <div className="muted small" style={{ fontFamily: "var(--sans)", fontWeight: 400 }}>final ETH return</div>}
                        </td>
                        <td className="small">{fmtDate(r.started_at ?? r.created_at)}</td>
                        <td className="small">
                          {r.has_report ? <Link to={`/runs/${r.run_id}/results`}>Full result</Link> : <Link to={`/runs/${r.run_id}`}>Watch</Link>}
                          {Boolean(r.launch) && (r.state === "queued" || r.state === "running") && hosted && role === "admin" && (
                            <>
                              {" "}
                              <button type="button" className="btn btn-small" disabled={executing === r.run_id} onClick={() => execute(r.run_id)}>
                                {executing === r.run_id ? "Running…" : "Execute"}
                              </button>
                            </>
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

/** What a run's state means, for someone who has never seen the state names. */
function describeState(state: string, progress: number | null, live: boolean): string {
  switch (state) {
    case "queued":
      return "waiting for the agent to connect";
    case "running":
      return progress === null ? "the agent is trading through the day" : `the agent is trading, ${progress}% of the day done`;
    case "paused":
      return "paused; the agent can resume";
    case "completed":
      return "finished the day";
    case "agent_failed":
      return "the agent stopped or failed before the day ended";
    case "environment_failed":
      return "the server hit an error; not the agent's fault";
    case "budget_exhausted":
      return "ran out of compute budget";
    case "aborted":
      return "cancelled before the day ended";
    default:
      return live ? "in progress" : state.replace(/_/g, " ");
  }
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
                {s.label} ({s.pack_count} episodes, {exportPolicy(s.mode)}){s.all_packs_imported ? "" : " . packs missing"}
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
                {a.name}
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
