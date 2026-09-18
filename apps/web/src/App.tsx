import { useCallback, useEffect, useState } from "react";
import { NavLink, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { ApiError, get, getServerUrl, setMyAgent, setServerUrl, setToken } from "./api";
import { RoleProvider, useRole } from "./role";
import { Badge } from "./ui";
import Home from "./pages/Home";
import ResultsFeed from "./pages/ResultsFeed";
import Episodes from "./pages/Episodes";
import AgentHistory from "./pages/AgentHistory";
import Agents from "./pages/Agents";
import Runs from "./pages/Runs";
import RunDetail from "./pages/RunDetail";
import Results from "./pages/Results";
import Compare from "./pages/Compare";
import DataHealth from "./pages/DataHealth";

const NAV: [string, string, boolean][] = [
  ["/", "Leaderboard", false],
  ["/results", "Results", false],
  ["/episodes", "Days", false],
  ["/agents", "Agents", true],
  ["/compare", "Compare", true],
];

type Health = { status: string; dev_mode: boolean };
type Conn = { state: "checking" } | { state: "ok"; health: Health } | { state: "down"; error: ApiError | Error };

/** `?token=` (operator) and `?server=` (a UI served elsewhere) are accepted once and removed from the URL. */
function useQueryParams(onChange: () => void) {
  const loc = useLocation();
  const nav = useNavigate();
  useEffect(() => {
    const p = new URLSearchParams(loc.search);
    const t = p.get("token");
    const srv = p.get("server");
    const mine = p.get("agent");
    let changed = false;
    if (mine) {
      setMyAgent(mine);
      p.delete("agent");
      changed = true;
    }
    if (srv !== null) {
      setServerUrl(srv);
      p.delete("server");
      changed = true;
    }
    if (t) {
      setToken(t);
      p.delete("token");
      changed = true;
    }
    if (changed) {
      nav({ pathname: loc.pathname, search: p.toString() ? `?${p}` : "" }, { replace: true });
      onChange();
    }
  }, [loc.search, loc.pathname, nav, onChange]);
}

function SignIn({ onChange }: { onChange: () => void }) {
  const { role, loading } = useRole();
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  if (role === "admin") {
    return (
      <span className="nav-right">
        <Badge tone="ok">Operator</Badge>
        <button
          type="button"
          className="btn btn-small"
          onClick={() => {
            setToken("");
            onChange();
          }}
        >
          Sign out
        </button>
      </span>
    );
  }
  return (
    <span className="nav-right">
      <button id="signin-btn" type="button" className="btn btn-small" onClick={() => setOpen(true)} disabled={loading}>
        Sign in
      </button>
      {open && (
        <div className="modal-backdrop" onClick={() => setOpen(false)}>
          <form
            className="modal form"
            onClick={(e) => e.stopPropagation()}
            onSubmit={async (e) => {
              e.preventDefault();
              setBusy(true);
              setErr(null);
              setToken(draft.trim());
              try {
                const m = await get<{ role: string }>("/meta");
                if (m.role !== "admin") throw new Error("not admin");
                setOpen(false);
                setDraft("");
                onChange();
              } catch {
                setToken("");
                setErr("That is not this server's admin token.");
              } finally {
                setBusy(false);
              }
            }}
          >
            <h2 style={{ textTransform: "none", letterSpacing: 0, color: "var(--text)" }}>Operator sign-in</h2>
            <p className="muted small">Only the person running this server needs this. Starting runs and reading results does not require signing in.</p>
            <label className="field">
              Admin token
              <input id="admin-token" type="password" autoComplete="current-password" value={draft} onChange={(e) => setDraft(e.target.value)} autoFocus />
            </label>
            {err && <p className="notice bad">{err}</p>}
            <div className="row">
              <button className="btn btn-primary" disabled={busy || !draft.trim()}>
                {busy ? "Checking…" : "Sign in"}
              </button>
              <button type="button" className="btn" onClick={() => setOpen(false)}>
                Cancel
              </button>
            </div>
          </form>
        </div>
      )}
    </span>
  );
}

/** First-run screen when no server answers. The only place the server address is edited. */
function NotConnected({ error, retry }: { error: ApiError | Error; retry: () => void }) {
  const kind = error instanceof ApiError ? error.kind : "unreachable";
  const [server, setServer] = useState(getServerUrl());
  const target = getServerUrl() || `${window.location.origin} (this origin)`;
  return (
    <main className="stack">
      <h1>Not connected to a Market Replay server</h1>
      <div className="card">
        <p>
          <strong>{kind === "no_backend" ? "This address serves only the interface." : "The server did not answer."}</strong> {target} returned {kind === "no_backend" ? "a page instead of the API" : "a network error"}. Every screen here reads from a
          running Market Replay server.
        </p>
        <form
          className="form"
          onSubmit={(e) => {
            e.preventDefault();
            setServerUrl(server);
            retry();
          }}
        >
          <label htmlFor="server-url" className="field">
            Server URL (blank = this origin)
            <input id="server-url" type="url" autoComplete="off" value={server} onChange={(e) => setServer(e.target.value)} placeholder="https://your-deployment.example" />
          </label>
          <div className="row">
            <button className="btn btn-primary">Connect</button>
            <button type="button" className="btn" onClick={retry}>
              Retry
            </button>
          </div>
        </form>
        <p className="muted small">
          Self-hosting: <code>make serve</code> starts a server on your machine; to use this page with it, allow this origin with <code>MARKET_REPLAY_CORS_ORIGINS={window.location.origin}</code>.
        </p>
        <p className="muted small">Detail: {error.message}</p>
      </div>
    </main>
  );
}

function Shell({ conn, recheck }: { conn: Conn; recheck: () => void }) {
  const { role } = useRole();
  return (
    <>
      <nav className="topnav" aria-label="Primary">
        <span className="brand">Market Replay</span>
        {NAV.filter(([, , operatorOnly]) => !operatorOnly || role === "admin").map(([to, label]) => (
          <NavLink key={to} to={to} end={to === "/"} className={({ isActive }) => (isActive ? "active" : "")}>
            {label}
          </NavLink>
        ))}
        {conn.state === "ok" && <SignIn onChange={recheck} />}
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
          <Route path="/" element={<Home />} />
          <Route path="/episodes" element={<Episodes />} />
          <Route path="/agents" element={<Agents />} />
          <Route path="/agents/:id" element={<AgentHistory />} />
          <Route path="/runs" element={<Runs />} />
          <Route path="/runs/:id" element={<RunDetail />} />
          <Route path="/runs/:id/results" element={<Results />} />
          <Route path="/results" element={<ResultsFeed />} />
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
                  No screen at this path. <NavLink to="/">Go to the leaderboard</NavLink>.
                </p>
              </main>
            }
          />
        </Routes>
      )}
      <footer>
        Blinded interface, not contamination-proof. Generated results are not historical performance.
        {conn.state === "ok" && conn.health.dev_mode && " · development mode"}
      </footer>
    </>
  );
}

export default function App() {
  const [conn, setConn] = useState<Conn>({ state: "checking" });
  const [tick, setTick] = useState(0);
  const recheck = useCallback(() => setTick((t) => t + 1), []);
  useQueryParams(recheck);
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
    <RoleProvider tick={tick}>
      <Shell conn={conn} recheck={recheck} />
    </RoleProvider>
  );
}
