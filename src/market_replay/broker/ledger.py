"""Append-only, balanced event ledger.

Accounts are strings such as ``agent.available``, ``agent.reserved``, ``agent.pending``,
``pool.<key>`` and ``sink.gas``. Every entry carries legs ``(account, asset, delta)`` and
the deltas per asset must sum to zero, so tokens are conserved between participant,
pools and cost sinks. Every entry is linked to a causative event.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field

AGENT_AVAILABLE = "agent.available"
AGENT_RESERVED = "agent.reserved"
AGENT_PENDING = "agent.pending"
SINK_GAS = "sink.gas"
SOURCE_BANKROLL = "source.bankroll"


class LedgerError(RuntimeError):
    pass


@dataclass(slots=True, frozen=True)
class Leg:
    account: str
    asset: str
    delta: int


@dataclass(slots=True, frozen=True)
class LedgerEntry:
    index: int
    time_ms: int
    cause_kind: str  # bankroll | reserve | release | fill | confirm | revert_gas | expire
    cause_ref: str  # order id or "init"
    legs: tuple[Leg, ...]
    note: str = ""

    def to_public(self) -> dict:
        return {
            "index": self.index,
            "time_ms": self.time_ms,
            "cause_kind": self.cause_kind,
            "cause_ref": self.cause_ref,
            "legs": [{"account": l.account, "asset": l.asset, "delta": str(l.delta)} for l in self.legs],
            "note": self.note,
        }


class Ledger:
    def __init__(self) -> None:
        self.entries: list[LedgerEntry] = []
        self._balances: dict[tuple[str, str], int] = defaultdict(int)

    def balance(self, account: str, asset: str) -> int:
        return self._balances.get((account, asset), 0)

    def balances_of(self, account: str) -> dict[str, int]:
        return {a: v for (acc, a), v in self._balances.items() if acc == account and v != 0}

    def post(self, time_ms: int, cause_kind: str, cause_ref: str, legs: list[Leg], note: str = "") -> LedgerEntry:
        sums: dict[str, int] = defaultdict(int)
        for leg in legs:
            sums[leg.asset] += leg.delta
        for asset, s in sums.items():
            if s != 0:
                raise LedgerError(f"unbalanced entry for {asset}: {s}")
        # Agent accounts can never go negative.
        for leg in legs:
            if leg.account.startswith("agent.") and self._balances[(leg.account, leg.asset)] + leg.delta < 0:
                # apply nothing
                raise LedgerError(f"insufficient balance in {leg.account} for {leg.asset}")
        for leg in legs:
            self._balances[(leg.account, leg.asset)] += leg.delta
        entry = LedgerEntry(
            index=len(self.entries), time_ms=time_ms, cause_kind=cause_kind, cause_ref=cause_ref, legs=tuple(legs), note=note
        )
        self.entries.append(entry)
        return entry

    def content_hash(self) -> str:
        h = hashlib.sha256()
        for e in self.entries:
            h.update(json.dumps(e.to_public(), sort_keys=True, separators=(",", ":")).encode())
        return h.hexdigest()

    def totals_by_cause(self, cause_kind: str, account: str, asset: str) -> int:
        total = 0
        for e in self.entries:
            if e.cause_kind == cause_kind:
                for leg in e.legs:
                    if leg.account == account and leg.asset == asset:
                        total += leg.delta
        return total
