"""Build the v3 run report from a finished (or failed) Session.

Everything is derived from the simulator's ledger, orders, equity grid and trace.
Incomplete valuations are reported as null secondary portfolio values; the primary
return counts settled cash regardless of whether unsold tokens can be valued. Nothing here produces a 0-100 score, percentile or recommendation.
"""

from __future__ import annotations

from collections import Counter
from fractions import Fraction
from typing import Any

from ..broker.ledger import AGENT_AVAILABLE, AGENT_RESERVED
from ..domain.quantities import fraction_to_decimal_str
from ..domain.status import OrderState
from ..engine.session import Session
from .debrief import attribution
from .validity import execution_validity

ENGINE_VERSION = "market_replay_engine_v2"
REPORT_VERSION = "run_report_v3"


def _dec(fr: Fraction | None, places: int = 8) -> str | None:
    return None if fr is None else fraction_to_decimal_str(fr, places)


def drawdown_from_points(points: list[tuple[int, int | None]]) -> tuple[Fraction | None, list[dict[str, int]]]:
    """Max drawdown over (time, equity) points; None equity = gap. Returns (max_dd, gaps)."""
    peak: int | None = None
    max_dd = Fraction(0)
    gaps: list[dict[str, int]] = []
    seen_any = False
    prev_t: int | None = None
    for t, eq in points:
        if eq is None:
            gaps.append({"time_ms": t, "prev_time_ms": prev_t if prev_t is not None else t})
            prev_t = t
            continue
        seen_any = True
        if peak is None or eq > peak:
            peak = eq
        if peak and peak > 0:
            dd = Fraction(peak - eq, peak)
            if dd > max_dd:
                max_dd = dd
        prev_t = t
    if not seen_any:
        return None, gaps
    return max_dd, gaps


