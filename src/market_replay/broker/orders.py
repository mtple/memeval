"""Order and quote records with the explicit v1 lifecycle."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from ..domain.status import OrderState


def payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(slots=True)
class Quote:
    quote_id: str
    created_ms: int
    expires_ms: int
    pool: str
    asset_in: str
    asset_out: str
    amount_in: int
    amount_out: int
    reserve_in: int
    reserve_out: int
    fee_num: int
    fee_den: int
    gas_cost: int
    state_version: int
    capacity_ok: bool
    capacity_reason: str | None


@dataclass(slots=True)
class Order:
    order_id: str
    idempotency_key: str
    payload_hash: str
    pool: str
    asset_in: str
    asset_out: str
    amount_in: int
    min_amount_out: int
    deadline_ms: int
    submitted_ms: int
    ready_ms: int
    inclusion_block: int | None
    inclusion_time_ms: int | None
    reserved_gas: int
    state: OrderState = OrderState.RECEIVED
    quote_id: str | None = None
    amount_out: int | None = None
    fill_block: int | None = None
    fill_time_ms: int | None = None
    confirm_block: int | None = None
    confirm_time_ms: int | None = None
    gas_charged: int = 0
    reason: str | None = None
    history: list[tuple[int, str]] = field(default_factory=list)
    intent: dict[str, str] = field(default_factory=dict)

    def transition(self, time_ms: int, new_state: OrderState, reason: str | None = None) -> None:
        self.state = new_state
        if reason:
            self.reason = reason
        self.history.append((time_ms, str(new_state)))

    def to_public(self, alias) -> dict[str, Any]:
        """Alias-mapped view. ``alias`` is an AliasMap."""
        return {
            "order_id": self.order_id,
            "idempotency_key": self.idempotency_key,
            "pool_id": alias.pool(self.pool),
            "asset_in": alias.asset(self.asset_in),
            "asset_out": alias.asset(self.asset_out),
            "amount_in_raw": str(self.amount_in),
            "min_amount_out_raw": str(self.min_amount_out),
            "deadline_ms": self.deadline_ms,
            "submitted_ms": self.submitted_ms,
            "ready_ms": self.ready_ms,
            "inclusion_time_ms": self.inclusion_time_ms,
            "intent": self.intent,
            "state": str(self.state),
            "amount_out_raw": None if self.amount_out is None else str(self.amount_out),
            "fill_time_ms": self.fill_time_ms,
            "confirm_time_ms": self.confirm_time_ms,
            "gas_charged_raw": str(self.gas_charged),
            "reason": self.reason,
            "history": [{"time_ms": t, "state": s} for t, s in self.history],
        }
