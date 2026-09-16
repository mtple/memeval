import { Link, useNavigate, useParams } from "react-router-dom";
import { get, list, type Pack, type PackHealth } from "../api";
import { fmtDate, fmtRel, humanize } from "../format";
import { Badge, Card, EmptyState, ErrorState, GateList, JsonView, KV, Loading, StrList, toneForStatus, useLoad } from "../ui";

const count = (v: unknown[] | number | undefined | null): number => (Array.isArray(v) ? v.length : typeof v === "number" ? v : 0);

export default function DataHealth() {
  const { packId } = useParams();
  const nav = useNavigate();
  const packs = useLoad(() => list<Pack>("/packs"), []);
  const health = useLoad(() => (packId ? get<PackHealth>(`/packs/${packId}/health`) : Promise.resolve(null)), [packId]);
  const descriptor = useLoad(() => (packId ? get<unknown>(`/packs/${packId}/descriptor`) : Promise.resolve(null)), [packId]);
  const H = health.data;

  return (
    <main className="stack">
      <h1>Data health</h1>
      <div className="row">
        <label className="field" style={{ minWidth: 260 }}>
          Pack
          <select value={packId ?? ""} onChange={(e) => nav(e.target.value ? `/data-health/${e.target.value}` : "/data-health")}>
            <option value="">select a pack…</option>
            {(packs.data ?? []).map((p) => (
              <option key={p.pack_id} value={p.pack_id}>
                {p.name} — {p.chain}, {humanize(p.use_status)}
              </option>
            ))}
          </select>
        </label>
        {packId && (
          <Link to={`/runs?pack_id=${packId}`} className="btn btn-small" style={{ alignSelf: "end" }}>
            Runs on this pack
          </Link>
        )}
      </div>
      {packs.error && <ErrorState error={packs.error} retry={packs.reload} />}
      {packs.data && packs.data.length === 0 && (
        <EmptyState title="No packs imported.">
          <Link to="/episodes">Import a pack on Episodes</Link> (generate fixtures with <code>make demo</code>).
        </EmptyState>
      )}
      {!packId && packs.data && packs.data.length > 0 && <p className="muted">Select a pack to inspect its universe, ingestion, coverage, rights and decision log.</p>}
      {packId && health.error && <ErrorState error={health.error} retry={health.reload} />}
      {packId && health.loading && !H && <Loading what="pack health" />}
      {H && (
        <>
          <div className="grid">
            <Card title="Universe boundaries">
              <KV
                rows={[
                  ["Description", H.universe?.description],
                  ["Factories", H.universe?.factories?.join(", ")],
                  ["Pool models", H.universe?.pool_models?.join(", ")],
                  ["Quote asset", H.universe?.quote_asset],
                  ["Selection rule", H.universe?.selection_rule_version],
                ]}
              />
              <div className="metrics" style={{ marginTop: 8 }}>
                <Metric label="Candidates" v={H.universe?.candidate_count} />
                <Metric label="Selected" v={H.universe?.selected_count} />
                <Metric label="Unsupported" v={H.universe?.unsupported_count} warn />
                <Metric label="Missing" v={H.universe?.missing_count} warn />
              </div>
            </Card>
            <Card title="Ingestion">
              <div className="metrics">
                <Metric label="Tape events" v={H.ingestion?.tape_events} />
                <Metric label="Blocks table rows" v={H.ingestion?.blocks_table_rows} />
                <Metric label="Indexed block ranges" v={H.ingestion?.indexed_block_ranges?.length} />
                <Metric label="Coverage intervals" v={H.coverage?.intervals} />
              </div>
              {H.ingestion?.indexed_block_ranges?.length > 0 && <JsonView value={H.ingestion.indexed_block_ranges} />}
              <KV
                rows={[
                  ["Pack imported", fmtDate(H.pack?.imported_at)],
                  ["Pools executable / total", `${H.pack?.summary?.pools_executable} / ${H.pack?.summary?.pools_total}`],
                  ["Executable failure", H.pack?.summary?.executable_failure ?? "none"],
                ]}
              />
            </Card>
            <Card title="Observations">
              <div className="metrics">
                <Metric label="Duplicates suspected" v={count(H.duplicates_suspected)} warn />
                <Metric label="Unpublished observations" v={count(H.unpublished_observations)} warn />
                <Metric label="Corrections" v={count(H.corrections)} />
                <Metric label="Restriction observations" v={H.restriction_observations} warn />
                <Metric label="Source disagreements" v={count(H.source_disagreements)} warn />
              </div>
              {Array.isArray(H.duplicates_suspected) && H.duplicates_suspected.length > 0 && <JsonView value={H.duplicates_suspected} />}
              {Array.isArray(H.unpublished_observations) && H.unpublished_observations.length > 0 && <JsonView value={H.unpublished_observations} />}
              {H.corrections?.length > 0 && <JsonView value={H.corrections} />}
            </Card>
          </div>

          <Card title={`Coverage: non-complete intervals (${H.coverage?.non_complete_count ?? 0})`}>
            {H.coverage?.non_complete_intervals?.length ? (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Object</th>
                      <th>Field</th>
                      <th>Start (UTC)</th>
                      <th>End (UTC)</th>
                      <th>State</th>
                      <th>Evidence</th>
                      <th>Gaps</th>
                    </tr>
                  </thead>
                  <tbody>
                    {H.coverage.non_complete_intervals.map((iv, i) => (
                      <tr key={i}>
                        <td className="mono small">{iv.object_ref}</td>
                        <td className="mono small">{iv.field}</td>
                        <td className="mono small">{fmtDate(iv.start_utc_ms)}</td>
                        <td className="mono small">{fmtDate(iv.end_utc_ms)}</td>
                        <td>
                          <Badge tone={iv.state === "complete" ? "ok" : "warn"}>{humanize(iv.state)}</Badge>
                        </td>
                        <td className="small">{iv.evidence}</td>
                        <td className="small">{iv.gaps === null || iv.gaps === undefined ? "" : typeof iv.gaps === "object" ? JSON.stringify(iv.gaps) : String(iv.gaps)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="muted">All coverage intervals are complete.</p>
            )}
          </Card>

          <div className="grid">
            <Card title={`Missing state pools (${H.missing_state_pools?.length ?? 0})`}>
              <StrList items={H.missing_state_pools?.map((p) => <span key={p} className="mono small">{p}</span>)} empty="None." />
              {H.missing_inventory?.length > 0 && (
                <>
                  <h3>Missing inventory</h3>
                  <JsonView value={H.missing_inventory} open />
                </>
              )}
            </Card>
            <Card title={`Unsupported inventory (${H.unsupported_inventory?.length ?? 0})`}>
              {H.unsupported_inventory?.length ? (
                <ul className="gate-list">
                  {H.unsupported_inventory.map((u, i) => (
                    <li key={i}>
                      <span className="mono small">{u.pool}</span> {u.model && <Badge tone="muted">{u.model}</Badge>} <span className="small">— {u.reason}</span>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="muted">None.</p>
              )}
            </Card>
            <Card title={`Source disagreements (${count(H.source_disagreements)})`}>
              {Array.isArray(H.source_disagreements) && H.source_disagreements.length > 0 ? <JsonView value={H.source_disagreements} open /> : <p className="muted">None recorded.</p>}
            </Card>
          </div>

          <div className="grid">
            <Card title="Assumptions and rights">
              <KV rows={[["Token behaviour assumption", <Badge tone="warn">{humanize(H.token_behavior_basis)}</Badge>]]} />
              <h3>Rights (separate statuses)</h3>
              <KV
                rows={[
                  ["Storage", <Badge tone="neutral">{humanize(H.rights?.storage_basis)}</Badge>],
                  ["Local processing", <Badge tone="neutral">{humanize(H.rights?.local_processing_basis)}</Badge>],
                  ["Redistribution", <Badge tone="neutral">{humanize(H.rights?.redistribution)}</Badge>],
                  ["Simulator serving", <Badge tone="neutral">{humanize(H.rights?.simulator_serving)}</Badge>],
                  ["Notes", Array.isArray(H.rights?.notes) ? H.rights.notes.join("; ") : H.rights?.notes],
                ]}
              />
            </Card>
            <Card title="Validation">
              {H.validation ? (
                <>
                  <KV
                    rows={[
                      ["Validator", H.validation.validator_version],
                      ["Requested → resulting", <>{humanize(H.validation.requested_qualification)} → <Badge tone={toneForStatus(H.validation.resulting_qualification)}>{humanize(H.validation.resulting_qualification)}</Badge></>],
                      ["Executable failure", H.validation.executable_failure ?? "none"],
                      ["Predictive validity", humanize(H.validation.predictive_validity)],
                    ]}
                  />
                  <GateList gates={H.validation.gates} />
                  <StrList items={H.validation.notes} empty="" />
                </>
              ) : (
                <p className="muted">No validation record.</p>
              )}
            </Card>
            <Card title="Attempts and exposure">
              <KV rows={[["Exposed runs", H.exposed_runs]]} />
              {H.attempts?.length ? (
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>Agent</th>
                        <th className="num">Attempts</th>
                      </tr>
                    </thead>
                    <tbody>
                      {H.attempts.map((a, i) => (
                        <tr key={i}>
                          <td>
                            <Link to={`/runs?agent_id=${a.agent_id}&pack_id=${a.pack_id}`} className="mono small">
                              {a.agent_id}
                            </Link>
                          </td>
                          <td className="num">{a.count}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <p className="muted">No attempts recorded for this pack.</p>
              )}
            </Card>
          </div>

          <Card title="Decision log">
            {H.decision_log?.length ? (
              <ol className="plain">
                {H.decision_log.map((d, i) => (
                  <li key={i} className="small">
                    {d}
                  </li>
                ))}
              </ol>
            ) : (
              <p className="muted">Empty.</p>
            )}
          </Card>
          <Card title="Public descriptor">
            {descriptor.error && <ErrorState error={descriptor.error} />}
            {descriptor.data ? <pre>{JSON.stringify(descriptor.data, null, 2)}</pre> : <Loading what="descriptor" />}
          </Card>
          <p className="muted small">
            Prehistory available to participants: {fmtRel(-(H.pack?.summary?.prehistory_ms ?? 0))} relative to episode start.
          </p>
          <JsonView value={H} />
        </>
      )}
    </main>
  );
}

function Metric({ label, v, warn }: { label: string; v: number | undefined; warn?: boolean }) {
  return (
    <div className="metric">
      <span className="lbl">{label}</span>
      <span className="val" style={warn && v ? { color: "var(--warn-fg)" } : undefined}>
        {v ?? "—"}
      </span>
    </div>
  );
}
