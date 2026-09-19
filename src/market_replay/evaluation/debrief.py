"""Recorded observations and exact FIFO attribution; no inferred agent intentions."""

from collections import defaultdict
from fractions import Fraction

from ..domain.quantities import fraction_to_decimal_str as decimal
from ..domain.status import OrderState


def attribution(session) -> dict:
    cash = session.pack.numeraire
    lots = defaultdict(list)
    spent = defaultdict(int)
    sales = []
    for order in sorted(session.sim.orders.values(), key=lambda o: (o.confirm_time_ms if o.confirm_time_ms is not None else 10**30, o.order_id)):
        if order.state != OrderState.CONFIRMED:
            continue
        if order.asset_in == cash and order.amount_out:
            lots[order.asset_out].append([order.amount_out, Fraction(order.amount_in + order.gas_charged)])
            spent[order.asset_out] += order.amount_in
        elif order.asset_out == cash:
            left, basis = order.amount_in, Fraction(0)
            for lot in lots[order.asset_in]:
                take = min(left, lot[0])
                if take:
                    cost = lot[1] * Fraction(take, lot[0])
                    lot[0] -= take
                    lot[1] -= cost
                    basis += cost
                    left -= take
            contribution = (
                Fraction((order.amount_out or 0) - order.gas_charged) - basis if left == 0 else None
            )
            sales.append(
                {
                    "order_id": order.order_id,
                    "asset_id": session.alias.asset(order.asset_in),
                    "contribution_raw": None if contribution is None else decimal(contribution, 18),
                }
            )
    positive = [r for r in sales if r["contribution_raw"] is not None and Fraction(r["contribution_raw"]) > 0]
    best = max(positive, key=lambda r: Fraction(r["contribution_raw"]), default=None)
    total = sum(spent.values())
    gains = sum((Fraction(r["contribution_raw"]) for r in positive), Fraction(0))
    return {
        "method": "confirmed_sale_fifo_v1",
        "sales": sales,
        "best_trade": best,
        "best_trade_share_of_positive_realized_contributions": decimal(
            Fraction(best["contribution_raw"]) / gains, 8
        )
        if best
        else None,
        "largest_asset_share_of_buy_notional": decimal(Fraction(max(spent.values()), total), 8)
        if total
        else None,
        "cash_reference_return": "0",
        "note": "Each confirmed sale is one trade. FIFO basis includes proportionate buy gas and sale gas. Failed-order gas and unsold inventory are excluded from realized contributions but remain in final cash. This attribution is not a counterfactual strategy return.",
    }


def timeline(session, cursor=0, limit=100) -> dict:
    rows = session.trace[cursor : cursor + limit]
    orders = {o.idempotency_key: o for o in session.sim.orders.values()}
    items = []
    for trace in rows:
        order = (
            orders.get(trace.arguments.get("idempotency_key"))
            if trace.tool == "broker.submit" and trace.status == "ok"
            else None
        )
        items.append(
            {
                "index": trace.index,
                "tool": trace.tool,
                "started_ms": trace.clock_before_ms,
                "delivered_ms": trace.clock_after_ms,
                "status": trace.status,
                "error_code": trace.error_code,
                "decision_elapsed_ms": trace.decision_elapsed_ms,
                "request": trace.arguments,
                "delivered": trace.delivered,
                "order": order.to_public(session.alias) if order else None,
            }
        )
    return {
        "items": items,
        "next_cursor": cursor + limit if cursor + limit < len(session.trace) else None,
        "total": len(session.trace),
        "note": "Delivery records describe what the client received. Large payloads retain a digest and explicit omission. Intent is optional agent-written metadata recorded before submission; no reasoning is inferred or scored.",
    }
