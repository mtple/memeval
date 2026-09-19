import { useState } from "react";
import { Link } from "react-router-dom";
import { EXAMPLES, RUNTIMES, get, list, post, type Agent, type Meta } from "../api";
import { fmtDate, shortHash } from "../format";
import { Badge, Card, CopyButton, EmptyState, ErrorState, JsonView, KV, Loading, StrList, useLoad } from "../ui";

const PY_SNIPPET = `# pip install -e sdk/python   (package: market_replay_client)
# env: MARKET_REPLAY_URL=<gateway_url>  MARKET_REPLAY_TOKEN=<session token>
from market_replay_client import client_from_env

c = client_from_env()
info = c.describe()
duration = info["episode"]["duration_ms"]
while True:
    adv = c.advance(min(c.clock_ms + 6 * 3_600_000, duration))
    if adv["episode_ended"]:
        break
pf = c.portfolio()
result = c.finish()
print(result["clock_ms"], pf["valuation"]["model_equity_raw"])  # raw strings, never floats`;

const TS_SNIPPET = `// node --experimental-strip-types my_agent.ts
// env: MARKET_REPLAY_URL=<gateway_url>  MARKET_REPLAY_TOKEN=<session token>
import { clientFromEnv } from "sdk/typescript/src/index.ts";

const c = clientFromEnv();
const info = (await c.describe()) as { episode: { duration_ms: number } };
for (;;) {
  const adv = await c.advance(Math.min(c.clockMs + 6 * 3_600_000, info.episode.duration_ms));
  if (adv.episode_ended) break;
}
const pf = await c.portfolio();
const result = await c.finish();
console.log(result.clock_ms, pf.valuation.model_equity_raw); // raw strings, use BigInt`;

const ENVELOPE = `POST /agent/v1/commands
Authorization: Bearer <session token>
Content-Type: application/json

{"request_id": "req_1", "tool": "session.describe", "arguments": {}, "session_id": "<after first call>"}

→ {"request_id", "session_id", "clock_ms", "status": "ok"|"error", "data": {...}|null,
   "quality": {"completeness", "availability_basis", "observed_through_ms", "stale", "warnings": []},
   "error": {"code", "message", "details"}|null}`;

