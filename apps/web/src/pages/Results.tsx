import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { get, post, type Report, type ReplayResult, type Run } from "../api";
import { DIMENSION_ORDER, dimensionLabel, explainDimension, summarySentence } from "../explain";
import { fmtAmount, fmtDuration, fmtMs, fmtPct, fmtRaw, fmtReturn, humanize, shortHash } from "../format";
import { useRole } from "../role";
import { Badge, Card, ErrorState, JsonView, KV, Loading, StrList, downloadJson, useLoad } from "../ui";

export default function Results() {
  const { id = "" } = useParams();
  const { role } = useRole();
  const rep = useLoad(() => get<Report>(`/runs/${id}/report?role=${role === "admin" ? "admin" : "participant"}`), [id, role]);
  const run = useLoad(() => get<Run>(`/runs/${id}`), [id]);
  const [replay, setReplay] = useState<ReplayResult | null>(null);
  const [replayErr, setReplayErr] = useState<unknown>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [exportErr, setExportErr] = useState<unknown>(null);

  const R = rep.data;
  const dec = R?.outcome.numeraire_decimals;
  const unit = R?.outcome.numeraire;
  const amt = (raw: string | null | undefined) => fmtAmount(raw, dec, unit);

  const doExport = async (role: "admin" | "participant") => {
    setBusy(role);
    setExportErr(null);
    try {
      const bundle = await get<unknown>(`/runs/${id}/export?role=${role}&include_mappings=${role === "admin"}`);
      downloadJson(`market-replay-${shortHash(id, 12).replace("…", "")}-${role}.json`, bundle);
    } catch (x) {
      setExportErr(x);
    } finally {
      setBusy(null);
    }
  };

  return (
    <main className="stack">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h1 style={{ margin: 0 }}>{run.data ? `${run.data.agent_name ?? "Agent"} on ${run.data.pack_name ?? "episode"}` : "Result"}</h1>
        <Link to={`/runs/${id}`} className="btn btn-small">
          Back to run
        </Link>
      </div>
      {rep.error && (
        <>
          <ErrorState error={rep.error} retry={rep.reload} />
          <p className="muted">
            A report exists only for runs that reached a terminal state. Check the run's state on <Link to={`/runs/${id}`}>its run page</Link>.
          </p>
        </>
      )}
      {rep.loading && !R && <Loading what="report" />}
      {R && (
        <>
          <Card title="What happened">
            <p style={{ fontSize: 15, margin: 0 }}>
              {summarySentence({
                agent: run.data?.agent_name ?? "The agent",
                episode: run.data?.pack_label ?? run.data?.pack_name ?? "the episode",
                durationMs: R.coverage_and_assumptions.episode_duration_ms,
                isFullWeek: R.coverage_and_assumptions.is_full_week,
                unit: unit ?? "",
                decimals: dec ?? 0,
                initialRaw: R.outcome.initial_equity_raw,
                terminalRaw: R.outcome.primary_metric === "final_cash_return_v1" ? R.outcome.final_cash_raw ?? null : R.outcome.terminal_model_equity_raw,
                headlineReturn: R.outcome.headline_return,
                valuationComplete: R.outcome.primary_metric === "final_cash_return_v1" || R.outcome.valuation_complete,
                orders: R.activity.orders_total,
                fills: R.activity.confirmed_fills,
                gasRaw: R.costs.gas_total_raw,
                unpriced: (R.unresolved.unpriced_inventory?.length ?? 0) + (R.unresolved.no_route_inventory?.length ?? 0),
              })}
            </p>
            <div className="metrics" style={{ marginTop: 12 }}>
              <M label="Final settled ETH/cash" value={fmtRaw(R.outcome.final_cash_raw ?? null, dec)} sub={unit} />
              <M label="Started with" value={fmtRaw(R.outcome.initial_equity_raw, dec)} sub={unit} />
              <M label={R.outcome.primary_metric === "final_cash_return_v1" ? "Liquidatable portfolio value (secondary)" : "Legacy final portfolio value"} value={R.outcome.terminal_model_equity_raw === null ? "could not be valued" : fmtRaw(R.outcome.terminal_model_equity_raw, dec)} sub={R.outcome.terminal_model_equity_raw === null ? undefined : unit} warn={R.outcome.terminal_model_equity_raw === null} />
              <M label={R.outcome.primary_metric === "final_cash_return_v1" ? "Final ETH/cash return" : "Legacy portfolio return (unranked)"} value={R.outcome.headline_return === null ? "not stated" : fmtReturn(R.outcome.headline_return)} warn={R.outcome.headline_return === null} />
              <M label="Worst drop from a peak" value={R.risk.max_drawdown === null ? "not supportable" : fmtPct(R.risk.max_drawdown)} warn={R.risk.max_drawdown === null} />
              <M label="Orders filled" value={`${R.activity.confirmed_fills} of ${R.activity.orders_total}`} sub={R.activity.reverted || R.activity.expired ? `${R.activity.reverted} reverted, ${R.activity.expired} expired` : undefined} />
              <M label="Gas paid" value={fmtRaw(R.costs.gas_total_raw, dec)} sub={unit} />
            </div>
            {R.outcome.valuation_warnings?.length > 0 && (
              <div className="notice" style={{ marginTop: 10 }}>
                <strong>About the valuation</strong>
                <StrList items={R.outcome.valuation_warnings} />
              </div>
            )}
            {(R.activity.quality_exposure?.invalid_calls ?? 0) + (R.activity.quality_exposure?.rate_limited ?? 0) > 0 && (
              <p className="muted small" style={{ marginTop: 8 }}>
                The agent made {R.activity.quality_exposure?.invalid_calls ?? 0} invalid calls and was rate-limited {R.activity.quality_exposure?.rate_limited ?? 0} times. That is part of the result.
              </p>
            )}
          </Card>

          <Card title="How much to trust this">
            <ul className="plain" style={{ paddingLeft: 18 }}>
              {DIMENSION_ORDER.map((k) => (
                <li key={k}>
                  <strong>{dimensionLabel(k)}.</strong> {explainDimension(k, R.status_dimensions?.[k])}
                </li>
              ))}
              <li>
                <strong>Valuation.</strong>{" "}
                {R.outcome.valuation_complete ? "Every holding could be priced by selling it through the model's own pools, so the final value is complete." : "Some holdings could not be priced, so liquidatable portfolio value is unknown. Final ETH/cash return does not depend on unsold token values."}
              </li>
            </ul>
            {R.coverage_and_assumptions.limitations?.length > 0 && (
              <>
                <h3>Known limits of this simulation</h3>
                <ul className="plain" style={{ paddingLeft: 18 }}>
                  {R.coverage_and_assumptions.limitations.map((l, i) => (
                    <li key={i}>{l}</li>
                  ))}
                </ul>
              </>
            )}
            <p className="statement">{R.statement}</p>
          </Card>

          <details className="more">
            <summary>All the details (risk, costs, activity, unresolved items, assumptions, versions, reproducibility, exports)</summary>
            <div className="stack" style={{ marginTop: 8 }}>
          <div className="grid-2">
            <Card title="Risk">
              <div className="metrics">
                <M label="Max drawdown" value={R.risk.max_drawdown === null ? "not supportable" : fmtPct(R.risk.max_drawdown)} sub={R.risk.drawdown_basis} warn={R.risk.max_drawdown === null} />
                <M label="Equity points complete / total" value={`${R.risk.equity_points_complete} / ${R.risk.equity_points_total}`} sub={`${R.risk.gap_count} gaps`} warn={R.risk.gap_count > 0} />
                <M label="Exposure share of grid" value={fmtPct(R.risk.exposure_share_of_grid)} />
                <M label="Largest position" value={R.risk.largest_position ? fmtPct(R.risk.largest_position.share) : "n/a"} sub={R.risk.largest_position?.asset_id} />
              </div>
              {R.risk.gaps?.length > 0 && <JsonView value={R.risk.gaps} />}
            </Card>
            <Card title="Costs">
              <KV
                rows={[
                  ["Gas total", `${amt(R.costs.gas_total_raw)} (${humanize(R.costs.gas_basis)})`],
                  ["Turnover (numeraire)", amt(R.costs.turnover_numeraire_raw)],
                  ["Implicit pool fee (numeraire)", amt(R.costs.implicit_pool_fee_numeraire_raw)],
                  ["Implicit pool fee (other, raw)", typeof R.costs.implicit_pool_fee_other_raw === "string" ? R.costs.implicit_pool_fee_other_raw : <code>{JSON.stringify(R.costs.implicit_pool_fee_other_raw)}</code>],
                  ["Fee note", R.costs.fee_note],
                ]}
              />
            </Card>
          </div>

          <div className="grid-2">
            <Card title="Activity">
              <KV
                rows={[
                  ["Orders total", R.activity.orders_total],
                  ["Orders by state", Object.entries(R.activity.orders_by_state ?? {}).map(([k, v]) => `${k}: ${v}`).join(", ") || "none"],
                  ["Confirmed / reverted / expired", `${R.activity.confirmed_fills} / ${R.activity.reverted} / ${R.activity.expired}`],
                  ["Model capacity rejected", R.activity.model_capacity_rejected],
                  ["Requests / decisions", `${R.activity.requests_total} / ${R.activity.decisions_total}`],
                  ["Budget exhausted", R.activity.budget_exhausted ? <Badge tone="bad">yes</Badge> : "no"],
                  ["Invalid calls / rate limited", `${R.activity.quality_exposure?.invalid_calls ?? 0} / ${R.activity.quality_exposure?.rate_limited ?? 0}`],
                  ["Errors by code", Object.entries(R.activity.quality_exposure?.errors_by_code ?? {}).map(([k, v]) => `${k}: ${v}`).join(", ") || "none"],
                ]}
              />
              <h3>Tool calls</h3>
              <KV rows={Object.entries(R.activity.tool_calls ?? {}).map(([k, v]) => [<code key={k}>{k}</code>, v])} />
            </Card>
            <Card title="Unresolved">
              <h3>Unresolved orders ({R.unresolved.orders?.length ?? 0})</h3>
              {R.unresolved.orders?.length ? <JsonView value={R.unresolved.orders} open /> : <p className="muted">None.</p>}
              <h3>No-route inventory ({R.unresolved.no_route_inventory?.length ?? 0})</h3>
              <InvTable rows={R.unresolved.no_route_inventory} />
              <h3>Unpriced inventory ({R.unresolved.unpriced_inventory?.length ?? 0})</h3>
              <InvTable rows={R.unresolved.unpriced_inventory} />
              <h3>Environment fidelity flags</h3>
              <StrList items={R.unresolved.environment_fidelity_flags} empty="None raised." />
            </Card>
          </div>

          <Card title="Coverage and assumptions">
            <div className="grid-2">
              <div>
                <KV
                  rows={[
                    ["Episode duration", fmtDuration(R.coverage_and_assumptions.episode_duration_ms, R.coverage_and_assumptions.is_full_week)],
                    ["Universe", R.coverage_and_assumptions.universe?.description],
                    [
                      "Universe counts",
                      `candidates ${R.coverage_and_assumptions.universe?.candidate_count}, selected ${R.coverage_and_assumptions.universe?.selected_count}, unsupported ${R.coverage_and_assumptions.universe?.unsupported_count}, missing ${R.coverage_and_assumptions.universe?.missing_count}`,
                    ],
                    ["Availability model", typeof R.coverage_and_assumptions.availability_model === "string" ? humanize(R.coverage_and_assumptions.availability_model) : <code>{JSON.stringify(R.coverage_and_assumptions.availability_model)}</code>],
                    ["Reconciliation mismatches in run", R.coverage_and_assumptions.reconciliation_mismatches_in_run],
                    [
                      "Reserve checkpoints re-anchored",
                      R.coverage_and_assumptions.reserve_adjustments_in_run && Object.keys(R.coverage_and_assumptions.reserve_adjustments_in_run).length > 0
                        ? Object.entries(R.coverage_and_assumptions.reserve_adjustments_in_run)
                            .map(([k, v]) => `${v} ${k}`)
                            .join(", ")
                        : "none",
                    ],
                  ]}
                />
                <h3>Limitations</h3>
                <StrList items={R.coverage_and_assumptions.limitations} empty="No limitations listed." />
              </div>
              <div>
                <h3>
                  Latency assumptions{" "}
                  <Badge tone={R.coverage_and_assumptions.latency_assumptions?.is_measured ? "ok" : "warn"}>{R.coverage_and_assumptions.latency_assumptions?.is_measured ? "measured" : "assumed"}</Badge>
                </h3>
                {R.coverage_and_assumptions.latency_assumptions && (
                  <KV
                    rows={[
                      ["Profile", `${R.coverage_and_assumptions.latency_assumptions.label} (${R.coverage_and_assumptions.latency_assumptions.profile_name})`],
                      ["Block interval", fmtMs(R.coverage_and_assumptions.latency_assumptions.block_interval_ms)],
                      ["Data / quote / submit latency", `${fmtMs(R.coverage_and_assumptions.latency_assumptions.data_latency_ms)} / ${fmtMs(R.coverage_and_assumptions.latency_assumptions.quote_latency_ms)} / ${fmtMs(R.coverage_and_assumptions.latency_assumptions.submit_latency_ms)}`],
                      ["Confirm blocks / settlement tail", `${R.coverage_and_assumptions.latency_assumptions.confirm_blocks} / ${R.coverage_and_assumptions.latency_assumptions.settlement_tail_blocks}`],
                      ["Quote TTL / availability delay", `${fmtMs(R.coverage_and_assumptions.latency_assumptions.quote_ttl_ms)} / ${fmtMs(R.coverage_and_assumptions.latency_assumptions.availability_delay_ms)}`],
                      ["Gas cost (raw) / basis", `${R.coverage_and_assumptions.latency_assumptions.gas_cost_raw} / ${humanize(R.coverage_and_assumptions.latency_assumptions.gas_basis)}`],
                    ]}
                  />
                )}
                <h3>Capacity profile</h3>
                {R.coverage_and_assumptions.capacity_profile ? (
                  <KV
                    rows={[
                      ["Version", R.coverage_and_assumptions.capacity_profile.version],
                      ["Max input (bps of reserve)", R.coverage_and_assumptions.capacity_profile.max_input_bps_of_reserve],
                      ["Max cumulative displacement (bps)", R.coverage_and_assumptions.capacity_profile.max_cumulative_displacement_bps],
                      ["Note", R.coverage_and_assumptions.capacity_profile.note],
                    ]}
                  />
                ) : (
                  <p className="muted">None.</p>
                )}
              </div>
            </div>
          </Card>

          <div className="grid-2">
            <Card title="Versions">
              <KV rows={Object.entries(R.versions ?? {}).map(([k, v]) => [humanize(k), <span key={k} className="mono">{v}</span>])} />
              <KV rows={[["Report version", R.report_version]]} />
            </Card>
            <Card
              title="Reproducibility"
              actions={
                role === "admin" && (
                <button
                  type="button"
                  className="btn btn-small"
                  disabled={busy === "replay"}
                  onClick={async () => {
                    setBusy("replay");
                    setReplayErr(null);
                    try {
                      setReplay(await post<ReplayResult>(`/runs/${id}/replay`));
                    } catch (x) {
                      setReplayErr(x);
                    } finally {
                      setBusy(null);
                    }
                  }}
                >
                  {busy === "replay" ? "Replaying…" : "Replay actions"}
                </button>
                )
              }
            >
              <KV
                rows={[
                  ["Ledger hash", <span className="mono">{R.reproducibility.ledger_hash}</span>],
                  ["State hash", <span className="mono">{R.reproducibility.state_hash}</span>],
                  ["Trace hash", <span className="mono">{R.reproducibility.trace_hash}</span>],
                  ["Result hash", <span className="mono">{R.reproducibility.result_hash}</span>],
                  ["Trace length", R.reproducibility.trace_length],
                ]}
              />
              {replayErr !== null && <ErrorState error={replayErr} />}
              {replay && (
                <div className="row" style={{ marginTop: 8 }}>
                  <Badge tone={replay.ledger_matches ? "ok" : "bad"}>ledger matches: {String(replay.ledger_matches)}</Badge>
                  <Badge tone={replay.state_matches ? "ok" : "bad"}>state matches: {String(replay.state_matches)}</Badge>
                  <span className="small muted">replayed {replay.trace_length} trace entries</span>
                  <JsonView value={replay} />
                </div>
              )}
            </Card>
          </div>

          <Card
            title="Export"
            actions={
              <>
                {role === "admin" && (
                  <button type="button" className="btn btn-small" disabled={busy === "admin"} onClick={() => doExport("admin")}>
                    Download admin bundle
                  </button>
                )}
                <button type="button" className="btn btn-small" disabled={busy === "participant"} onClick={() => doExport("participant")}>
                  Download participant bundle
                </button>
              </>
            }
          >
            <p className="muted small">The admin bundle includes mappings; the participant bundle is blinded. Both are JSON.</p>
            {exportErr !== null && <ErrorState error={exportErr} />}
          </Card>

            </div>
          </details>
          <JsonView value={R} />
        </>
      )}
    </main>
  );
}

function M({ label, value, sub, warn }: { label: string; value: string; sub?: string | null; warn?: boolean }) {
  return (
    <div className="metric">
      <span className="lbl">{label}</span>
      <span className="val" style={warn ? { color: "var(--warn-fg)" } : undefined}>
        {value}
      </span>
      {sub && <span className="lbl">{sub}</span>}
    </div>
  );
}

function InvTable({ rows }: { rows: { asset_id: string; quantity_raw: string; reason?: string }[] | undefined }) {
  if (!rows?.length) return <p className="muted">None.</p>;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Asset</th>
            <th className="num">Quantity (raw)</th>
            <th>Reason</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>
              <td className="mono small">{r.asset_id}</td>
              <td className="num">{r.quantity_raw}</td>
              <td className="small">{r.reason ?? ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

