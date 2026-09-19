import { DecisionTimeline } from "../DecisionTimeline";
import { TradeReview } from "../TradeReview";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { marketLines, marketPct } from "../MarketCard";
import { get, post, type Pack, type Report, type ReplayResult, type Run } from "../api";
import { DIMENSION_ORDER, dimensionLabel, explainDimension, summarySentence } from "../explain";
import { fmtAmount, fmtDuration, fmtMs, fmtPct, fmtRaw, fmtReturn, humanize, shortHash, unitLabel } from "../format";
import { useRole } from "../role";
import { Badge, Card, ErrorState, JsonView, KV, Loading, StrList, downloadJson, useLoad } from "../ui";

export default function Results() {
  const { id = "" } = useParams();
  const { role } = useRole();
  const rep = useLoad(() => get<Report>(`/runs/${id}/report?role=${role === "admin" ? "admin" : "participant"}`), [id, role]);
  const run = useLoad(() => get<Run>(`/runs/${id}`), [id]);
  const packId = run.data?.pack_id ?? "";
  const pack = useLoad(() => (packId ? get<Pack>(`/packs/${packId}`) : Promise.resolve(null)), [packId]);
  const [replay, setReplay] = useState<ReplayResult | null>(null);
  const [replayErr, setReplayErr] = useState<unknown>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [exportErr, setExportErr] = useState<unknown>(null);

  const R = rep.data;
  const dec = R?.outcome.numeraire_decimals;
  const unit = unitLabel(R?.outcome.numeraire);
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
          {(() => {
            const ranked = R.outcome.primary_metric === "final_cash_return_v1" && R.outcome.headline_return !== null && !R.provisional;
            const ret = R.outcome.headline_return === null ? null : Number(R.outcome.headline_return);
            const tone = ret === null ? "na" : ret > 0 ? "up" : ret < 0 ? "down" : "";
            const L = marketLines(pack.data?.market_baseline);
            const cmp = (v: string | null) => (v === null ? "not read" : marketPct(v));
            return (
              <section className="card hero-result">
                <div className="hero-main">
                  <span className="lbl">{R.outcome.primary_metric === "final_cash_return_v1" ? "Final ETH return after gas and fees" : "Legacy portfolio return, not ranked"}</span>
                  <span className={`hero-number ${tone}`}>{ret === null ? "not stated" : fmtReturn(R.outcome.headline_return)}</span>
                  <span className="hero-sub">
                    Started with {fmtRaw(R.outcome.initial_equity_raw, dec)} {unit}, ended with {fmtRaw(R.outcome.final_cash_raw ?? null, dec)} {unit} in settled {unit}.
                    {R.provisional ? " Provisional: excluded from ranking under the execution eligibility rule." : ranked ? " This is the number that ranks on the leaderboard." : R.outcome.primary_metric !== "final_cash_return_v1" ? " Scored under the old rule, so it does not rank; play the day again for a ranked result." : " Not ranked."}
                  </span>
                </div>
                <div className="hero-side">
                  <span className="lbl">The market that day, in dollars</span>
                  <div className="hero-lines">
                    <div>
                      <span className="k">Base ecosystem</span>
                      <span className="v">{cmp(L.baseUsd)}</span>
                    </div>
                    <div>
                      <span className="k">ETH</span>
                      <span className="v">{cmp(L.ethUsd)}</span>
                    </div>
                    <div>
                      <span className="k">Crypto market</span>
                      <span className="v">{cmp(L.crypto)}</span>
                    </div>
                  </div>
                  <span className="lbl">Results are in ETH, so holding ETH is 0%. Base ecosystem {L.vsEth === null ? "not read" : `${marketPct(L.vsEth)} against ETH`}.</span>
                </div>
              </section>
            );
          })()}
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
            <div className="metrics secondary" style={{ marginTop: 12 }}>
              <M label="Orders filled" value={`${R.activity.confirmed_fills} of ${R.activity.orders_total}`} sub={R.activity.reverted || R.activity.expired ? `${R.activity.reverted} reverted, ${R.activity.expired} expired` : undefined} />
              <M label="Gas paid" value={fmtRaw(R.costs.gas_total_raw, dec)} sub={unit} />
              <M label="Worst drop from a peak" value={R.risk.max_drawdown === null ? "not supportable" : fmtPct(R.risk.max_drawdown)} warn={R.risk.max_drawdown === null} />
              <M label="Unsold holdings, if sold through the model" value={R.outcome.terminal_model_equity_raw === null ? "could not be valued" : fmtRaw(R.outcome.terminal_model_equity_raw, dec)} sub={R.outcome.terminal_model_equity_raw === null ? "does not affect the ranked number" : `${unit} total; not part of the ranked number`} warn={R.outcome.terminal_model_equity_raw === null} />
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

          <TradeReview key={id} runId={id} />
          <DecisionTimeline key={`timeline-${id}`} runId={id} />
          <p className="small muted" style={{ maxWidth: "80ch" }}>
            This is a replay of a recorded day, not live trading: other traders' actions are fixed, fills come from a pool model, and nothing here predicts live results. <Link to="/about">How results work</Link>.
          </p>
          <details className="more">
            <summary>All the details (risk, costs, activity, unresolved items, assumptions, versions, reproducibility, exports)</summary>
            <div className="stack" style={{ marginTop: 8 }}>
          <Card title="Trust and validity">
            <ul className="plain" style={{ paddingLeft: 18 }}>
              {DIMENSION_ORDER.map((k) => (
                <li key={k}>
                  <strong>{dimensionLabel(k)}.</strong> {explainDimension(k, R.status_dimensions?.[k])}
                </li>
              ))}
            </ul>
            <h3>Run validity</h3>
            {R.execution_validity ? <>
              <p className={R.provisional ? "notice warn" : "muted"}>{R.provisional ? "Provisional outcome. This run is excluded from ranking under the execution eligibility rule." : "This run passes the execution eligibility gates for its declared model and resource profile."}</p>
              <KV rows={R.execution_validity.gates.map(g => [humanize(g.gate), g.passed ? "Pass" : "Excluded"])} />
              <p className="small muted">Rule: {R.execution_validity.rule_version}. {R.execution_validity.capacity_policy} Rejections: {R.execution_validity.capacity_rejections}.</p>
            </> : <p className="notice">This report predates execution eligibility gates. A fresh run is needed for the current protocol.</p>}
            <KV rows={[["Isolation", humanize(R.status_dimensions.isolation)], ["Token behavior", humanize(R.status_dimensions.token_behavior)], ["Data completeness at delivery", Object.entries(R.activity.quality_exposure?.delivered_completeness ?? {}).map(([k, v]) => `${humanize(k)}: ${v}`).join(", ") || "Not recorded"], ["Stale deliveries", R.activity.quality_exposure?.stale_deliveries ?? "Not recorded"], ["Prior attempts", Math.max(0, Number(R.run.attempt_number ?? 1) - 1)], ["Predictive validity", "Not established"]]} />
            {R.resource_profile && <KV rows={[["Resource profile", humanize(R.resource_profile.profile_id)], ["Computation treatment", R.resource_profile.decision_latency_basis], ["Assumed decision time", `${R.resource_profile.decision_latency_ms} ms`], ["Execution stress", R.resource_profile.stress_basis]]} />}
            <p className="statement">{R.statement}</p>
          </Card>
          {R.attribution && <Card title="Concentration and trade dependence"><KV rows={[["Largest asset share of buy notional", fmtPct(R.attribution.largest_asset_share_of_buy_notional)], ["Best sale's share of positive realized contributions", fmtPct(R.attribution.best_trade_share_of_positive_realized_contributions)], ["Cash reference return", fmtReturn(R.attribution.cash_reference_return)]]} /><p className="muted small">{R.attribution.note}</p></Card>}
          {R.execution_evidence && <Card title="Execution evidence"><KV rows={Object.entries(R.execution_evidence.mechanics).map(([key, value]) => [humanize(key), humanize(value)])} /><p className="muted small">{humanize(R.execution_evidence.flow_basis)}. {R.execution_evidence.calibration}</p></Card>}
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
                  ["Implicit pool fee paid in tokens", typeof R.costs.implicit_pool_fee_other_raw === "string" ? fmtRaw(R.costs.implicit_pool_fee_other_raw, dec) : "several tokens"],
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
                      ["Gas per fill", `${amt(R.coverage_and_assumptions.latency_assumptions.gas_cost_raw)} (${humanize(R.coverage_and_assumptions.latency_assumptions.gas_basis)})`],
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

          {role === "admin" && <div className="grid-2">
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
          </div>}

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


