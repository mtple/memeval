import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { EXAMPLES, RUNTIMES, list, post, type Agent, type Pack, type Run, type Suite } from "../api";
import { fmtDate, fmtRel, fmtRaw, shortHash } from "../format";
import { Badge, Card, CopyButton, EmptyState, ErrorState, Loading, RunStateBadge, useLoad } from "../ui";

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
  const packById = new Map((packs.data ?? []).map((p) => [p.pack_id, p]));

  return (
    <main className="stack">
      <h1>Run</h1>
      <div className="grid-2">
        <CreateRun agents={agents.data ?? []} packs={packs.data ?? []} onCreated={runs.reload} />
        <RunSuite agents={agents.data ?? []} suites={suites.data ?? []} onCreated={runs.reload} />
      </div>
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
        {runs.data && runs.data.length === 0 && <EmptyState title="No runs yet.">Create one above. You need at least one registered agent and one runnable pack.</EmptyState>}
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
                      <td className="mono small">{agents.data?.find((a) => a.agent_id === r.agent_id)?.name ?? shortHash(r.agent_id)}</td>
                      <td>
                        {r.mode} <span className="muted small">{r.isolation}</span>
                      </td>
                      <td className="mono">{fmtRel(r.live?.clock_ms ?? r.clock_ms)}</td>
                      <td className="num">{p ? fmtRaw(r.bankroll_raw, p.summary?.numeraire_decimals) : r.bankroll_raw}</td>
                      <td className="small">{fmtDate(r.created_at)}</td>
                      <td>{r.has_report ? <Link to={`/runs/${r.run_id}/results`}>Results</Link> : <span className="muted">—</span>}</td>
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

function CreateRun({ agents, packs, onCreated }: { agents: Agent[]; packs: Pack[]; onCreated: () => void }) {
  const runnable = packs.filter((p) => p.runnable);
  const [f, setF] = useState({ agent_id: "", pack_id: "", mode: "practice", bankroll_raw: "", isolation: "practice", launchKind: "example", example: "cash_only", runtime: "python", agent_seed: "" });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<unknown>(null);
  const [created, setCreated] = useState<Run | null>(null);
  const pack = runnable.find((p) => p.pack_id === f.pack_id);
  const dec = pack?.summary?.numeraire_decimals;
  const bankrollOk = /^\d+$/.test(f.bankroll_raw);

  return (
    <Card title="Create run">
      {agents.length === 0 && (
        <p className="notice">
          No agents registered. <Link to="/agents">Register one on Agent setup</Link>.
        </p>
      )}
      {runnable.length === 0 && (
        <p className="notice">
          No runnable packs. <Link to="/episodes">Import a pack on Episodes</Link>.
        </p>
      )}
      <form
        className="form cols"
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          setErr(null);
          setCreated(null);
          const body: Record<string, unknown> = { agent_id: f.agent_id, pack_id: f.pack_id, mode: f.mode, bankroll_raw: f.bankroll_raw, isolation: f.isolation };
          if (f.launchKind === "example") body.launch = { kind: "example", name: f.example, runtime: f.runtime };
          if (f.agent_seed.trim()) body.agent_seed = Number(f.agent_seed);
          try {
            setCreated(await post<Run>("/runs", body));
            onCreated();
          } catch (x) {
            setErr(x);
          } finally {
            setBusy(false);
          }
        }}
      >
        <label className="field">
          Agent
          <select required value={f.agent_id} onChange={(e) => setF({ ...f, agent_id: e.target.value })}>
            <option value="">select…</option>
            {agents.map((a) => (
              <option key={a.agent_id} value={a.agent_id}>
                {a.name} v{a.version} ({a.runtime}){a.compatibility?.compatible ? "" : " — incompatible"}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          Pack (runnable only)
          <select required value={f.pack_id} onChange={(e) => setF({ ...f, pack_id: e.target.value })}>
            <option value="">select…</option>
            {runnable.map((p) => (
              <option key={p.pack_id} value={p.pack_id}>
                {p.name} — {p.chain}, {p.use_status}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          Mode
          <select value={f.mode} onChange={(e) => setF({ ...f, mode: e.target.value })}>
            <option value="practice">practice</option>
            <option value="sealed">sealed</option>
          </select>
        </label>
        <label className="field">
          Isolation
          <select value={f.isolation} onChange={(e) => setF({ ...f, isolation: e.target.value })}>
            <option value="practice">practice</option>
            <option value="sealed">sealed</option>
            <option value="none">none</option>
          </select>
        </label>
        <label className="field">
          Bankroll (raw atomic units{pack ? `, ${pack.summary.numeraire_alias} has ${dec} decimals` : ""})
          <input required inputMode="numeric" value={f.bankroll_raw} onChange={(e) => setF({ ...f, bankroll_raw: e.target.value.trim() })} placeholder="e.g. 1000000000" aria-invalid={f.bankroll_raw !== "" && !bankrollOk} />
          <span className="small">{bankrollOk && dec !== undefined ? `= ${fmtRaw(f.bankroll_raw, dec)} ${pack?.summary.numeraire_alias}` : f.bankroll_raw ? "digits only" : ""}</span>
        </label>
        <label className="field">
          Agent seed (optional integer)
          <input inputMode="numeric" value={f.agent_seed} onChange={(e) => setF({ ...f, agent_seed: e.target.value })} />
        </label>
        <label className="field">
          Launch
          <select value={f.launchKind} onChange={(e) => setF({ ...f, launchKind: e.target.value })}>
            <option value="example">included reference agent (server launches it)</option>
            <option value="external">external client (no launch; session credential returned once)</option>
          </select>
        </label>
        {f.launchKind === "example" ? (
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
                {RUNTIMES.map((x) => (
                  <option key={x}>{x}</option>
                ))}
              </select>
            </label>
          </div>
        ) : (
          <p className="muted small">The run waits for your client to connect with the session token.</p>
        )}
        <div className="span2 row">
          <button className="btn btn-primary" disabled={busy || !bankrollOk || !f.agent_id || !f.pack_id}>
            {busy ? "Creating…" : "Create run"}
          </button>
          {created && (
            <span>
              Created <Link to={`/runs/${created.run_id}`} className="mono">{shortHash(created.run_id, 14)}</Link> <RunStateBadge state={created.state} />
            </span>
          )}
        </div>
      </form>
      {err !== null && <ErrorState error={err} />}
      {created?.session_credential && (
        <div className="credential" role="alert">
          <strong>Session credential — shown only once.</strong> It is not stored by this interface and cannot be retrieved again; copy it now.
          <dl className="kv" style={{ marginTop: 6 }}>
            <div className="kv-row">
              <dt>MARKET_REPLAY_TOKEN</dt>
              <dd>
                <code>{created.session_credential.token}</code> <CopyButton text={created.session_credential.token} />
              </dd>
            </div>
            <div className="kv-row">
              <dt>MARKET_REPLAY_URL</dt>
              <dd>
                <code>{created.session_credential.gateway_url}</code> <CopyButton text={created.session_credential.gateway_url} />
              </dd>
            </div>
            <div className="kv-row">
              <dt>Commands URL</dt>
              <dd>
                <code>{created.session_credential.commands_url}</code>
              </dd>
            </div>
          </dl>
          <CopyButton
            label="Copy as env exports"
            text={`export MARKET_REPLAY_URL=${created.session_credential.gateway_url}\nexport MARKET_REPLAY_TOKEN=${created.session_credential.token}`}
          />
        </div>
      )}
    </Card>
  );
}

function RunSuite({ agents, suites, onCreated }: { agents: Agent[]; suites: Suite[]; onCreated: () => void }) {
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
          if (f.agent_seed.trim()) body.agent_seed = Number(f.agent_seed);
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
                {s.suite_id} ({s.pack_count} packs, {s.mode}){s.all_packs_imported ? "" : " — packs missing"}
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
              {RUNTIMES.map((x) => (
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
            {busy ? "Starting…" : "Start suite (background)"}
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
