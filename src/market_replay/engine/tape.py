"""Compact in-memory tape with times converted to relative episode milliseconds."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from fractions import Fraction

from ..domain.models import TapeEvent


@dataclass(slots=True)
class RelEvent:
    seq: int
    block: int
    log_index: int
    time_ms: int  # relative to episode start
    kind: str
    pool: str
    tx: str | None
    wallet: str | None
    asset_in: str | None
    amount_in: int | None
    amount_out_recorded: int | None
    amount0: int | None
    amount1: int | None
    reserve0: int | None
    reserve1: int | None
    payload: dict
    available_ms: int | None  # relative; None -> never published
    availability_basis: str
    # concentrated liquidity (cl_init / cl_modify / cl_swap); None when the row has no such field.
    # cl_swap amount0/amount1 are pool deltas (positive = paid into the pool).
    sqrt_price_x96: int | None = None
    tick: int | None = None
    tick_lower: int | None = None
    tick_upper: int | None = None
    liquidity_delta: int | None = None
    sqrt_price_x96_after: int | None = None
    liquidity_after: int | None = None
    tick_after: int | None = None
    fee_pips: int | None = None
    # filled during reference replay: recorded_out / max_out on reference state
    output_ratio: Fraction | None = None


def _opt_int(v) -> int | None:
    return None if v is None else int(v)


def to_relative(events: list[TapeEvent] | list[dict], start_utc_ms: int) -> list[RelEvent]:
    """Convert validated TapeEvent models or raw row dicts (fast path for large tapes)."""
    out: list[RelEvent] = []
    for e in events:
        d = e if isinstance(e, dict) else e.model_dump()
        avail = d.get("available_utc_ms")
        out.append(
            RelEvent(
                seq=int(d["seq"]),
                block=int(d["block"]),
                log_index=int(d["log_index"]),
                time_ms=int(d["time_utc_ms"]) - start_utc_ms,
                kind=sys.intern(str(d["kind"])),
                pool=sys.intern(str(d["pool"])),
                tx=d.get("tx"),
                wallet=d.get("wallet"),
                asset_in=d.get("asset_in"),
                amount_in=_opt_int(d.get("amount_in")),
                amount_out_recorded=_opt_int(d.get("amount_out_recorded")),
                amount0=_opt_int(d.get("amount0")),
                amount1=_opt_int(d.get("amount1")),
                reserve0=_opt_int(d.get("reserve0")),
                reserve1=_opt_int(d.get("reserve1")),
                payload=dict(d.get("payload") or {}),
                available_ms=(int(avail) - start_utc_ms) if avail is not None else None,
                availability_basis=sys.intern(str(d.get("availability_basis") or "unknown")),
                sqrt_price_x96=_opt_int(d.get("sqrt_price_x96")),
                tick=_opt_int(d.get("tick")),
                tick_lower=_opt_int(d.get("tick_lower")),
                tick_upper=_opt_int(d.get("tick_upper")),
                liquidity_delta=_opt_int(d.get("liquidity_delta")),
                sqrt_price_x96_after=_opt_int(d.get("sqrt_price_x96_after")),
                liquidity_after=_opt_int(d.get("liquidity_after")),
                tick_after=_opt_int(d.get("tick_after")),
                fee_pips=_opt_int(d.get("fee_pips")),
            )
        )
    # Deterministic order: block, log_index, seq. Time must be monotone with block.
    out.sort(key=lambda r: (r.block, r.log_index, r.seq))
    for i in range(1, len(out)):
        if out[i].time_ms < out[i - 1].time_ms:
            raise ValueError(f"tape time not monotone at seq {out[i].seq}")
    return out