def build_report(session: Session, *, run_meta: dict[str, Any], role: str = "admin") -> dict[str, Any]:
    sim = session.sim
    pack = session.pack
    m = pack.manifest
    alias = session.alias
    numeraire = pack.numeraire
    dec = m.numeraire_decimals

    initial = sim.bankroll_raw
    term = sim.value_portfolio()
    terminal_equity = term.equity
    # Count settled cash only. Reserved cash still belongs to the agent; pending
    # sale proceeds do not count until confirmed. Unsold tokens earn no primary credit.
    final_cash = sim.ledger.balance(AGENT_AVAILABLE, numeraire) + sim.ledger.balance(AGENT_RESERVED, numeraire)
    cash_return = Fraction(final_cash - initial, initial) if initial > 0 else None
    net_return = None
    if terminal_equity is not None and initial > 0:
        net_return = Fraction(terminal_equity - initial, initial)

    # Equity grid (fixed grid + ledger points), sorted; gaps are None equity.
    pts = sorted(((p.time_ms, p.equity) for p in sim.equity_points), key=lambda x: x[0])
    max_dd, dd_gaps = drawdown_from_points(pts)
    complete_points = sum(1 for _, e in pts if e is not None)

    orders = list(sim.orders.values())
    states = Counter(str(o.state) for o in orders)
    confirmed = [o for o in orders if o.state == OrderState.CONFIRMED]
    filled = [o for o in orders if o.state in (OrderState.CONFIRMED, OrderState.FILLED_PENDING_CONFIRMATION)]
    gas_total = sum(o.gas_charged for o in orders)
    turnover_numeraire = 0
    implicit_fee_numeraire = 0
    implicit_fee_other: dict[str, int] = Counter()
    for o in filled:
        pool = pack.pools[o.pool]
        fee_frac = Fraction(pool.fee_denominator - pool.fee_numerator, pool.fee_denominator)
        if o.asset_in == numeraire:
            turnover_numeraire += o.amount_in
            implicit_fee_numeraire += int(o.amount_in * fee_frac)
        else:
            turnover_numeraire += o.amount_out or 0
            implicit_fee_other[alias.asset(o.asset_in)] += int(o.amount_in * fee_frac)

    # Exposure: share of grid points with any non-numeraire inventory or unresolved orders
    exposed = sum(1 for p in sim.equity_points if p.source == "grid" and (p.priced != 0 or p.no_route_assets or p.unpriced_assets))
    grid_n = sum(1 for p in sim.equity_points if p.source == "grid")

    # Concentration at terminal state
    largest = None
    if terminal_equity and terminal_equity > 0:
        vals = [(h["asset"], h.get("net_value", h["value"])) for h in term.holdings if h["class"] == "priced_liquidatable"]
        cash = term.cash_available + term.cash_reserved + term.cash_pending
        candidates = [(alias.asset(numeraire), cash)] + [(alias.asset(a), v) for a, v in vals]
        a, v = max(candidates, key=lambda x: x[1])
        largest = {"asset_id": a, "share": _dec(Fraction(v, terminal_equity), 6)}

    tool_counts = Counter(r.tool for r in session.trace)
    error_counts = Counter(r.error_code for r in session.trace if r.error_code)
    quality_exposure = {"invalid_calls": session.budget.invalid_calls, "rate_limited": session.budget.rate_limited, "errors_by_code": dict(error_counts)}

    delivered_quality = [r.delivered["quality"] for r in session.trace if "quality" in r.delivered]
    quality_exposure["delivered_completeness"] = dict(Counter(q["completeness"] for q in delivered_quality))
    quality_exposure["stale_deliveries"] = sum(bool(q.get("stale")) for q in delivered_quality)
    quality_exposure["delivery_warnings"] = dict(Counter(w for q in delivered_quality for w in q.get("warnings", [])))
    unresolved = [o.to_public(alias) for o in sim.unresolved_orders()]
    no_route = [h for h in term.holdings if h["class"] == "no_route"]
    unpriced = [h for h in term.holdings if h["class"] == "unpriced_missing_data"]

    fidelity_intervals = [{"time_ms": f.time_ms, "pool_id": alias.pool(f.pool), "code": f.code} for f in sim.fidelity_flags]

    report: dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "run": {k: v for k, v in run_meta.items() if role == "admin" or k not in ("mask_seed", "engine_seed", "pack_id", "pack_path")},
        "status_dimensions": {
            "data_origin": str(m.origin),
            "availability_basis": str(session.basis),
            "execution_model": str(m.execution.model),
            "token_behavior": str(m.data.token_behavior_basis),
            "isolation": run_meta.get("isolation", "trusted_external_client"),
            "use_status": str(m.validation.qualification),
            "predictive_validity": "not_established",
        },
        "outcome": {
            "numeraire": alias.asset(numeraire),
            "numeraire_decimals": dec,
            "initial_equity_raw": str(initial),
            "terminal_model_equity_raw": None if terminal_equity is None else str(terminal_equity),
            "valuation_complete": term.complete,
            "primary_metric": "final_cash_return_v1",
            "final_cash_raw": str(final_cash),
            "headline_return": _dec(cash_return, 8),
            "liquidatable_portfolio_return": _dec(net_return, 8),
            "return_definition": "(final settled cash - initial equity) / initial equity; unsold tokens and unconfirmed proceeds excluded; modeled costs already in balances",
            "terminal_cash_raw": str(term.cash_available + term.cash_reserved + term.cash_pending),
            "terminal_priced_inventory_raw": str(term.priced_value),
            "terminal_liquidation_gas_raw": str(term.liquidation_gas),
            "valuation_policy": term.policy,
            "valuation_warnings": term.warnings,
        },
        "risk": {
            "max_drawdown": _dec(max_dd, 6) if complete_points > 1 else None,
            "drawdown_basis": f"fixed grid every {pack.params.reporting_grid_ms} ms plus ledger events; complete points only",
            "equity_points_total": len(pts),
            "equity_points_complete": complete_points,
            "gaps": dd_gaps[:100],
            "gap_count": len(dd_gaps),
            "exposure_share_of_grid": _dec(Fraction(exposed, grid_n), 4) if grid_n else None,
            "largest_position": largest,
        },
        "costs": {
            "gas_total_raw": str(gas_total),
            "gas_basis": pack.params.gas_basis,
            "implicit_pool_fee_numeraire_raw": str(implicit_fee_numeraire),
            "implicit_pool_fee_other_raw": {k: str(v) for k, v in implicit_fee_other.items()},
            "fee_note": "Pool fees are embedded in swap outputs and shown for information; they are not subtracted again.",
            "turnover_numeraire_raw": str(turnover_numeraire),
        },
        "activity": {
            "orders_total": len(orders),
            "orders_by_state": dict(states),
            "confirmed_fills": len(confirmed),
            "reverted": states.get("reverted", 0),
            "expired": states.get("expired", 0),
            "model_capacity_rejected": states.get("model_capacity_rejected", 0),
            "tool_calls": dict(tool_counts),
            "requests_total": session.budget.requests,
            "decisions_total": session.budget.decisions,
            "quality_exposure": quality_exposure,
            "budget_exhausted": session.budget.exhausted,
        },
        "unresolved": {
            "orders": unresolved,
            "no_route_inventory": [{"asset_id": alias.asset(h["asset"]), "quantity_raw": str(h["quantity"]), "reason": h["reason"]} for h in no_route],
            "unpriced_inventory": [{"asset_id": alias.asset(h["asset"]), "quantity_raw": str(h["quantity"]), "reason": h["reason"]} for h in unpriced],
            "environment_fidelity_flags": fidelity_intervals,
        },
        "coverage_and_assumptions": {
            "episode_duration_ms": m.period.duration_ms,
            "is_full_week": m.period.is_full_week,
            "universe": {
                "selection_rule_version": m.universe.selection_rule_version,
                "candidate_count": m.universe.candidate_count,
                "selected_count": m.universe.selected_count,
                "unsupported_count": m.universe.unsupported_count,
                "missing_count": m.universe.missing_count,
                "description": m.universe.description,
            },
            "latency_assumptions": pack.params.public_latency_assumptions() | {"computation_time_basis": session.resource_profile.public()["decision_latency_basis"]},
            "capacity_profile": pack.params.capacity.model_dump(),
            "availability_model": m.data.availability_model,
            "reconciliation_mismatches_in_run": len(sim.reconciliation_mismatches),
            "reserve_adjustments_in_run": dict(sim.reserve_adjustments),
            "limitations": [
                "Historical-flow-based simulation: external intents fixed, outputs counterfactual.",
                "Blinded interface, not contamination-proof.",
                "Isolation unenforced for trusted external clients." if run_meta.get("isolation", "trusted_external_client") == "trusted_external_client" else "Restricted local runner controls listed in run metadata.",
                "Generated fixture: no historical performance information." if str(m.origin) == "generated_fixture" else "Research pack: predictive validity not established.",
            ],
        },
        "versions": {
            "engine": ENGINE_VERSION,
            "report": REPORT_VERSION,
            "execution_parameters_hash": m.execution.parameters_hash,
            "execution_profile": pack.params.profile_name,
            "validator": m.validation.validator_version,
            "pack_id": m.pack_id if role == "admin" else "hidden",
        },
        "reproducibility": {
            "ledger_hash": sim.ledger.content_hash(),
            "state_hash": sim.state_hash(),
            "trace_hash": session.trace_hash(),
            "result_hash": session.result_hash(),
            "trace_length": len(session.trace),
        },
        "wall_clock": run_meta.get("wall_clock", {}),
        "inference": run_meta.get("inference", {"recorded": False}),
        "resource_profile": session.resource_profile.public(),
        "attribution": attribution(session),
        "statement": "This report describes behavior in modeled episodes after modeled costs. It does not establish an edge or future improvement.",
    }
    if role != "admin":
        report["versions"]["pack_id"] = "hidden"
    report["execution_validity"] = execution_validity(report)
    report["provisional"] = not report["execution_validity"]["eligible"]
    report["execution_evidence"] = {
        "flow_basis": "fixed_flow_with_stress_assumptions" if session.resource_profile.stress != "none" else "fixed_flow_simulation",
        "mechanics": {
            "quote_expiry": "modeled", "inclusion_slippage": "modeled", "failed_inclusion_gas": "modeled",
            "concentrated_liquidity": "adapter_with_tick_state", "approvals": "excluded",
            "transaction_replacement": "excluded", "transaction_cancellation": "excluded",
            "market_reaction": "excluded", "hook_callbacks": "excluded_last_observed_fee_only",
            "historical_transfer_restrictions": str(m.data.token_behavior_basis),
        },
        "calibration": "Independent historical wallet-fill and PnL calibration is not established. Passing reconciliation does not validate counterfactual fills or PnL.",
    }
    return report
