"""Canonical private identity and run-scoped public aliases.

Private identity:
    asset: ``(chain_id, contract_address)``  -> key ``"{chain_id}:{address}"``
    pool:  ``(chain_id, protocol, pool_address)`` -> key ``"{chain_id}:{protocol}:{address}"``

Symbols are metadata and are never used as identity. Public identity is a random,
stable alias scoped to a run (mask seed). Aliases are derived with a keyed hash so
the same canonical object always gets the same alias within a run and different
runs (different seeds) get unrelated aliases. Alias assignment does not depend on
anything that happens later in the episode.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_ALIAS_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no confusable chars


def normalize_address(address: str) -> str:
    a = address.strip()
    if _ADDR_RE.match(a):
        return a.lower()
    # Fixture identifiers may be arbitrary non-EVM strings; keep them but forbid whitespace.
    if not a or any(c.isspace() for c in a):
        raise ValueError(f"invalid address: {address!r}")
    return a


@dataclass(frozen=True, slots=True, order=True)
class AssetId:
    chain_id: int
    address: str

    @property
    def key(self) -> str:
        return f"{self.chain_id}:{self.address}"

    @classmethod
    def parse(cls, key: str) -> AssetId:
        chain, addr = key.split(":", 1)
        return cls(int(chain), normalize_address(addr))

    def __post_init__(self) -> None:
        object.__setattr__(self, "address", normalize_address(self.address))


@dataclass(frozen=True, slots=True, order=True)
class PoolId:
    chain_id: int
    protocol: str
    address: str

    @property
    def key(self) -> str:
        return f"{self.chain_id}:{self.protocol}:{self.address}"

    @classmethod
    def parse(cls, key: str) -> PoolId:
        chain, proto, addr = key.split(":", 2)
        return cls(int(chain), proto, normalize_address(addr))

    def __post_init__(self) -> None:
        object.__setattr__(self, "address", normalize_address(self.address))
        if not self.protocol or ":" in self.protocol:
            raise ValueError("protocol must be a non-empty string without ':'")


def _b32(digest: bytes, length: int) -> str:
    out = []
    n = int.from_bytes(digest, "big")
    for _ in range(length):
        out.append(_ALIAS_ALPHABET[n % len(_ALIAS_ALPHABET)])
        n //= len(_ALIAS_ALPHABET)
    return "".join(out)


class AliasMap:
    """Run-scoped, keyed, stable alias mapping with reverse lookup.

    ``numeraire_alias`` maps the settlement asset to a declared public name such
    as ``CASH`` or ``NATIVE``; every other object receives a random alias.
    """

    KINDS = ("asset", "pool", "wallet", "tx")

    def __init__(self, mask_seed: str, numeraire_key: str | None = None, numeraire_alias: str = "CASH") -> None:
        if not mask_seed:
            raise ValueError("mask_seed required")
        self._key = hashlib.blake2b(mask_seed.encode("utf-8"), digest_size=32).digest()
        self._forward: dict[tuple[str, str], str] = {}
        self._reverse: dict[str, tuple[str, str]] = {}
        self.numeraire_key = numeraire_key
        self.numeraire_alias = numeraire_alias
        if numeraire_key is not None:
            self._forward[("asset", numeraire_key)] = numeraire_alias
            self._reverse[numeraire_alias] = ("asset", numeraire_key)

    def _derive(self, kind: str, canonical: str) -> str:
        digest = hashlib.blake2b(f"{kind}|{canonical}".encode(), key=self._key, digest_size=16).digest()
        return f"{kind}_{_b32(digest, 8)}"

    def alias(self, kind: str, canonical: str) -> str:
        if kind not in self.KINDS:
            raise ValueError(f"unknown alias kind {kind}")
        k = (kind, canonical)
        existing = self._forward.get(k)
        if existing is not None:
            return existing
        a = self._derive(kind, canonical)
        # Extremely unlikely collision handling: lengthen deterministically.
        n = 0
        while a in self._reverse and self._reverse[a] != k:
            n += 1
            a = self._derive(kind, f"{canonical}#{n}")
        self._forward[k] = a
        self._reverse[a] = k
        return a

    def asset(self, canonical_key: str) -> str:
        return self.alias("asset", canonical_key)

    def pool(self, canonical_key: str) -> str:
        return self.alias("pool", canonical_key)

    def wallet(self, canonical: str) -> str:
        return self.alias("wallet", canonical)

    def tx(self, canonical: str) -> str:
        return self.alias("tx", canonical)

    def resolve(self, alias: str) -> tuple[str, str] | None:
        """Reverse lookup. Only aliases already issued by this map resolve; canonical keys never do."""
        return self._reverse.get(alias)

    def known_aliases(self) -> dict[str, tuple[str, str]]:
        return dict(self._reverse)


def looks_like_canonical(identifier: str) -> bool:
    """True if a supplied identifier resembles a private canonical key or raw address (escape-hatch guard)."""
    s = identifier.strip()
    if _ADDR_RE.match(s):
        return True
    if re.match(r"^\d+:", s):
        return True
    if re.match(r"^0x[0-9a-fA-F]{64}$", s):
        return True
    return False
