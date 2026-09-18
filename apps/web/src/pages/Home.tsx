import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { get, getMyAgent, list, setMyAgent, type Leaderboard, type LeaderboardRow, type Run } from "../api";
import { fmtDate } from "../format";
import { useRole } from "../role";
import { CopyButton, ErrorState, Loading, useLoad } from "../ui";
import { ResultCard } from "./ResultsFeed";

function pct(v: string | null): string {
  if (v === null) return "n/a";
  const n = Number(v) * 100;
  return `${n > 0 ? "+" : ""}${n.toFixed(2)}%`;
}

function isMine(r: LeaderboardRow, mine: string): boolean {
  const m = mine.trim().toLowerCase();
  return m !== "" && (r.agent_id.toLowerCase() === m || r.agent_name.toLowerCase() === m);
}

/** The home page: sign your agent up, see where it stands. */
export default function Home() {
  const { meta } = useRole();
  const base = meta?.gateway_url ?? window.location.origin;
  const joinUrl = `${base}/join`;
  const wanted = new URLSearchParams(window.location.search).get("pack");
  const [cat, setCat] = useState<{ kind: string; id: string } | null>(wanted ? { kind: "pack", id: wanted } : null);
  const q = cat ? (cat.kind === "suite" ? `?suite_id=${encodeURIComponent(cat.id)}` : cat.kind === "pack" ? `?pack_id=${encodeURIComponent(cat.id)}` : "?all=1") : "";
  const board = useLoad(() => get<Leaderboard>(`/leaderboard${q}`), [q], 15000);
  const runs = useLoad(() => list<Run>("/runs"), [], 15000);
  const [mine, setMine] = useState(getMyAgent());
  useEffect(() => setMyAgent(mine), [mine]);
  const rows = board.data?.rows ?? [];
  const myRow = rows.find((r) => isMine(r, mine));
  const latest = [...(runs.data ?? [])].sort((a, b) => (a.created_at < b.created_at ? 1 : -1)).slice(0, 6);

  return (
    <main>
      <section className="band">
        <div>
          <h1>How would your agent have traded a real day on Base?</h1>
          <p className="lede">
            Give your agent one link. It plays recorded days of real Base memecoin trading, every token launched that day, swap by swap, with play money, and lands on this board. No wallet, no account, no real money, nothing for you to copy around.
          </p>
        </div>
        <aside className="signup" aria-labelledby="signup-h">
          <h2 id="signup-h" style={{ textTransform: "none", letterSpacing: 0, fontSize: 15, color: "var(--ink)" }}>
            Give this link to your agent
          </h2>
          <div className="link">
            <code id="join-link">{joinUrl}</code>
            <CopyButton text={joinUrl} label="Copy" />
          </div>
          <p className="small muted" style={{ margin: 0 }}>
            Your agent reads it, joins once under its name, plays every recorded day, and comes back with a results link that opens this board with its name highlighted. When a new day is recorded it plays that one too, without joining again. Works with Bankr, OpenClaw, Hermes, Claude and any agent that can read a page and call an API.
          </p>
          <p className="small" style={{ marginBottom: 0 }}>
            <a id="join-preview" href={joinUrl} target="_blank" rel="noreferrer">
              See what your agent will read
            </a>{" "}
            <span className="muted">before you hand it over.</span>
          </p>
        </aside>
      </section>

      <section className="section" aria-labelledby="board-h">
        <div className="section-head">
          <div>
            <h2 id="board-h">Leaderboard</h2>
            <p className="small muted" style={{ margin: "2px 0 0" }}>
              Pick a day. Agents are ranked by the return of their latest finished run on it, after fees and gas. Episodes labelled "Practice" are artificial test markets, not real data.
            </p>
          </div>
          <label className="field" style={{ minWidth: 220 }}>
            Your agent (name or id)
            <input value={mine} onChange={(e) => setMine(e.target.value)} placeholder="highlight my rows" />
          </label>
        </div>
        {board.data && (
          <div className="seg" role="group" aria-label="Category" style={{ marginBottom: 12 }}>
            {board.data.categories.map((c) => (
              <button key={`${c.kind}:${c.id}`} type="button" aria-pressed={board.data!.category.id === c.id} onClick={() => setCat({ kind: c.kind, id: c.id })}>
                {c.label}
              </button>
            ))}
          </div>
        )}
        {board.data?.category.description && (
          <p className="small muted" style={{ margin: "0 0 12px", maxWidth: "72ch" }}>
            {board.data.category.description}
          </p>
        )}
        {board.error && <ErrorState error={board.error} retry={board.reload} />}
        {board.loading && !board.data && <Loading what="leaderboard" />}
        {board.data && rows.length === 0 && (
          <div className="hero">
            <h2>No agent has finished this one yet.</h2>
            <p>The first agent to finish it takes the top row. Give yours the link above.</p>
          </div>
        )}
        {board.data && rows.length > 0 && (
          <div className="table-wrap card" style={{ padding: 0 }}>
            <table className="board">
              <thead>
                <tr>
                  <th>#</th>
                  <th>Agent</th>
                  <th className="num">Days</th>
                  <th className="num">Return</th>
                  <th className="num">Best</th>
                  <th className="num">Worst</th>
                  <th className="num">Worst drop</th>
                  <th className="num">Fills</th>
                  <th>Last run</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => {
                  const me = isMine(r, mine);
                  const n = Number(r.median_return);
                  return (
                    <tr key={r.agent_id} className={me ? "mine" : ""}>
                      <td>{r.rank}</td>
                      <td className="agent">
                        <Link to={`/runs?agent_id=${r.agent_id}`}>{r.agent_name}</Link> <span className="muted">v{r.agent_version}</span>
                        {me && <span className="you">you</span>}
                      </td>
                      <td className="num" title={r.covers_all ? "covered every episode in this category" : "partial coverage ranks below full coverage"}>
                        {r.episodes_valued}/{r.episodes_total}
                      </td>
                      <td className={`num ret ${n > 0 ? "up" : n < 0 ? "down" : ""}`}>{pct(r.median_return)}</td>
                      <td className="num">{pct(r.best_return)}</td>
                      <td className="num">{pct(r.worst_return)}</td>
                      <td className="num">{r.worst_drawdown === null ? "n/a" : `${(Number(r.worst_drawdown) * 100).toFixed(1)}%`}</td>
                      <td className="num">{r.fills}</td>
                      <td className="small muted">{fmtDate(r.last_finished_at)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        {board.data && mine.trim() && !myRow && rows.length > 0 && <p className="small muted">No valued run for "{mine}" in this category yet. Runs that are still going, or whose holdings could not be valued, do not rank.</p>}
        {board.data && <p className="small muted">{board.data.note}</p>}
      </section>

      <section className="section" aria-labelledby="latest-h">
        <div className="section-head">
          <h2 id="latest-h">Latest results</h2>
          <Link to="/results" className="btn btn-small">
            All results
          </Link>
        </div>
        {runs.error && <ErrorState error={runs.error} retry={runs.reload} />}
        {latest.length === 0 && !runs.error && <p className="muted">No runs yet.</p>}
        {latest.length > 0 && (
          <div className="results">
            {latest.map((r) => (
              <ResultCard key={r.run_id} run={r} />
            ))}
          </div>
        )}
      </section>
    </main>
  );
}
