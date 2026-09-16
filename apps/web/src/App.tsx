import { useEffect, useState } from "react";
import { NavLink, Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { get, getToken, setToken } from "./api";
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

function SettingsBar() {
  const [tok, setTok] = useState(getToken());
  const [draft, setDraft] = useState(tok);
  const [health, setHealth] = useState<{ status: string; dev_mode: boolean } | null>(null);
  const loc = useLocation();
  const nav = useNavigate();

  useEffect(() => {
    const p = new URLSearchParams(loc.search);
    const t = p.get("token");
    if (t) {
      setToken(t);
      setTok(t);
      setDraft(t);
      p.delete("token");
      nav({ pathname: loc.pathname, search: p.toString() ? `?${p}` : "" }, { replace: true });
    }
  }, [loc.search, loc.pathname, nav]);

  useEffect(() => {
    get<{ status: string; dev_mode: boolean }>("/health")
      .then(setHealth)
      .catch(() => setHealth(null));
  }, [tok]);

  return (
    <form
      id="settings"
      className="settings"
      onSubmit={(e) => {
        e.preventDefault();
        setToken(draft.trim());
        setTok(draft.trim());
      }}
    >
      <label htmlFor="admin-token" className="status">
        Admin token
      </label>
      <input id="admin-token" type="password" autoComplete="off" value={draft} onChange={(e) => setDraft(e.target.value)} placeholder="Bearer token for /api/v1 (stored in localStorage as mr_admin_token)" />
      <button type="submit" className="btn btn-small">
        Save
      </button>
      {tok && (
        <button
          type="button"
          className="btn btn-small"
          onClick={() => {
            setToken("");
            setTok("");
            setDraft("");
          }}
        >
          Clear
        </button>
      )}
      <span className="status">
        {health ? (
          <>
            <Badge tone="ok">server {health.status}</Badge> {health.dev_mode && <Badge tone="warn">development mode</Badge>}
          </>
        ) : (
          <Badge tone="bad">server unreachable</Badge>
        )}{" "}
        {tok ? <Badge tone="neutral">token set</Badge> : <Badge tone="warn">no token</Badge>}
      </span>
    </form>
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
  return (
    <>
      <SettingsBar />
      <nav className="topnav" aria-label="Primary">
        <span className="brand">Market Replay</span>
        {NAV.map(([to, label]) => (
          <NavLink key={to} to={to} className={({ isActive }) => (isActive ? "active" : "")}>
            {label}
          </NavLink>
        ))}
      </nav>
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
      <footer>Blinded interface, not contamination-proof. Generated results are not historical performance.</footer>
    </>
  );
}
