import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError, get, post, type Holding, type Observed, type Pack, type Run } from "../api";
import { CandleChart, EquitySparkline } from "../charts";
import { fmtDate, fmtRaw, fmtRel, humanize, shortHash, unitLabel } from "../format";
import { useRole } from "../role";
import { Badge, Card, ErrorState, JsonView, KV, Loading, RunStateBadge, useLoad } from "../ui";

const ACTIVE = new Set(["queued", "running", "paused"]);

export default function RunDetail() {
  const { id = "" } = useParams();
  const [active, setActive] = useState(true);
  const run = useLoad(() => get<Run>(`/runs/${id}`), [id], active ? 1500 : null);
  const pack = useLoad(() => (run.data?.pack_id ? get<Pack>(`/packs/${run.data.pack_id}`) : Promise.resolve(null)), [run.data?.pack_id]);
  useEffect(() => {
    if (run.data) setActive(ACTIVE.has(run.data.state));
  }, [run.data]);
  const [actErr, setActErr] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const { role } = useRole();

  const r = run.data;
  const dec = pack.data?.summary?.numeraire_decimals;
  const unit = unitLabel(pack.data?.summary?.numeraire_alias);
  const live = r?.live ?? null;
  const clock = live?.clock_ms ?? r?.clock_ms ?? 0;
  const duration = live?.duration_ms ?? pack.data?.duration_ms ?? 0;
  const progress = duration > 0 ? Math.max(0, Math.min(1, clock / duration)) : 0;

  const act = async (verb: "pause" | "resume" | "abort") => {
    if (verb === "abort" && !window.confirm("Abort this run? This cannot be undone.")) return;
    setBusy(true);
    setActErr(null);
    try {
      run.setData(await post<Run>(`/runs/${id}/${verb}`));
    } catch (x) {
      setActErr(x);
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="stack">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h1 style={{ margin: 0 }}>{r ? `${r.agent_name ?? "Agent"} on ${r.pack_label ?? r.pack_name ?? "a day"}` : "Run"}</h1>
        <Link to="/runs" className="btn btn-small">
          All runs
        </Link>
      </div>
      {run.error && <ErrorState error={run.error} retry={run.reload} />}
      {run.loading && !r && <Loading what="run" />}
      {r && (
        <>
          <Card
            title="State"
            actions={
              <>
                {role === "admin" && r.state === "running" && (
                  <button type="button" className="btn btn-small" disabled={busy} onClick={() => act("pause")}>
                    Pause
                  </button>
                )}
                {role === "admin" && r.state === "paused" && (
                  <button type="button" className="btn btn-small" disabled={busy} onClick={() => act("resume")}>
                    Resume
                  </button>
                )}
                {role === "admin" && ACTIVE.has(r.state) && (
                  <button type="button" className="btn btn-small btn-danger" disabled={busy} onClick={() => act("abort")}>
                    Abort
                  </button>
                )}
                {r.has_report && (
                  <Link to={`/runs/${r.run_id}/results`} className="btn btn-small btn-primary">
                    Results
                  </Link>
                )}
              </>
            }
          >
            {actErr !== null && <ErrorState error={actErr} />}
            <div className="row">
              <RunStateBadge state={r.state} />
              {active && <span className="muted small">polling every 1.5s</span>}
              {r.exposed && <Badge tone="warn">exposed</Badge>}
            </div>
            {r.error && <p className={`notice ${r.state === "agent_failed" ? "bad" : ""}`}>{r.state === "agent_failed" ? "Agent error: " : r.state === "environment_failed" ? "Environment/data error: " : ""}{r.error}</p>}
            <div className="metrics" style={{ marginTop: 8 }}>
              <Metric label="Virtual clock" value={fmtRel(clock)} />
              <Metric label="Remaining" value={fmtRel(live?.remaining_ms ?? (duration ? duration - clock : null))} />
              <Metric label="Duration" value={fmtRel(duration)} />
              <Metric label="Cash available" value={fmtRaw(live?.cash_available_raw, dec)} sub={unit} />
              <Metric label="Cash reserved" value={fmtRaw(live?.cash_reserved_raw, dec)} sub={unit} />
              <Metric
                label="Model equity"
                value={live ? (live.model_equity_raw === null ? "incomplete" : fmtRaw(live.model_equity_raw, dec)) : "n/a"}
                sub={live && !live.valuation_complete ? "valuation incomplete" : unit}
                tone={live && !live.valuation_complete ? "warn" : undefined}
              />
            </div>
            <div className="progress" style={{ marginTop: 8 }} role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(progress * 100)} aria-label="Episode progress">
              <div style={{ width: `${progress * 100}%` }} />
            </div>
            <KV
              rows={[
                ["Day", <Link to={`/episodes?pack=${r.pack_id}`}>{r.pack_label ?? r.pack_name ?? "a recorded day"}</Link>],
                ["Agent", <Link to={`/agents/${r.agent_id}`}>{r.agent_name ?? "unnamed agent"}</Link>],
                ["Started with", `${fmtRaw(r.bankroll_raw, dec)} ${unit ?? ""}`],
                ["Created / started / finished", `${fmtDate(r.created_at)} / ${fmtDate(r.started_at)} / ${fmtDate(r.finished_at)}`],
                ["Calls / decisions / orders", live ? `${live.requests} / ${live.decisions} / ${live.orders_total} (${live.pending_orders} pending)` : "n/a"],
              ]}
            />
          </Card>

          {live && (
            <div className="grid-2">
              <Card title="Holdings by class">
                <HoldingsTable holdings={live.holdings} dec={dec} />
              </Card>
              <Card title="Data health and isolation">
                <div className="row">
                  <Badge tone={live.data_health?.rate_limited ? "warn" : "ok"}>rate limited: {String(live.data_health?.rate_limited ?? 0)}</Badge>
                  <Badge tone={live.data_health?.invalid_calls ? "warn" : "ok"}>invalid calls: {String(live.data_health?.invalid_calls ?? 0)}</Badge>
                  <Badge tone={live.data_health?.budget_exhausted ? "bad" : "ok"}>budget exhausted: {live.data_health?.budget_exhausted ? "yes" : "no"}</Badge>
                </div>
                <h3>Fidelity flags</h3>
                {live.fidelity_flags?.length ? (
                  <div className="row">
                    {live.fidelity_flags.map((f) => (
                      <Badge key={f} tone="warn">
                        {f}
                      </Badge>
                    ))}
                  </div>
                ) : (
                  <p className="muted">None raised.</p>
                )}
                <h3>Isolation controls</h3>
                <IsolationControls controls={live.isolation_controls} />
                <h3>Recent actions</h3>
                {live.recent_actions?.length ? (
                  <div className="table-wrap">
                    <table>
                      <thead>
                        <tr>
                          <th>Clock</th>
                          <th>Tool</th>
                          <th>Status</th>
                          <th>Error</th>
                        </tr>
                      </thead>
                      <tbody>
                        {live.recent_actions.map((a, i) => (
                          <tr key={i}>
                            <td className="mono">{fmtRel(a.clock_ms)}</td>
                            <td className="mono">{a.tool}</td>
                            <td>
                              <Badge tone={a.status === "ok" ? "ok" : "bad"}>{a.status}</Badge>
                            </td>
                            <td className="mono small">{a.error_code ?? ""}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ) : (
                  <p className="muted">No actions yet.</p>
                )}
              </Card>
            </div>
          )}

          <ObservedPanel runId={id} clock={clock} dec={dec} active={active || r.state === "completed"} />
          <JsonView value={r} />
        </>
      )}
    </main>
  );
}

function Metric({ label, value, sub, tone }: { label: string; value: string; sub?: string | null; tone?: "warn" }) {
  return (
    <div className="metric">
      <span className="lbl">{label}</span>
      <span className="val" style={tone === "warn" ? { color: "var(--warn-fg)" } : undefined}>
        {value}
      </span>
      {sub && <span className="lbl">{sub}</span>}
    </div>
  );
}

function IsolationControls({ controls }: { controls: Record<string, unknown> | undefined }) {
  const entries = Object.entries(controls ?? {});
  if (!entries.length) return <p className="muted">No isolation controls reported.</p>;
  return (
    <div className="row">
      {entries.map(([k, v]) => {
        const on = v === true || v === "enforced";
        return (
          <Badge key={k} tone={on ? "ok" : "warn"}>
            {humanize(k)}: {typeof v === "boolean" ? (v ? "enforced" : "unenforced") : String(v)}
          </Badge>
        );
      })}
    </div>
  );
}

const CLASS_LABEL: Record<string, [string, "ok" | "warn" | "bad"]> = {
  priced_liquidatable: ["priced", "ok"],
  no_route: ["no route", "warn"],
  unpriced_missing_data: ["unpriced (missing data)", "bad"],
};

function HoldingsTable({ holdings, dec }: { holdings: Holding[]; dec: number | undefined }) {
  if (!holdings?.length) return <p className="muted">No holdings (cash only).</p>;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Asset</th>
            <th>Class</th>
            <th className="num">Quantity (raw)</th>
            <th className="num">Model value</th>
          </tr>
        </thead>
        <tbody>
          {holdings.map((h) => {
            const [label, tone] = CLASS_LABEL[h.class] ?? [h.class, "warn"];
            return (
              <tr key={h.asset_id}>
                <td className="mono small">{h.asset_id}</td>
                <td>
                  <Badge tone={tone}>{label}</Badge>
                </td>
                <td className="num">{h.quantity_raw}</td>
                <td className="num">{h.model_value_raw === null ? <span className="muted">not valued</span> : fmtRaw(h.model_value_raw, dec)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function ObservedPanel({ runId, clock, dec, active }: { runId: string; clock: number; dec: number | undefined; active: boolean }) {
  const [pool, setPool] = useState("");
  const [interval, setInterval_] = useState(60000);
  const obs = useLoad(
    () => get<Observed>(`/runs/${runId}/observed?interval_ms=${interval}${pool ? `&pool_id=${encodeURIComponent(pool)}` : ""}`),
    [runId, pool, interval],
    active ? 3000 : null,
  );
  const gone = obs.error instanceof ApiError && obs.error.status === 404;
  const d = obs.data;
  const asOf = d?.clock_ms ?? clock;
  return (
    <Card
      title="Observed only, as of the virtual clock"
      actions={
        <>
          <label className="field">
            Pool
            <select value={pool} onChange={(e) => setPool(e.target.value)}>
              <option value="">{d?.series?.pool_id ? `default (${shortHash(d.series.pool_id, 10)})` : "default"}</option>
              {(d?.pools ?? []).map((p) => (
                <option key={p.pool_id} value={p.pool_id}>
                  {p.pool_id}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            Interval
            <select value={interval} onChange={(e) => setInterval_(Number(e.target.value))}>
              <option value={60000}>1m</option>
              <option value={300000}>5m</option>
              <option value={900000}>15m</option>
              <option value={3600000}>1h</option>
            </select>
          </label>
        </>
      }
    >
      <p className="muted small">
        Shows only observations available to the participant at virtual clock <span className="mono">{fmtRel(asOf)}</span>. Nothing after the clock is rendered. Gaps are shaded; partial or unclosed bars are faded.
      </p>
      {gone ? (
        <p className="muted">Observed data is only available while the run is active in the server process. This run is no longer resident; open Results for the terminal report.</p>
      ) : obs.error ? (
        <ErrorState error={obs.error} retry={obs.reload} />
      ) : !d ? (
        <Loading what="observed series" />
      ) : (
        <>
          {d.series ? (
            <>
              <CandleChart bars={d.series.items} gaps={d.series.gaps ?? []} clockMs={asOf} />
              <p className="muted small">
                {d.series.items.filter((b) => !b.synthetic_empty_bar).length} bars ({d.series.items.filter((b) => b.synthetic_empty_bar).length} synthetic empty), {d.series.gaps?.length ?? 0} gaps, series as of{" "}
                {fmtRel(d.series.as_of_ms)}.
                {d.series.gaps?.map((g, i) => (
                  <span key={i}>
                    {" "}
                    gap {fmtRel(g.start_ms)}→{fmtRel(g.end_ms)} ({g.reason});
                  </span>
                ))}
              </p>
            </>
          ) : (
            <p className="muted">No price series for the selected pool.</p>
          )}
          <h3>Model equity (gaps drawn as breaks)</h3>
          <EquitySparkline points={d.equity ?? []} clockMs={asOf} />
          <p className="muted small">
            {d.equity?.length ?? 0} points, {(d.equity ?? []).filter((p) => p.equity_raw === null || !p.complete).length} incomplete.
            {d.equity?.length ? ` Latest: ${(() => { const last = d.equity[d.equity.length - 1]!; return last.equity_raw === null ? "incomplete" : fmtRaw(last.equity_raw, dec); })()} (${d.equity[d.equity.length - 1]!.source})` : ""}
          </p>
          {d.orders?.length ? (
            <>
              <h3>Orders ({d.orders.length})</h3>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Order</th>
                      <th>State</th>
                      <th>Pool</th>
                      <th>In → out</th>
                      <th className="num">Amount in (raw)</th>
                      <th className="num">Amount out (raw)</th>
                      <th>Submitted</th>
                      <th>Filled</th>
                      <th>Reason</th>
                    </tr>
                  </thead>
                  <tbody>
                    {d.orders.map((o, i) => (
                      <tr key={String(o.order_id ?? i)}>
                        <td className="mono small">{shortHash(String(o.order_id ?? ""), 10)}</td>
                        <td>
                          <Badge tone={o.state === "confirmed" ? "ok" : o.state === "pending" || o.state === "submitted" ? "info" : "warn"}>{String(o.state)}</Badge>
                        </td>
                        <td className="mono small">{shortHash(String(o.pool_id ?? ""), 10)}</td>
                        <td className="small">
                          {String(o.asset_in ?? "")} → {String(o.asset_out ?? "")}
                        </td>
                        <td className="num">{String(o.amount_in_raw ?? "n/a")}</td>
                        <td className="num">{o.amount_out_raw === null || o.amount_out_raw === undefined ? "n/a" : String(o.amount_out_raw)}</td>
                        <td className="mono small">{fmtRel(o.submitted_ms as number)}</td>
                        <td className="mono small">{o.fill_time_ms === null || o.fill_time_ms === undefined ? "n/a" : fmtRel(o.fill_time_ms as number)}</td>
                        <td className="small">{o.reason ? String(o.reason) : ""}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          ) : null}
          {d.note && <p className="muted small">{d.note}</p>}
        </>
      )}
    </Card>
  );
}
