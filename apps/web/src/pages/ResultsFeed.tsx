import { Link } from "react-router-dom";
import { get, list, type Comparison, type Run, type Usage } from "../api";
import { exportPolicy, fmtDate, fmtPct, fmtRaw, humanize, shortHash, unitLabel } from "../format";
import { trustTags } from "../explain";
import { useRole } from "../role";
import { Badge, ErrorState, Loading, RunStateBadge, useLoad } from "../ui";

/** Results first: every run as a sentence with one number, newest at the top. */
export default function ResultsFeed() {
  const runs = useLoad(() => list<Run>("/runs"), []);
  const comps = useLoad(() => list<Comparison>("/comparisons"), []);
  const usage = useLoad(() => get<Usage>("/usage"), []);
  const { meta } = useRole();
  const items = [...(runs.data ?? [])].sort((a, b) => (a.created_at < b.created_at ? 1 : -1));

  return (
    <main className="stack">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h1 style={{ margin: 0 }}>Runs</h1>
        <span className="row">
          <Link to="/runs" className="btn btn-small">
            Table view
          </Link>
        </span>
      </div>
      {runs.error && <ErrorState error={runs.error} retry={runs.reload} />}
      {runs.loading && !runs.data && <Loading what="results" />}
      {runs.data && runs.data.length === 0 && (
        <p className="muted">
          No runs yet. <Link to="/">Give an agent the join link</Link> and its results appear here.
        </p>
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

export function ResultCard({ run: r }: { run: Run }) {
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
          {" · "}{r.pack_label ?? r.pack_name ?? shortHash(r.pack_id)}
        </strong>
        <RunStateBadge state={r.state} />
      </div>
      {s?.ranking_eligible === false && <p className="notice warn">Provisional outcome. Excluded from ranking; see the report's eligibility gates.</p>}
      {headline}
      {s && (
        <div className="muted small">
          {s.primary_metric === "final_cash_return_v1" ? "final ETH/cash return" : "legacy portfolio return (unranked)"} · after modeled costs · {s.confirmed_fills ?? "unknown"} fill{s.confirmed_fills === 1 ? "" : "s"} of {s.orders_total ?? "unknown"} order{s.orders_total === 1 ? "" : "s"} · max drawdown {s.max_drawdown === null || s.max_drawdown === undefined ? "n/a" : fmtPct(s.max_drawdown, 1)} · gas{" "}
          {fmtRaw(s.gas_total_raw, s.numeraire_decimals)} {unitLabel(s.numeraire)}
          {s.unresolved_orders > 0 && <Badge tone="warn">{s.unresolved_orders} unresolved order(s)</Badge>}
        </div>
      )}
      <div className="links small">
        <Link to={`/runs/${r.run_id}`}>Run</Link>
        {r.has_report && <Link to={`/runs/${r.run_id}/results`}>Full report</Link>}
        <span className="muted">{fmtDate(r.created_at)}</span>
      </div>
      <div className="foot">{s ? trustTags(dims, s.valuation_complete).join(" · ") : `${exportPolicy(r.mode)} · ${r.launch ? "reference participant" : "external agent"}`}</div>
    </article>
  );
}

