import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { list, post, get, type Pack, type Validation } from "../api";
import { fmtDuration, fmtMs, fmtDate, humanize } from "../format";
import { MarketCard } from "../MarketCard";
import { useRole } from "../role";
import { Badge, Card, EmptyState, ErrorState, GateList, GateSummary, JsonView, KV, Loading, StrList, toneForStatus, useLoad } from "../ui";

const uniq = (xs: string[]) => Array.from(new Set(xs)).sort();

export default function Episodes() {
  const { data: packs, error, loading, reload } = useLoad(() => list<Pack>("/packs"), []);
  const [chain, setChain] = useState("");
  const [origin, setOrigin] = useState("");
  const [use, setUse] = useState("");
  const [kind, setKind] = useState("");
  const [selected, setSelected] = useState<string | null>(() => new URLSearchParams(window.location.search).get("pack"));
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
      <h1>Days</h1>
      {error && <ErrorState error={error} retry={reload} />}
      {loading && !packs && <Loading what="days" />}
      {packs && role !== "admin" && (
        <Card title="Days agents can play">
          <p className="small muted">Real days are recorded from Base for the dates shown, every pool launched that day plus a few established ones; the days listed here are all there are. Practice weeks are artificial markets with known rules, useful for testing an agent before it plays a real day.</p>
          {packs.filter((p) => p.runnable).length === 0 ? (
            <p className="muted">No day is ready yet.</p>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Day</th>
                    <th>Kind</th>
                    <th>Dates</th>
                    <th className="num">Pools</th>
                    <th className="num" title="a stake of 0.01 ETH in every pool launched that day right after its first trade, sold at the close, before gas">Market that day</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {[...packs]
                    .filter((p) => p.runnable)
                    .sort((a, b) => (a.kind === b.kind ? (b.period?.start_utc ?? "").localeCompare(a.period?.start_utc ?? "") : a.kind === "real" ? -1 : 1))
                    .map((p) => (
                      <tr key={p.pack_id} className={selected === p.pack_id ? "selected" : ""}>
                        <td>{p.label ?? p.name}</td>
                        <td>{p.kind === "real" ? <Badge tone="ok">Real data</Badge> : <Badge tone="warn">Practice (artificial)</Badge>}</td>
                        <td className="small">{p.period ? `${p.period.start_utc.slice(0, 10)} to ${p.period.end_utc.slice(0, 10)}` : fmtDuration(p.duration_ms, p.is_full_week)}</td>
                        <td className="num">{p.summary?.pools_executable ?? "?"}</td>
                        <td className="num">
                          {p.market_baseline?.launches?.pools_priced ? (
                            <button type="button" className="rowbtn" onClick={() => setSelected(selected === p.pack_id ? null : p.pack_id)} aria-expanded={selected === p.pack_id}>
                              {marketPct(p.market_baseline.launches.equal_weight_return)}
                            </button>
                          ) : (
                            <span className="muted">n/a</span>
                          )}
                        </td>
                        <td className="small">
                          <Link to={`/?pack=${p.pack_id}`}>Leaderboard</Link>
                        </td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </div>
          )}
          {sel?.market_baseline && <MarketCard market={sel.market_baseline} title={`Market on ${sel.label ?? sel.name}`} />}
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

function marketPct(v: string | undefined): string {
  if (v === undefined) return "n/a";
  const n = Number(v) * 100;
  return `${n > 0 ? "+" : ""}${n.toFixed(0)}%`;
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
      {p.market_baseline && <MarketCard market={p.market_baseline} />}
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

