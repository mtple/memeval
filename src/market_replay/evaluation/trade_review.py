"""Read-only explanations of recorded orders; never submits commands or advances time."""
from __future__ import annotations

from fractions import Fraction

from ..broker.ledger import AGENT_AVAILABLE, AGENT_PENDING, AGENT_RESERVED
from ..domain.quantities import fraction_to_decimal_str
from ..domain.status import OrderState
from ..engine.session import Session


def build_trade_review(session: Session) -> dict:
    sim, alias = session.sim, session.alias
    cash = session.pack.numeraire
    cash_decimals = session.pack.manifest.numeraire_decimals
    tokens: dict[str, dict] = {}
    events = []
    for order in sorted(sim.orders.values(), key=lambda o: o.fill_time_ms if o.fill_time_ms is not None else o.submitted_ms):
        buy = order.asset_in == cash
        sell = order.asset_out == cash
        if not (buy or sell):
            continue
        asset = order.asset_out if buy else order.asset_in
        decimals = session._decimals(asset)
        item = tokens.setdefault(asset, {"asset_id": alias.asset(asset), "decimals": decimals, "spent": 0, "recovered": 0, "gas": 0, "buys": 0, "sells": 0, "first_buy_ms": None, "last_sell_ms": None, "lots": [], "held_ms": 0, "sold_quantity": 0})
        item["gas"] += order.gas_charged
        confirmed = order.state == OrderState.CONFIRMED
        quantity = (order.amount_out or 0) if buy else order.amount_in
        cash_amount = order.amount_in if buy else (order.amount_out or 0)
        t = order.fill_time_ms
        if confirmed:
            if buy:
                item["spent"] += cash_amount
                item["buys"] += 1
                if item["first_buy_ms"] is None:
                    item["first_buy_ms"] = t
                item["lots"].append([quantity, t])
            else:
                item["recovered"] += cash_amount
                item["sells"] += 1
                item["last_sell_ms"] = t
                remaining = quantity
                for lot in item["lots"]:
                    matched = min(remaining, lot[0])
                    item["held_ms"] += matched * max(0, t - lot[1])
                    item["sold_quantity"] += matched
                    lot[0] -= matched
                    remaining -= matched
                    if not remaining:
                        break
        price = None
        if t is not None and quantity > 0:
            price = fraction_to_decimal_str(Fraction(cash_amount * 10**decimals, quantity * 10**cash_decimals), 18)
        events.append({"order_id": order.order_id, "pool_id": alias.pool(order.pool), "asset_id": alias.asset(asset), "side": "buy" if buy else "sell", "state": str(order.state), "submitted_ms": order.submitted_ms, "fill_time_ms": t, "confirm_time_ms": order.confirm_time_ms, "quantity_raw": str(quantity) if t is not None else None, "cash_raw": str(cash_amount) if t is not None else None, "gas_raw": str(order.gas_charged), "price": price, "reason": order.reason})
    rows = []
    for asset, item in tokens.items():
        balances = [sim.ledger.balance(a, asset) for a in (AGENT_AVAILABLE, AGENT_RESERVED, AGENT_PENDING)]
        rows.append({"asset_id": item["asset_id"], "decimals": item["decimals"], "eth_spent_raw": str(item["spent"]), "eth_recovered_raw": str(item["recovered"]), "gas_raw": str(item["gas"]), "net_cash_raw": str(item["recovered"] - item["spent"] - item["gas"]), "remaining_raw": str(sum(balances)), "pending_raw": str(balances[2]), "buys": item["buys"], "sells": item["sells"], "first_buy_ms": item["first_buy_ms"], "last_sell_ms": item["last_sell_ms"], "average_hold_ms": item["held_ms"] // item["sold_quantity"] if item["sold_quantity"] else None})
    rows.sort(key=lambda r: int(r["net_cash_raw"]), reverse=True)
    return {"clock_ms": session.now, "numeraire": alias.asset(cash), "numeraire_decimals": cash_decimals, "tokens": rows, "events": events, "rejected_calls": [{"time_ms": t.clock_after_ms, "error_code": t.error_code} for t in session.trace if t.tool == "broker.submit" and t.status == "error"], "note": "Cash contributions include confirmed trades and charged gas. Unsold tokens are not profit. Holding time uses quantity-weighted FIFO matching of confirmed sales. Charts show the observed simulated pool price; markers show actual simulated fill prices. This records actions, not the agent's reasoning."}