export default function Agents() {
  const agents = useLoad(() => list<Agent>("/agents"), []);
  const meta = useLoad(() => get<Meta>("/meta"), []);
  const [snippet, setSnippet] = useState<"python" | "typescript">("python");

  return (
    <main className="stack">
      <h1>Agents</h1>
      <SkillCard base={meta.data?.gateway_url ?? window.location.origin} />
      <p className="muted">
        Every agent that has run here, by name. The name is the agent: all of its runs list under it. Nothing is registered by hand — an agent that follows the skill enrolls itself.
      </p>
      <Card title="Registered agents" actions={<button type="button" className="btn btn-small" onClick={agents.reload}>Refresh</button>}>
        {agents.error && <ErrorState error={agents.error} retry={agents.reload} />}
        {agents.loading && !agents.data && <Loading what="agents" />}
        {agents.data && agents.data.length === 0 && (
          <EmptyState title="No agents yet.">
            <Link to="/">Give an agent the join link</Link> and it appears here.
          </EmptyState>
        )}
        {agents.data && agents.data.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Runtime</th>
                  <th>Fingerprint</th>
                  <th>Capabilities</th>
                  <th>Compatibility</th>
                  <th>Created</th>
                </tr>
              </thead>
              <tbody>
                {agents.data.map((a) => (
                  <tr key={a.agent_id}>
                    <td>
                      {a.name}
                      <div className="muted small mono">{a.agent_id}</div>
                    </td>
                    <td>{a.runtime}</td>
                    <td className="mono" title={a.fingerprint}>
                      {shortHash(a.fingerprint, 16)}
                    </td>
                    <td className="small">{a.capabilities?.join(", ") || <span className="muted">none</span>}</td>
                    <td>
                      {a.compatibility?.compatible ? (
                        <Badge tone="ok">compatible</Badge>
                      ) : (
                        <>
                          <Badge tone="bad">incompatible</Badge>
                          <div className="small">requests unsupported: {a.compatibility?.unsupported_requested?.join(", ")}</div>
                        </>
                      )}
                    </td>
                    <td className="small">{fmtDate(a.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <div className="grid-2">
        <RegisterForm onDone={agents.reload} unsupported={meta.data?.unsupported_capabilities ?? []} />
        <ReferenceAgents onDone={agents.reload} />
      </div>

      <Card title="Connection instructions">
        <p>
          A participant is an ordinary external client. Each run issues a <strong>session token</strong> (shown once, on the New run screen). The participant sends tool calls over HTTP or MCP and receives envelopes;
          the token works for that run only and cannot reach anything else.
        </p>
        <pre>{ENVELOPE}</pre>
        {meta.error && <ErrorState error={meta.error} retry={meta.reload} />}
        {meta.data && (
          <div className="grid-2">
            <div>
              <h3>Available tools</h3>
              <KV rows={Object.entries(meta.data.tools ?? {}).map(([k, v]) => [<code key={k}>{k}</code>, v])} />
              <KV rows={[["HTTP", <code>{meta.data.gateway_url}/agent/v1/commands</code>], ["MCP", <code>{meta.data.mcp_url ?? `${meta.data.gateway_url}/agent/mcp`}</code>]]} />
            </div>
            <div>
              <h3>Unsupported capabilities</h3>
              <p className="muted small">Agents that request any of these are marked incompatible and their runs will be refused or flagged.</p>
              <StrList items={meta.data.unsupported_capabilities} empty="None reported." />
            </div>
          </div>
        )}
        <h3>Integration example</h3>
        <div className="tabs" role="group" aria-label="Snippet language">
          <button type="button" className="btn btn-small" aria-pressed={snippet === "python"} onClick={() => setSnippet("python")}>
            Python
          </button>
          <button type="button" className="btn btn-small" aria-pressed={snippet === "typescript"} onClick={() => setSnippet("typescript")}>
            TypeScript
          </button>
        </div>
        <pre>{snippet === "python" ? PY_SNIPPET : TS_SNIPPET}</pre>
        <p className="small">
          Run a reference participant against a suite from the repo root: <code>make run-agent ARGS="--agent cash_only --suite generated-dev-v1"</code>
        </p>
      </Card>
    </main>
  );
}

function SkillCard({ base }: { base: string }) {
  const skillUrl = `${base}/skill.md`;
  const prompt = `Install the Market Replay skill from ${skillUrl} and run the tests. Use the agent name "<your agent's name>". Report the results URL and, per episode, the model equity and whether the valuation was complete.`;
  return (
    <Card title="Give your agent the skill" className="connect">
      <p>
        Point any agent that can read a skill file (Bankr, OpenClaw, Hermes, Claude and similar) at <code>{skillUrl}</code> and ask it to run the tests. The skill tells it how to enroll by name, get one session token per episode, trade through the
        tools over HTTP or MCP, finish, and read the report. No registration screen, no operator.
      </p>
      <div className="row">
        <CopyButton text={skillUrl} label="Copy skill URL" />
        <CopyButton text={prompt} label="Copy a prompt for your agent" />
        <a className="btn btn-small" href={skillUrl} target="_blank" rel="noreferrer">
          Read the skill
        </a>
      </div>
      <p className="small muted" style={{ marginTop: 8 }}>
        Bankr catalog form: <code>install the market-replay skill from https://github.com/BankrBot/skills/tree/main/market-replay</code> once the skill is listed there; until then the URL above is the same file.
      </p>
    </Card>
  );
}

function RegisterForm({ onDone, unsupported }: { onDone: () => void; unsupported: string[] }) {
  const [f, setF] = useState({ name: "", runtime: "python", capabilities: "", config: "{}" });
  const [err, setErr] = useState<unknown>(null);
  const [ok, setOk] = useState<Agent | null>(null);
  const [busy, setBusy] = useState(false);
  const caps = f.capabilities.split(",").map((s) => s.trim()).filter(Boolean);
  const clash = caps.filter((c) => unsupported.includes(c));
  return (
    <Card title="Register agent">
      <form
        className="form"
        onSubmit={async (e) => {
          e.preventDefault();
          setErr(null);
          setOk(null);
          let config: unknown;
          try {
            config = JSON.parse(f.config || "{}");
          } catch (x) {
            setErr(new Error(`Config is not valid JSON: ${(x as Error).message}`));
            return;
          }
          setBusy(true);
          try {
            setOk(await post<Agent>("/agents", { name: f.name, version: "1", runtime: f.runtime, capabilities: caps, config }));
            onDone();
          } catch (x) {
            setErr(x);
          } finally {
            setBusy(false);
          }
        }}
      >
        <label className="field">
          Name
          <input required value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} />
        </label>
        <label className="field">
          Runtime
          <select value={f.runtime} onChange={(e) => setF({ ...f, runtime: e.target.value })}>
            {RUNTIMES.map((r) => (
              <option key={r}>{r}</option>
            ))}
            <option value="other">other</option>
          </select>
        </label>
        <label className="field">
          Capabilities (comma separated)
          <input value={f.capabilities} onChange={(e) => setF({ ...f, capabilities: e.target.value })} placeholder="e.g. swap, advance" />
        </label>
        {clash.length > 0 && <p className="notice">Requested capabilities not supported by this server: {clash.join(", ")}</p>}
        <label className="field">
          Config (JSON)
          <textarea value={f.config} onChange={(e) => setF({ ...f, config: e.target.value })} />
        </label>
        <div className="row">
          <button className="btn btn-primary" disabled={busy}>
            {busy ? "Registering…" : "Register"}
          </button>
          {ok && (
            <span>
              Registered <strong>{ok.name}</strong> <span className="mono small">{shortHash(ok.fingerprint)}</span>
            </span>
          )}
        </div>
      </form>
      {err !== null && <ErrorState error={err} />}
    </Card>
  );
}

function ReferenceAgents({ onDone }: { onDone: () => void }) {
  const [runtime, setRuntime] = useState<(typeof RUNTIMES)[number]>("python");
  const [msg, setMsg] = useState<Record<string, string>>({});
  const [err, setErr] = useState<unknown>(null);
  const descr: Record<string, string> = {
    cash_only: "Never trades. Establishes that abstention is legitimate and cost accounting is stable.",
    scheduled_basket: "Buys a fixed basket on a schedule; exercises order lifecycle and settlement.",
    random_actions: "Random valid and invalid tool calls; probes error handling and quality exposure.",
    model_client: "Calls an external model for decisions; requires configuration (Python only).",
  };
  return (
    <Card title="Included reference agents">
      <label className="field" style={{ maxWidth: 200 }}>
        Runtime
        <select value={runtime} onChange={(e) => setRuntime(e.target.value as (typeof RUNTIMES)[number])}>
          {RUNTIMES.map((r) => (
            <option key={r}>{r}</option>
          ))}
        </select>
      </label>
      <ul className="gate-list">
        {EXAMPLES.map((ex) => (
          <li key={ex} className="row" style={{ justifyContent: "space-between" }}>
            <span>
              <strong>{ex}</strong> <span className="muted small">{descr[ex]}</span>
              {msg[ex] && <div className="small">{msg[ex]}</div>}
            </span>
            <button
              type="button"
              className="btn btn-small"
              onClick={async () => {
                setErr(null);
                try {
                  const a = await post<Agent>("/agents", { name: `${ex}_${runtime}`, version: "1", runtime, capabilities: [], config: { example: ex } });
                  setMsg((m) => ({ ...m, [ex]: `registered as ${a.name} (${shortHash(a.agent_id)})` }));
                  onDone();
                } catch (x) {
                  setErr(x);
                }
              }}
            >
              Register {ex}_{runtime}
            </button>
          </li>
        ))}
      </ul>
      {err !== null && <ErrorState error={err} />}
      <p className="muted small">Registering a name that already exists returns 409.</p>
      <JsonView value={{ name: "<example>_<runtime>", runtime, capabilities: [], config: { example: "<example>" } }} />
    </Card>
  );
}
