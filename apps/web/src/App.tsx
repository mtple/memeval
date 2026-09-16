import { useCallback, useEffect, useState } from "react";
import { NavLink, Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { ApiError, get, getServerUrl, getToken, setServerUrl, setToken } from "./api";
import { Badge } from "./ui";
import Episodes from "./pages/Episodes";
import Agents from "./pages/Agents";
import Runs from "./pages/Runs";
import RunDetail from "./pages/RunDetail";
import Results from "./pages/Results";
import Compare from "./pages/Compare";
import DataHealth from "./pages/DataHealth";

const NAV: [string, string][] = [
  ["/episodes", "Episodes"],
  ["/agents", "Agent setup"],
  ["/runs", "Run"],
  ["/results", "Results"],
  ["/compare", "Compare"],
  ["/data-health", "Data health"],
];

type Health = { status: string; dev_mode: boolean };
type Conn = { state: "checking" } | { state: "ok"; health: Health } | { state: "down"; error: ApiError | Error };

function SettingsBar({ conn, onChange }: { conn: Conn; onChange: () => void }) {
  const [tok, setTok] = useState(getToken());
  const [draft, setDraft] = useState(tok);
  const [server, setServer] = useState(getServerUrl());
  const loc = useLocation();
  const nav = useNavigate();

  useEffect(() => {
    const p = new URLSearchParams(loc.search);
    const t = p.get("token");
    const srv = p.get("server");
    let changed = false;
    if (srv !== null) {
      setServerUrl(srv);
      setServer(getServerUrl());
      p.delete("server");
      changed = true;
    }
    if (t) {
      setToken(t);
      setTok(t);
      setDraft(t);
      p.delete("token");
      changed = true;
    }
    if (changed) {
      nav({ pathname: loc.pathname, search: p.toString() ? `?${p}` : "" }, { replace: true });
      onChange();
    }
  }, [loc.search, loc.pathname, nav, onChange]);

  return (
    <form
      id="settings"
      className="settings"
      onSubmit={(e) => {
        e.preventDefault();
        setServerUrl(server);
        setServer(getServerUrl());
        setToken(draft.trim());
        setTok(draft.trim());
        onChange();
      }}
    >
      <label htmlFor="server-url" className="status">
        Server
      </label>
      <input id="server-url" type="url" autoComplete="off" value={server} onChange={(e) => setServer(e.target.value)} placeholder="http://127.0.0.1:8000 (blank = this origin)" style={{ maxWidth: 260 }} />
      <label htmlFor="admin-token" className="status">
        Admin token
      </label>
      <input id="admin-token" type="password" autoComplete="off" value={draft} onChange={(e) => setDraft(e.target.value)} placeholder="printed by `make serve`" />
      <button type="submit" className="btn btn-small">
        Connect
      </button>
      {tok && (
        <button
          type="button"
          className="btn btn-small"
          onClick={() => {
            setToken("");
            setTok("");
            setDraft("");
            onChange();
          }}
        >
          Clear token
        </button>
      )}
      <span className="status">
        {conn.state === "ok" ? (
          <>
            <Badge tone="ok">server {conn.health.status}</Badge> {conn.health.dev_mode && <Badge tone="warn">development mode</Badge>}
          </>
        ) : conn.state === "checking" ? (
          <Badge tone="neutral">checking…</Badge>
        ) : (
          <Badge tone="bad">not connected</Badge>
        )}{" "}
        {tok ? <Badge tone="neutral">token set</Badge> : <Badge tone="warn">no token</Badge>}
      </span>
    </form>
  );
}

/** One explanation instead of a 404 on every card when no server answers. */
function NotConnected({ error, retry }: { error: ApiError | Error; retry: () => void }) {
  const kind = error instanceof ApiError ? error.kind : "unreachable";
  const target = getServerUrl() || `${window.location.origin} (this origin)`;
  return (
    <main className="stack">
      <h1>Not connected to a Market Replay server</h1>
      <div className="card">
        <p>
          <strong>{kind === "no_backend" ? "This address serves only the interface." : "The server did not answer."}</strong> {target} returned {kind === "no_backend" ? "a page instead of the API" : "a network error"}. Every screen here reads from a
          running Market Replay server; there is nothing to show until one is connected.
        </p>
        <ol>
          <li>
            Start the server on a machine you control: <code>make serve</code> (prints the admin token). Packs, runs and reports live on that machine.
          </li>
          <li>
            If this page is not served by that server (for example a static host), allow this origin on the server:{" "}
            <code>MARKET_REPLAY_CORS_ORIGINS={window.location.origin} make serve</code>
          </li>
          <li>Enter the server URL and admin token in the bar above and press Connect.</li>
        </ol>
        <p className="muted small">Detail: {error.message}</p>
        <button type="button" className="btn" onClick={retry}>
          Retry
        </button>
      </div>
    </main>
  );
}

function ResultsIndex() {
  return (
    <main>
      <h1>Results</h1>
      <p>Results are attached to a run. Open a run from the Run screen and follow its Results link (available once a report exists).</p>
      <NavLink to="/runs" className="btn">
        Go to runs
      </NavLink>
    </main>
  );
}

export default function App() {
  const [conn, setConn] = useState<Conn>({ state: "checking" });
  const [tick, setTick] = useState(0);
  const recheck = useCallback(() => setTick((t) => t + 1), []);
  useEffect(() => {
    let alive = true;
    setConn({ state: "checking" });
    get<Health>("/health")
      .then((h) => alive && setConn({ state: "ok", health: h }))
      .catch((e: unknown) => alive && setConn({ state: "down", error: e instanceof Error ? e : new Error(String(e)) }));
    return () => {
      alive = false;
    };
  }, [tick]);
  return (
    <>
      <SettingsBar conn={conn} onChange={recheck} />
      <nav className="topnav" aria-label="Primary">
        <span className="brand">Market Replay</span>
        {NAV.map(([to, label]) => (
          <NavLink key={to} to={to} className={({ isActive }) => (isActive ? "active" : "")}>
            {label}
          </NavLink>
        ))}
      </nav>
      {conn.state === "down" ? (
        <NotConnected error={conn.error} retry={recheck} />
      ) : conn.state === "checking" ? (
        <main>
          <p className="muted" role="status">
            Connecting to the server…
          </p>
        </main>
      ) : (
        <Routes>
          <Route path="/" element={<Navigate to="/episodes" replace />} />
          <Route path="/episodes" element={<Episodes />} />
          <Route path="/agents" element={<Agents />} />
          <Route path="/runs" element={<Runs />} />
          <Route path="/runs/:id" element={<RunDetail />} />
          <Route path="/runs/:id/results" element={<Results />} />
          <Route path="/results" element={<ResultsIndex />} />
          <Route path="/compare" element={<Compare />} />
          <Route path="/compare/:id" element={<Compare />} />
          <Route path="/data-health" element={<DataHealth />} />
          <Route path="/data-health/:packId" element={<DataHealth />} />
          <Route
            path="*"
            element={
              <main>
                <h1>Not found</h1>
                <p>
                  No screen at this path. <NavLink to="/episodes">Go to Episodes</NavLink>.
                </p>
              </main>
            }
          />
        </Routes>
      )}
      <footer>Blinded interface, not contamination-proof. Generated results are not historical performance.</footer>
    </>
  );
}
