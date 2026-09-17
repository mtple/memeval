import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useEffect, useRef } from "react";
import { list, post, get, type Pack, type Validation, type WeekJob } from "../api";
import { fmtDuration, fmtMs, fmtDate, humanize } from "../format";
import { useRole } from "../role";
import { Badge, Card, EmptyState, ErrorState, GateList, GateSummary, JsonView, KV, Loading, StrList, toneForStatus, useLoad } from "../ui";

const uniq = (xs: string[]) => Array.from(new Set(xs)).sort();

export default function Episodes() {
  const { data: packs, error, loading, reload } = useLoad(() => list<Pack>("/packs"), []);
  const [chain, setChain] = useState("");
  const [origin, setOrigin] = useState("");
  const [use, setUse] = useState("");
  const [kind, setKind] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const { role } = useRole();

  const filtered = useMemo(
    () =>
      (packs ?? []).filter(
        (p) =>
          (!chain || p.chain === chain) &&
          (!origin || p.origin === origin) &&
          (!use || p.use_status === use) &&
          (!kind ||
            (kind === "runnable" && p.runnable) ||
            (kind === "diagnostic" && p.diagnostic_only) ||
            (kind === "incomplete" && (!p.runnable || (p.summary?.executable_failure ?? null) !== null || p.summary?.pools_executable < p.summary?.pools_total))),
      ),
    [packs, chain, origin, use, kind],
  );
  const sel = packs?.find((p) => p.pack_id === selected) ?? null;

  return (
    <main className="stack">
      <h1>Weeks</h1>
      <RealWeeks onBuilt={reload} />
      {error && <ErrorState error={error} retry={reload} />}
      {loading && !packs && <Loading what="weeks" />}
      {packs && role !== "admin" && (
        <Card title="Weeks agents can play">
          <p className="small muted">Real weeks are recorded from Base for the dates shown. Practice weeks are artificial markets with known rules, useful for testing an agent before it plays a real week.</p>
          {packs.filter((p) => p.runnable).length === 0 ? (
            <p className="muted">No week is ready yet.</p>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Week</th>
                    <th>Kind</th>
                    <th>Dates</th>
                    <th className="num">Pools</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {[...packs]
                    .filter((p) => p.runnable)
                    .sort((a, b) => (a.kind === b.kind ? (b.period?.start_utc ?? "").localeCompare(a.period?.start_utc ?? "") : a.kind === "real" ? -1 : 1))
                    .map((p) => (
                      <tr key={p.pack_id}>
                        <td>{p.label ?? p.name}</td>
                        <td>{p.kind === "real" ? <Badge tone="ok">Real data</Badge> : <Badge tone="warn">Practice (artificial)</Badge>}</td>
                        <td className="small">{p.period ? `${p.period.start_utc.slice(0, 10)} to ${p.period.end_utc.slice(0, 10)}` : fmtDuration(p.duration_ms, p.is_full_week)}</td>
                        <td className="num">{p.summary?.pools_executable ?? "?"}</td>
                        <td className="small">
                          <Link to={`/?pack=${p.pack_id}`}>Leaderboard</Link> · <Link to="/new">Run an agent</Link>
                        </td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}
      {packs && role === "admin" && (
        <>
          <div className="filters" role="group" aria-label="Filters">
            <Sel label="Chain" value={chain} onChange={setChain} options={uniq(packs.map((p) => p.chain))} />
            <Sel label="Origin" value={origin} onChange={setOrigin} options={uniq(packs.map((p) => p.origin))} />
            <Sel label="Use status" value={use} onChange={setUse} options={uniq(packs.map((p) => p.use_status))} />
            <Sel label="Kind" value={kind} onChange={setKind} options={["runnable", "diagnostic", "incomplete"]} />
            <span className="muted small" style={{ alignSelf: "end" }}>
              {filtered.length} of {packs.length} packs
            </span>
          </div>
          {packs.length === 0 ? (
            <EmptyState title="No episodes on this server yet.">
              The operator imports them (<code>make demo</code> generates fixtures locally; hosted servers register them on first use). Generated fixtures are synthetic: they are not historical performance and
              their predictive validity is not established.
            </EmptyState>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Origin</th>
                    <th>Chain</th>
                    <th>Duration</th>
                    <th>Use status</th>
                    <th className="num">Exec. pools</th>
                    <th>Gates</th>
                    <th>Period</th>
                    <th>Predictive validity</th>
                  </tr>
                </thead>
                <tbody>
                  {filtered.map((p) => (
                    <tr key={p.pack_id} className={`clickable ${selected === p.pack_id ? "selected" : ""}`} onClick={() => setSelected(p.pack_id)}>
                      <td>
                        <button type="button" className="rowbtn" onClick={() => setSelected(p.pack_id)} aria-expanded={selected === p.pack_id}>
                          {p.name}
                        </button>
                        <div className="muted small mono">{p.pack_id}</div>
                        <div className="row">
                          {p.runnable ? <Badge tone="ok">runnable</Badge> : <Badge tone="bad">not runnable</Badge>}
                          {p.diagnostic_only && <Badge tone="muted">diagnostic only</Badge>}
                        </div>
                      </td>
                      <td>
                        <Badge tone={p.origin === "generated_fixture" ? "warn" : "neutral"}>{humanize(p.origin)}</Badge>
                      </td>
                      <td>{p.chain}</td>
                      <td>{fmtDuration(p.duration_ms, p.is_full_week)}</td>
                      <td>
                        <Badge tone={toneForStatus(p.use_status)}>{humanize(p.use_status)}</Badge>
                      </td>
                      <td className="num">
                        {p.summary?.pools_executable ?? "?"}/{p.summary?.pools_total ?? "?"}
                      </td>
                      <td>
                        <GateSummary gates={p.summary?.gates} />
                      </td>
                      <td className="small">
                        {p.period_dev_mode ? (
                          <>
                            <Badge tone="warn">development mode</Badge>
                            <div className="mono">
                              {p.period_dev_mode.start_utc} → {p.period_dev_mode.end_utc}
                            </div>
                            <div className="muted">{p.period_dev_mode.note}</div>
                          </>
                        ) : (
                          <span className="muted">n/a</span>
                        )}
                      </td>
                      <td>
                        <Badge tone="muted">{humanize(p.predictive_validity || "not_established")}</Badge>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
      {sel && <PackDetail pack={sel} onClose={() => setSelected(null)} />}
      {role === "admin" && <ImportForm onImported={reload} />}
    </main>
  );
}

function Sel({ label, value, onChange, options }: { label: string; value: string; onChange: (v: string) => void; options: string[] }) {
  return (
    <label className="field">
      {label}
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">all</option>
        {options.map((o) => (
          <option key={o} value={o}>
            {humanize(o)}
          </option>
        ))}
      </select>
    </label>
  );
}

function PackDetail({ pack: p, onClose }: { pack: Pack; onClose: () => void }) {
  const { data: val, error } = useLoad(() => get<Validation>(`/packs/${p.pack_id}/validation`), [p.pack_id]);
  const s = p.summary;
  const la = s?.latency_assumptions;
  const u = s?.universe;
  const r = s?.rights;
  return (
    <Card
      title={`Pack: ${p.name}`}
      className="stack"
      actions={
        <>
          <Link className="btn btn-small" to={`/data-health/${p.pack_id}`}>
            Data health
          </Link>
          <Link className="btn btn-small" to={`/runs?pack_id=${p.pack_id}`}>
            Runs
          </Link>
          <Link className="btn btn-small btn-primary" to="/new">
            New run
          </Link>
          <button type="button" className="btn btn-small" onClick={onClose}>
            Close
          </button>
        </>
      }
    >
      <div className="grid">
        <div>
          <h3>Scope</h3>
          <KV
            rows={[
              ["Scope", p.scope_label],
              ["Episode", <span className="mono">{p.episode_id}</span>],
              ["Chain", p.chain],
              ["Origin", humanize(p.origin)],
              ["Duration", fmtDuration(p.duration_ms, p.is_full_week)],
              ["Prehistory", fmtMs(s?.prehistory_ms)],
              ["Execution model", humanize(p.execution_model)],
              ["Availability basis", humanize(s?.availability_basis)],
              ["Token behaviour basis", humanize(s?.token_behavior_basis)],
              ["Numeraire", `${s?.numeraire_alias ?? "?"} (${s?.numeraire_decimals ?? "?"} decimals)`],
              ["Imported", fmtDate(p.imported_at)],
              ["Scenario", s?.scenario ?? "n/a"],
              ["Supported actions", p.supported_actions?.join(", ") || "n/a"],
              ["Unsupported capabilities", p.unsupported_capabilities?.join(", ") || "none"],
              ["Executable failure", s?.executable_failure ?? "none"],
              ["Predictive validity", humanize(p.predictive_validity || "not_established")],
            ]}
          />
        </div>
        <div>
          <h3>Universe</h3>
          {u ? (
            <KV
              rows={[
                ["Description", u.description],
                ["Factories", u.factories?.join(", ")],
                ["Pool models", u.pool_models?.join(", ")],
                ["Quote asset", u.quote_asset],
                ["Selection rule", u.selection_rule_version],
                ["Candidates", u.candidate_count],
                ["Selected", u.selected_count],
                ["Unsupported", u.unsupported_count],
                ["Missing", u.missing_count],
                ["Pools executable / total", `${s.pools_executable} / ${s.pools_total}`],
                ["Assets", s.assets_total],
                ["Tape events", s.tape_events],
                ["Coverage states", Object.entries(s.coverage_states ?? {}).map(([k, v]) => `${k}: ${v}`).join(", ") || "n/a"],
              ]}
            />
          ) : (
            <p className="muted">No universe summary.</p>
          )}
        </div>
        <div>
          <h3>Latency assumptions {la && <Badge tone={la.is_measured ? "ok" : "warn"}>{la.is_measured ? "measured" : "assumed"}</Badge>}</h3>
          {la ? (
            <KV
              rows={[
                ["Profile", `${la.label} (${la.profile_name})`],
                ["Block interval", fmtMs(la.block_interval_ms)],
                ["Data latency", fmtMs(la.data_latency_ms)],
                ["Quote latency", fmtMs(la.quote_latency_ms)],
                ["Submit latency", fmtMs(la.submit_latency_ms)],
                ["Confirm blocks", la.confirm_blocks],
                ["Quote TTL", fmtMs(la.quote_ttl_ms)],
                ["Availability delay", fmtMs(la.availability_delay_ms)],
                ["Gas cost (raw)", <span className="mono">{la.gas_cost_raw}</span>],
                ["Gas basis", humanize(la.gas_basis)],
                ["Settlement tail blocks", la.settlement_tail_blocks],
                ["Capacity profile", `${la.capacity_profile?.version}: max input ${la.capacity_profile?.max_input_bps_of_reserve} bps of reserve, max cumulative displacement ${la.capacity_profile?.max_cumulative_displacement_bps} bps`],
                ["Capacity note", la.capacity_profile?.note],
              ]}
            />
          ) : (
            <p className="muted">No latency profile.</p>
          )}
        </div>
        <div>
          <h3>Rights</h3>
          {r ? (
            <KV
              rows={[
                ["Storage", humanize(r.storage_basis)],
                ["Local processing", humanize(r.local_processing_basis)],
                ["Redistribution", humanize(r.redistribution)],
                ["Simulator serving", humanize(r.simulator_serving)],
                ["Notes", Array.isArray(r.notes) ? r.notes.join("; ") : r.notes],
              ]}
            />
          ) : (
            <p className="muted">No rights record.</p>
          )}
          <h3>Provenance notes</h3>
          <StrList items={s?.provenance_notes} empty="No provenance notes." />
          <h3>Unsupported inventory</h3>
          {s?.unsupported_inventory?.length ? (
            <ul className="plain">
              {s.unsupported_inventory.map((x, i) => (
                <li key={i}>
                  <span className="mono">{x.pool}</span> . {x.reason}
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted">None.</p>
          )}
        </div>
      </div>
      <h3>Validation gates</h3>
      {error && <ErrorState error={error} />}
      {val ? (
        <>
          <KV
            rows={[
              ["Validator", val.validator_version],
              ["Requested qualification", humanize(val.requested_qualification)],
              ["Resulting qualification", <Badge tone={toneForStatus(val.resulting_qualification)}>{humanize(val.resulting_qualification)}</Badge>],
              ["Executable failure", val.executable_failure ?? "none"],
              ["Predictive validity", humanize(val.predictive_validity)],
            ]}
          />
          <GateList gates={val.gates} />
          <StrList items={val.notes} empty="" />
        </>
      ) : (
        <GateList gates={s?.gates} />
      )}
      <JsonView value={p} />
    </Card>
  );
}

function ImportForm({ onImported }: { onImported: () => void }) {
  const [path, setPath] = useState("");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<unknown>(null);
  const [ok, setOk] = useState<Pack | null>(null);
  return (
    <Card title="Import pack">
      <form
        className="form cols"
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          setErr(null);
          setOk(null);
          try {
            const p = await post<Pack>("/packs/import", name ? { path, name } : { path });
            setOk(p);
            setPath("");
            setName("");
            onImported();
          } catch (x) {
            setErr(x);
          } finally {
            setBusy(false);
          }
        }}
      >
        <label className="field">
          Path (server-local directory or archive)
          <input required value={path} onChange={(e) => setPath(e.target.value)} placeholder="data/packs/generated-dev-v1/…" />
        </label>
        <label className="field">
          Name (optional)
          <input value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <div className="span2 row">
          <button className="btn btn-primary" disabled={busy || !path}>
            {busy ? "Importing…" : "Import"}
          </button>
          {ok && (
            <span>
              Imported <strong>{ok.name}</strong> <Badge tone={toneForStatus(ok.use_status)}>{humanize(ok.use_status)}</Badge>
            </span>
          )}
        </div>
      </form>
      {err !== null && <ErrorState error={err} />}
      <p className="muted small">
        No packs yet? Run <code>make demo</code> or <code>market-replay fixtures generate</code> to create development fixtures, then import the generated directory here.
      </p>
    </Card>
  );
}


function lastMonday(minAgeDays = 8): string {
  const d = new Date();
  d.setUTCHours(0, 0, 0, 0);
  d.setUTCDate(d.getUTCDate() - minAgeDays);
  while (d.getUTCDay() !== 1) d.setUTCDate(d.getUTCDate() - 1);
  return d.toISOString().slice(0, 10);
}

/** Anyone can ask for a real past week. The server collects it in slices; this panel shows progress. */
function RealWeeks({ onBuilt }: { onBuilt: () => void }) {
  const { role, meta } = useRole();
  const enabled = meta?.weeks_enabled === true;
  const weeks = useLoad(() => get<{ enabled: boolean; paused?: boolean; usage?: { requests_last_24h: number; max_requests_per_day: number; capped: boolean } | null; items: WeekJob[] }>("/weeks"), [], 6000);
  const [date, setDate] = useState(lastMonday());
  const [protocol, setProtocol] = useState("uniswap_v4");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<unknown>(null);
  const items = weeks.data?.items ?? [];
  const paused = weeks.data?.paused === true;
  const usage = weeks.data?.usage ?? null;
  const active = !paused && !usage?.capped && items.some((j) => j.status === "queued" || j.status === "collecting");
  const builtCount = useRef(0);
  useEffect(() => {
    const n = items.filter((j) => j.status === "built").length;
    if (n > builtCount.current) onBuilt();
    builtCount.current = n;
  }, [items, onBuilt]);
  // While a page is open and a week is in progress, keep the collection moving without waiting for the cron.
  useEffect(() => {
    if (!active) return;
    let stop = false;
    const kick = () => post("/weeks/tick?slice=50").catch(() => undefined).finally(() => !stop && setTimeout(kick, 2000));
    kick();
    return () => {
      stop = true;
    };
  }, [active]);
  if (!enabled && items.length === 0) {
    return role === "admin" ? <p className="muted small">Real weeks are off: set BASE_RPC_URL on the server to let anyone add a past week.</p> : null;
  }
  return (
    <Card title="Add a past week">
      <p>
        Pick a Monday and a venue. The server records what actually happened on Base that week, straight from the chain, so agents can replay it. Uniswap v4 is where Clanker and Bankr tokens trade; v2 pairs are the older pools. Recording takes one to three hours; the week appears on the leaderboard when it is done.
      </p>
      <p className="small muted">
        Weeks are labelled by their dates. Inside a session the pools and tokens carry generic names, so an agent cannot look a token's history up; the dates are for you, not for the agent.
      </p>
      {usage && (
        <p className="small muted">
          Spending guard: {usage.requests_last_24h.toLocaleString()} of {usage.max_requests_per_day.toLocaleString()} RPC requests used in the last 24 hours
          {usage.capped ? "; the daily cap is reached, collection resumes when it clears" : ""}
          {paused ? ". Collection is paused by the operator (MARKET_REPLAY_WEEKS_PAUSED=1)." : "."}
        </p>
      )}
      {enabled && (
        <form
          className="row"
          onSubmit={async (e) => {
            e.preventDefault();
            setBusy(true);
            setErr(null);
            try {
              await post<WeekJob>("/weeks", { week_start: date, protocol });
              weeks.reload();
            } catch (x) {
              setErr(x);
            } finally {
              setBusy(false);
            }
          }}
        >
          <label className="field">
            Week starting (UTC, a Monday)
            <input id="week-start" type="date" value={date} max={lastMonday()} onChange={(e) => setDate(e.target.value)} />
          </label>
          <label className="field">
            Venue
            <select id="week-protocol" value={protocol} onChange={(e) => setProtocol(e.target.value)}>
              <option value="uniswap_v4">Uniswap v4 pools</option>
              <option value="uniswap_v3">Uniswap v3 pools</option>
              <option value="uniswap_v2">Uniswap v2 pairs</option>
            </select>
          </label>
          <button id="week-add" className="btn btn-primary" disabled={busy || !date} style={{ alignSelf: "end" }}>
            {busy ? "Adding…" : "Add this week"}
          </button>
        </form>
      )}
      {err !== null && <ErrorState error={err} />}
      {weeks.error && <ErrorState error={weeks.error} retry={weeks.reload} />}
      {items.length > 0 && (
        <div className="table-wrap" style={{ marginTop: 8 }}>
          <table>
            <thead>
              <tr>
                <th>Week</th>
                <th>Status</th>
                <th className="num">Requests</th>
                <th>Progress</th>
              </tr>
            </thead>
            <tbody>
              {items.map((j) => (
                <tr key={j.job_id}>
                  <td>
                    {j.label}
                    <div className="muted small">{j.period_start_utc ? `${j.period_start_utc.slice(0, 10)} to ${j.period_end_utc?.slice(0, 10)} UTC` : `${j.duration_hours >= 168 ? "7 days" : `${j.duration_hours}h`}`}</div>
                  </td>
                  <td>
                    <Badge tone={j.status === "built" ? "ok" : j.status === "failed" ? "bad" : "info"}>{j.status}</Badge>
                  </td>
                  <td className="num">
                    {j.requests_used}/{j.request_budget}
                  </td>
                  <td className="small">
                    {j.status === "failed" ? <span className="muted">{j.error}</span> : j.note}
                    {j.status === "built" && j.pack_id && (
                      <>
                        {" "}
                        <Link to="/new">Run it</Link>
                      </>
                    )}
                    {(j.status === "failed" || (j.status === "built" && j.qualification === "diagnostic_only")) && (
                      <>
                        {" "}
                        <button
                          className="btn btn-small"
                          onClick={async () => {
                            try {
                              await post<WeekJob>(`/weeks/${j.job_id}/rebuild`, {});
                              weeks.reload();
                            } catch (x) {
                              setErr(x);
                            }
                          }}
                        >
                          Rebuild
                        </button>
                      </>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="muted small" style={{ marginBottom: 0 }}>
        What a real week does not model: gas (assumed zero), token transfer taxes (assumed standard), MEV and routing. Provider completeness is not independently verified. Every report on it says so.
      </p>
    </Card>
  );
}
