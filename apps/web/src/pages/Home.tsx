import { Link } from "react-router-dom";
import { get, list, type Comparison, type Run, type Usage } from "../api";
import { fmtDate, fmtPct, fmtRaw, humanize, shortHash } from "../format";
import { useRole } from "../role";
import { Badge, ErrorState, Loading, RunStateBadge, useLoad } from "../ui";

const DIMS: [string, string][] = [
  ["data_origin", "data"],
  ["availability_basis", "availability"],
  ["execution_model", "execution"],
  ["valuation", "valuation"],
  ["isolation", "isolation"],
  ["predictive_validity", "predictive validity"],
];

/** Results first: every run as a sentence with one number, newest at the top. */
export default function Home() {
  const runs = useLoad(() => list<Run>("/runs"), [], 5000);
  const comps = useLoad(() => list<Comparison>("/comparisons"), []);
  const usage = useLoad(() => get<Usage>("/usage"), [], 30000);
  const { meta } = useRole();
  const items = [...(runs.data ?? [])].sort((a, b) => (a.created_at < b.created_at ? 1 : -1));

  return (
    <main className="stack">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h1 style={{ margin: 0 }}>Results</h1>
        <span className="row">
          <Link to="/runs" className="btn btn-small">
            Table view
          </Link>
          <Link to="/new" className="btn btn-small btn-primary">
            + New run
          </Link>
        </span>
      </div>
      {runs.error && <ErrorState error={runs.error} retry={runs.reload} />}
      {runs.loading && !runs.data && <Loading what="results" />}
      {runs.data && runs.data.length === 0 && (
        <section className="hero">
          <h2>Point your agent at a market and see what it does with real money mechanics and no real money.</h2>
          <p>
            Market Replay replays bounded market episodes with virtual time, blinded asset names, exact accounting and an explicit constant-product execution model. Your agent trades through fourteen tools over HTTP or MCP. The report says what
            happened after modeled costs and what to distrust about that answer. There is no score.
          </p>
          <ol className="steps">
            <li>
              Press <strong>New run</strong>, name your agent and pick an episode. You get a one-time session token.
            </li>
            <li>Point your agent at the URL with that token (HTTP, MCP, or the Python and TypeScript SDKs).</li>
            <li>The result appears here when the agent calls session.finish or the episode ends.</li>
          </ol>
          <p className="muted small">
            Agent that can read a skill file? Point it at <code>{`${meta?.gateway_url ?? window.location.origin}/skill.md`}</code> and ask it to run the tests; it enrolls itself. No agent yet? Choose a reference participant in New run and the server runs
            it for you.
          </p>
          <Link to="/new" className="btn btn-primary">
            Start a run
          </Link>
        </section>
      )}
      {items.length > 0 && (
        <div className="results">
          {items.map((r) => (
            <ResultCard key={r.run_id} run={r} />
          ))}
        </div>
      )}
      {comps.data && comps.data.length > 0 && (
        <section className="card">
          <h2>Comparisons</h2>
          <ul className="plain">
            {comps.data.map((c) => (
              <li key={c.comparison_id}>
                <Link to={`/compare/${c.comparison_id}`}>
                  {c.agents?.a?.name ?? "?"} vs {c.agents?.b?.name ?? "?"}
                </Link>{" "}
                <span className="muted small">
                  {c.summary?.episodes_paired ?? 0} paired episodes · {fmtDate(c.created_at)}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}
      {usage.data && (
        <p className="muted small">
          Server compute today: {usage.data.today.runs} runs · {usage.data.today.cpu_seconds.toFixed(0)} CPU-s of a {usage.data.caps.max_runs_per_day}-run daily cap.
          {meta?.public_runs === false && " Starting runs on this server requires operator sign-in."}
        </p>
      )}
    </main>
  );
}

function ResultCard({ run: r }: { run: Run }) {
  const s = r.result_summary;
  const ret = s?.headline_return;
  const n = ret === null || ret === undefined ? null : Number(ret);
  const terminal = !["queued", "running", "paused"].includes(r.state);
  let headline: JSX.Element;
  if (s && n !== null && Number.isFinite(n)) {
    headline = <div className={`headline ${n > 0 ? "up" : n < 0 ? "down" : ""}`}>{`${n > 0 ? "+" : ""}${fmtPct(n, 2)}`}</div>;
  } else if (s && !s.valuation_complete) {
    headline = <div className="headline na">could not be valued: {s.unpriced_inventory} holding(s) unpriced</div>;
  } else if (!terminal) {
    headline = <div className="headline na">{r.state === "queued" ? "waiting for the agent to connect" : `${humanize(r.state)} · ${r.live ? `${Math.round(((r.live.clock_ms ?? 0) / Math.max(1, r.live.duration_ms)) * 100)}% of the episode` : ""}`}</div>;
  } else {
    headline = <div className="headline na">{r.error ? r.error : "no report"}</div>;
  }
  const dims = s?.status_dimensions ?? {};
  return (
    <article className="card result-card">
      <div className="title">
        <strong>
          {r.agent_name ?? shortHash(r.agent_id)}
          {r.agent_version ? <span className="muted"> v{r.agent_version}</span> : null} · {r.pack_name ?? shortHash(r.pack_id)}
        </strong>
        <RunStateBadge state={r.state} />
      </div>
      {headline}
      {s && (
        <div className="muted small">
          after modeled costs · {s.confirmed_fills} fill{s.confirmed_fills === 1 ? "" : "s"} of {s.orders_total} order{s.orders_total === 1 ? "" : "s"} · max drawdown {s.max_drawdown === null || s.max_drawdown === undefined ? "n/a" : fmtPct(s.max_drawdown, 1)} · gas{" "}
          {fmtRaw(s.gas_total_raw, s.numeraire_decimals)} {s.numeraire ?? ""}
          {s.unresolved_orders > 0 && <Badge tone="warn">{s.unresolved_orders} unresolved order(s)</Badge>}
        </div>
      )}
      <div className="links small">
        <Link to={`/runs/${r.run_id}`}>Run</Link>
        {r.has_report && <Link to={`/runs/${r.run_id}/results`}>Full report</Link>}
        <span className="muted">{fmtDate(r.created_at)}</span>
      </div>
      <div className="foot">
        {DIMS.filter(([k]) => dims[k]).map(([k, label]) => (
          <span key={k} title={label} style={{ marginRight: 8 }}>
            {humanize(dims[k])}
          </span>
        ))}
        {!s && <span>{r.mode} mode · {humanize(r.isolation)}</span>}
      </div>
    </article>
  );
}
