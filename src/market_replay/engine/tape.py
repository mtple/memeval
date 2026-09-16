"""Compact in-memory tape with times converted to relative episode milliseconds."""

from __future__ import annotations

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
                kind=str(d["kind"]),
                pool=str(d["pool"]),
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
                availability_basis=str(d.get("availability_basis") or "unknown"),
            )
        )
    # Deterministic order: block, log_index, seq. Time must be monotone with block.
    out.sort(key=lambda r: (r.block, r.log_index, r.seq))
    for i in range(1, len(out)):
        if out[i].time_ms < out[i - 1].time_ms:
            raise ValueError(f"tape time not monotone at seq {out[i].seq}")
    return out
