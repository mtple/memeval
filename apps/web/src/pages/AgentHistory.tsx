import { Link, useParams } from "react-router-dom";
import { get, type Agent } from "../api";
import { fmtDate } from "../format";
import { Badge, Card, ErrorState, Loading, RunStateBadge, useLoad } from "../ui";

type HistoryRun = {
  run_id: string;
  state: string;
  outcome: string;
  counts_for_ranking: boolean;
  primary_metric: string | null;
  headline_return: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string | null;
  fills: number | null;
  orders: number | null;
  final_cash: string | null;
  started_with: string | null;
  gas_paid: string | null;
  max_drawdown: string | null;
  unsold_holdings: number | null;
  activity: string | null;
  results_path: string | null;
};
type HistoryDay = {
  pack_id: string;
  pack_name: string | null;
  label: string;
  kind: "real" | "practice";
  date: string | null;
  available: boolean;
  market_note: string | null;
  market_return: string | null;
  runs: HistoryRun[];
  counted_run_id: string | null;
  counted_return: string | null;
  summary: string;
};
type History = {
  agent: Agent;
  days: HistoryDay[];
  totals: { runs: number; days_played: number; days_finished: number; runs_in_progress: number; fills: number };
  note: string;
};

/** One agent's runs, day by day, in plain words. Public: an owner should be able to read it without a login. */
export default function AgentHistory() {
  const { id = "" } = useParams();
  const h = useLoad(() => get<History>(`/agents/${encodeURIComponent(id)}/history`), [id], 10000);
  const H = h.data;
  return (
    <main className="stack">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h1 style={{ margin: 0 }}>{H ? `${H.agent.name} v${H.agent.version}` : "Agent"}</h1>
        <span className="row">
          <Link to={`/runs?agent_id=${encodeURIComponent(id)}`} className="btn btn-small">
            Run table
          </Link>
          <Link to="/" className="btn btn-small">
            Leaderboard
          </Link>
        </span>
      </div>
      {h.error && <ErrorState error={h.error} retry={h.reload} />}
      {h.loading && !H && <Loading what="history" />}
      {H && (
        <>
          <Card title="What this agent has done here">
            <div className="metrics">
              <div className="metric">
                <span className="lbl">Days played</span>
                <span className="val">{H.totals.days_played}</span>
                <span className="lbl">{H.totals.days_finished} with a ranked result</span>
              </div>
              <div className="metric">
                <span className="lbl">Runs</span>
                <span className="val">{H.totals.runs}</span>
                <span className="lbl">{H.totals.runs_in_progress ? `${H.totals.runs_in_progress} in progress` : "none in progress"}</span>
              </div>
              <div className="metric">
                <span className="lbl">Orders filled</span>
                <span className="val">{H.totals.fills}</span>
                <span className="lbl">across ranked runs</span>
              </div>
              <div className="metric">
                <span className="lbl">Joined</span>
                <span className="val" style={{ fontSize: 16 }}>
                  {fmtDate(H.agent.created_at)}
                </span>
                <span className="lbl">{H.agent.runtime}</span>
              </div>
            </div>
            <p className="small muted" style={{ marginBottom: 0 }}>
              {H.note}
            </p>
          </Card>
          {H.days.length === 0 && (
            <Card title="No runs yet">
              <p className="muted">This agent has joined but has not played a day. Tell it to play, and its runs appear here.</p>
            </Card>
          )}
          {H.days.map((d) => (
            <Card
              key={d.pack_id}
              title={
                <>
                  {d.label} {d.kind === "practice" ? <Badge tone="warn">Generated data</Badge> : !d.available ? <Badge tone="warn">withdrawn</Badge> : null}
                </>
              }
              actions={
                d.available ? (
                  <Link className="btn btn-small" to={`/?pack=${d.pack_id}`}>
                    Board for this day
                  </Link>
                ) : null
              }
            >
              <p style={{ marginTop: 0 }}>{d.summary}</p>
              {d.market_note && (
                <p className="small muted" style={{ maxWidth: "80ch" }}>
                  {d.market_note}
                </p>
              )}
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Started</th>
                      <th>What happened</th>
                      <th>Activity</th>
                      <th className="num">Started with</th>
                      <th className="num">Ended with</th>
                      <th className="num">Gas paid</th>
                      <th></th>
                    </tr>
                  </thead>
                  <tbody>
                    {d.runs.map((r) => (
                      <tr key={r.run_id} className={r.counts_for_ranking ? "selected" : ""}>
                        <td className="small">{fmtDate(r.started_at ?? r.created_at)}</td>
                        <td>
                          <RunStateBadge state={r.state} /> {r.outcome}
                          {r.counts_for_ranking && <Badge tone="ok">counts on the board</Badge>}
                        </td>
                        <td className="small">{r.activity ?? <span className="muted">no report yet</span>}</td>
                        <td className="num small">{r.started_with ?? "n/a"}</td>
                        <td className="num small">{r.final_cash ?? "n/a"}</td>
                        <td className="num small">{r.gas_paid ?? "n/a"}</td>
                        <td className="small">{r.results_path ? <Link to={r.results_path}>Full result</Link> : <Link to={`/runs/${r.run_id}`}>Run</Link>}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>
          ))}
        </>
      )}
    </main>
  );
}
