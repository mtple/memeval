"""Read-only report views. Building a view never issues an agent command."""
from __future__ import annotations

from typing import Any

from ..engine.session import Session


def observed_view(s: Session, pool_id: str | None, interval_ms: int) -> dict[str, Any]:
    discovered = s.sim.discovered_pools(s.now)
    pools = [{"pool_id": s.alias.pool(k)} for k in discovered]
    series: dict[str, Any] | None = None
    if pool_id or pools:
        pid = pool_id or pools[0]["pool_id"]
        r = s.alias.resolve(pid)
        if r and r[0] == "pool" and r[1] in discovered:
            from fractions import Fraction

            from ..domain.quantities import fraction_to_decimal_str
            from ..observations.store import aggregate_candles

            key = r[1]
            base, quote = s._base_quote(key)
            end = s.now
            start = max(end - interval_ms * 400, -(10**12))
            candles, gaps = aggregate_candles(s.sim.obs[key], base_asset=base, interval_ms=interval_ms, start_ms=start, end_ms=end, as_of=s.now, availability_delay_ms=s.params.availability_delay_ms, include_partial=False)
            scale = Fraction(10 ** s._decimals(base), 10 ** s._decimals(quote))
            items = []
            for c in candles:
                d = c.to_public()
                for f in ("open", "high", "low", "close"):
                    v = getattr(c, f)
                    d[f] = None if v is None else fraction_to_decimal_str(v * scale, 18)
                items.append(d)
            series = {"pool_id": pid, "interval_ms": interval_ms, "items": items, "gaps": gaps, "as_of_ms": s.now}
    equity = [{"time_ms": p.time_ms, "equity_raw": None if p.equity is None else str(p.equity), "complete": p.complete, "source": p.source} for p in s.sim.equity_points]
    orders = [o.to_public(s.alias) for o in list(s.sim.orders.values())[-50:]]
    return {"clock_ms": s.now, "pools": pools, "series": series, "equity": equity, "orders": orders, "note": "Only observations with available_ms <= clock are shown; no future data."}


def recorded_timeline(trace: list[dict], cursor: int, limit: int, completed_orders: list[dict] | None = None) -> dict:
    """Use original deliveries; never reconstruct observations just to display a timeline."""
    orders = {}
    for record in trace:
        data = ((record.get("delivered") or {}).get("payload") or {}).get("data")
        if record.get("status") != "ok" or not isinstance(data, dict):
            continue
        candidates = [data.get("order")]
        if isinstance(data.get("orders"), list):
            candidates.extend(data["orders"])
        for order in candidates:
            if isinstance(order, dict) and "idempotency_key" in order and "order_id" in order:
                orders[order["idempotency_key"]] = order
    for order in completed_orders or []:
        orders[order["idempotency_key"]] = order
    items = []
    for record in trace[cursor:cursor + limit]:
        args = record.get("arguments") or {}
        delivered = record.get("delivered") or {
            "evidence_basis": "not_recorded", "payload": None, "payload_omitted": False,
        }
        items.append({
            "index": record["index"], "tool": record["tool"],
            "started_ms": record["clock_before_ms"], "delivered_ms": record["clock_after_ms"],
            "status": record["status"], "error_code": record.get("error_code"),
            "decision_elapsed_ms": record.get("decision_elapsed_ms", 0),
            "request": args, "delivered": delivered,
            "order": orders.get(args.get("idempotency_key"))
            if record["tool"] == "broker.submit" and record["status"] == "ok" else None,
        })
    return {
        "items": items, "next_cursor": cursor + limit if cursor + limit < len(trace) else None,
        "total": len(trace),
        "note": "Original recorded deliveries; large payloads retain a digest and omission flag. Older observations that were not recorded are unavailable, not reconstructed. Order summaries use the completed snapshot when available, otherwise the latest recorded delivery. Intent is optional agent-written metadata, not inferred reasoning.",
    }
