import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { EXAMPLES, list, post, type Pack, type Run } from "../api";
import { connectSnippets } from "../connect";
import { buildNewRunBody, validateNewRun, type NewRunForm } from "../forms";
import { fmtDuration, fmtRaw } from "../format";
import { useRole } from "../role";
import { CopyButton, ErrorState, Loading, RunStateBadge, useLoad } from "../ui";

const EXAMPLE_TEXT: Record<string, string> = {
  cash_only: "never trades; shows what abstention costs",
  scheduled_basket: "buys a fixed basket on a schedule",
  random_actions: "random valid and invalid calls; stress-tests error handling",
  model_client: "asks an external model; needs configuration",
};

/** One screen: who drives the run, which episode, then either connection details or the result. */
export default function NewRun() {
  const packs = useLoad(() => list<Pack>("/packs"), []);
  const { role, meta } = useRole();
  const nav = useNavigate();
  const runnable = (packs.data ?? []).filter((p) => p.runnable);
  const [f, setF] = useState<NewRunForm>({ driver: "own", agent_name: "", agent_version: "1", pack_id: "", example: "scheduled_basket", mode: "practice", bankroll_raw: "1000000", agent_seed: "" });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<unknown>(null);
  const [created, setCreated] = useState<Run | null>(null);
  const [tab, setTab] = useState<"mcp" | "curl" | "python" | "typescript" | "env">("mcp");
  const form = { ...f, pack_id: f.pack_id || runnable[0]?.pack_id || "" };
  const pack = runnable.find((p) => p.pack_id === form.pack_id);
  const dec = pack?.summary?.numeraire_decimals;
  const problems = validateNewRun(form);
  const blocked = meta?.public_runs === false && role !== "admin";

  if (created?.session_credential) {
    const s = connectSnippets(created.session_credential);
    const code = tab === "mcp" ? s.mcpConfig : tab === "curl" ? s.curl : tab === "python" ? s.python : tab === "typescript" ? s.typescript : s.env;
    return (
      <main className="stack">
        <h1>Connect your agent</h1>
        <section className="connect" role="alert">
          <p>
            <strong>Run created.</strong> This session token is shown once and is not stored by this page. It works only for this run.
          </p>
          <dl className="kv">
            <div className="kv-row">
              <dt>Token</dt>
              <dd>
                <code id="session-token">{created.session_credential.token}</code> <CopyButton text={created.session_credential.token} />
              </dd>
            </div>
            <div className="kv-row">
              <dt>HTTP</dt>
              <dd>
                <code>{created.session_credential.commands_url}</code>
              </dd>
            </div>
            <div className="kv-row">
              <dt>MCP</dt>
              <dd>
                <code>{created.session_credential.mcp_url}</code>
              </dd>
            </div>
          </dl>
          <div className="tabs" role="group" aria-label="How to connect" style={{ marginTop: 10 }}>
            {(
              [
                ["mcp", "MCP config (OpenClaw, Hermes, Claude…)"],
                ["curl", "HTTP"],
                ["python", "Python SDK"],
                ["typescript", "TypeScript SDK"],
                ["env", "Env vars"],
              ] as const
            ).map(([k, label]) => (
              <button key={k} type="button" className="btn btn-small" aria-pressed={tab === k} onClick={() => setTab(k)}>
                {label}
              </button>
            ))}
          </div>
          <pre>{code}</pre>
          <CopyButton text={code} label="Copy" />
        </section>
        <p>
          The agent has fourteen tools (markets, candles, quotes, orders, portfolio, clock) and every reply carries the same envelope with a quality block. Reading the run's <Link to={`/runs/${created.run_id}`}>live page</Link> shows each call as it
          arrives. The result appears on <Link to="/">Results</Link> when the agent calls <code>session.finish</code> or the episode ends.
        </p>
        <div className="row">
          <Link to={`/runs/${created.run_id}`} className="btn btn-primary">
            Watch the run
          </Link>
          <button type="button" className="btn" onClick={() => setCreated(null)}>
            Start another
          </button>
        </div>
      </main>
    );
  }

  return (
    <main className="stack">
      <h1>New run</h1>
      {packs.error && <ErrorState error={packs.error} retry={packs.reload} />}
      {packs.loading && !packs.data && <Loading what="episodes" />}
      {blocked && <p className="notice">Starting runs on this server is reserved for its operator. Sign in at the top right.</p>}
      {packs.data && runnable.length === 0 && <p className="notice">No runnable episodes on this server yet.</p>}
      <form
        className="form"
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          setErr(null);
          try {
            const run = await post<Run>("/runs", buildNewRunBody(form));
            if (run.session_credential) setCreated(run);
            else nav(run.has_report ? `/runs/${run.run_id}/results` : `/runs/${run.run_id}`);
          } catch (x) {
            setErr(x);
          } finally {
            setBusy(false);
          }
        }}
      >
        <fieldset className="choice" style={{ border: 0, padding: 0, margin: 0 }}>
          <legend className="muted small" style={{ marginBottom: 4 }}>
            Who drives the run?
          </legend>
          <label>
            <input type="radio" name="driver" checked={f.driver === "own"} onChange={() => setF({ ...f, driver: "own" })} />
            <span>
              <strong>My own agent</strong>
              <br />
              <span className="muted small">
                Bankr, OpenClaw, Hermes, a script: anything that can call HTTP or MCP tools. You get a token to paste in. (An agent that reads skill files can skip this screen entirely: give it <code>{`${meta?.gateway_url ?? ""}/skill.md`}</code>.)
              </span>
            </span>
          </label>
          <label>
            <input type="radio" name="driver" checked={f.driver === "reference"} onChange={() => setF({ ...f, driver: "reference" })} />
            <span>
              <strong>A reference participant</strong>
              <br />
              <span className="muted small">The server runs a simple included strategy so you have something to compare against.</span>
            </span>
          </label>
        </fieldset>
        {f.driver === "own" ? (
          <div className="row">
            <label className="field" style={{ flex: "2 1 240px" }}>
              Agent name
              <input id="new-agent-name" required maxLength={64} value={f.agent_name} onChange={(e) => setF({ ...f, agent_name: e.target.value })} placeholder="e.g. bankr-momentum" />
            </label>
            <label className="field" style={{ flex: "1 1 120px" }}>
              Version
              <input maxLength={32} value={f.agent_version} onChange={(e) => setF({ ...f, agent_version: e.target.value })} />
            </label>
          </div>
        ) : (
          <label className="field">
            Reference participant
            <select value={f.example} onChange={(e) => setF({ ...f, example: e.target.value })}>
              {EXAMPLES.filter((x) => x !== "model_client").map((x) => (
                <option key={x} value={x}>
                  {x} — {EXAMPLE_TEXT[x]}
                </option>
              ))}
            </select>
          </label>
        )}
        <label className="field">
          Episode
          <select id="new-episode" required value={form.pack_id} onChange={(e) => setF({ ...f, pack_id: e.target.value })}>
            {runnable.map((p) => (
              <option key={p.pack_id} value={p.pack_id}>
                {p.name} · {fmtDuration(p.duration_ms, p.is_full_week)} · {p.summary?.pools_executable ?? "?"} tradable pools · {p.origin === "generated_fixture" ? "generated" : p.origin.replace(/_/g, " ")}
              </option>
            ))}
          </select>
          {pack?.summary?.scenario && <span className="small muted">{pack.summary.scenario}</span>}
        </label>
        <details className="more">
          <summary>More options (mode, bankroll, seed)</summary>
          <div className="row">
            <label className="field">
              Mode
              <select value={f.mode} onChange={(e) => setF({ ...f, mode: e.target.value })}>
                <option value="practice">practice (episode dates may be revealed after)</option>
                <option value="sealed">sealed (no dates, no trajectory disclosure)</option>
              </select>
            </label>
            <label className="field">
              Bankroll (raw units{pack ? `, ${pack.summary.numeraire_alias} has ${dec} decimals` : ""})
              <input inputMode="numeric" value={f.bankroll_raw} onChange={(e) => setF({ ...f, bankroll_raw: e.target.value.trim() })} />
              <span className="small muted">{/^\d+$/.test(f.bankroll_raw) && dec !== undefined ? `= ${fmtRaw(f.bankroll_raw, dec)} ${pack?.summary.numeraire_alias}` : "digits only"}</span>
            </label>
            <label className="field">
              Agent seed (stochastic participants)
              <input value={f.agent_seed} onChange={(e) => setF({ ...f, agent_seed: e.target.value })} style={{ width: 100 }} />
            </label>
          </div>
        </details>
        <div className="row">
          <button id="new-submit" className="btn btn-primary" disabled={busy || blocked || problems.length > 0} title={problems.join("; ")}>
            {busy ? (f.driver === "reference" ? "Running…" : "Creating…") : f.driver === "reference" ? "Run it" : "Create run and get token"}
          </button>
          {problems.length > 0 && <span className="muted small">{problems.join(" · ")}</span>}
          {created && !created.session_credential && (
            <span>
              Created <Link to={`/runs/${created.run_id}`}>{created.run_id}</Link> <RunStateBadge state={created.state} />
            </span>
          )}
        </div>
      </form>
      {err !== null && <ErrorState error={err} />}
      <p className="muted small">
        Practice runs are not a benchmark claim: generated episodes are artificial and predictive validity is not established. Starting a run counts against the server's daily cap{meta?.rate_limit_per_hour_per_ip ? ` and a limit of ${meta.rate_limit_per_hour_per_ip} runs per hour per address` : ""}.
      </p>
    </main>
  );
}
