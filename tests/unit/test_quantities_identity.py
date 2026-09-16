"""Acceptance 8, 9: identity by contract, exact large-integer round trips."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from market_replay.domain.identity import AliasMap, AssetId, PoolId, looks_like_canonical
from market_replay.domain.quantities import (
    QuantityError,
    decimal_to_raw,
    format_units,
    parse_raw,
    raw_str,
    raw_to_decimal,
)


def test_same_symbol_different_contract_distinct():
    a = AssetId(8453, "0x" + "1" * 40)
    b = AssetId(8453, "0x" + "2" * 40)
    assert a != b and a.key != b.key
    m = AliasMap("seed")
    assert m.asset(a.key) != m.asset(b.key)


def test_pool_identity_includes_protocol():
    p1 = PoolId(8453, "uniswap_v2", "0x" + "a" * 40)
    p2 = PoolId(8453, "other_v2", "0x" + "a" * 40)
    assert p1.key != p2.key


def test_alias_stable_and_reverse():
    m = AliasMap("seed", numeraire_key="0:cash", numeraire_alias="CASH")
    a1 = m.asset("0:tok_01")
    assert m.asset("0:tok_01") == a1
    assert m.resolve(a1) == ("asset", "0:tok_01")
    assert m.asset("0:cash") == "CASH"
    assert AliasMap("other").asset("0:tok_01") != a1
    assert m.resolve("0:tok_01") is None  # canonical keys never resolve


def test_alias_independent_of_order_of_requests():
    m1 = AliasMap("s")
    m2 = AliasMap("s")
    x1 = m1.asset("0:x")
    m2.asset("0:y")
    assert m2.asset("0:x") == x1


def test_looks_like_canonical():
    assert looks_like_canonical("0x" + "a" * 40)
    assert looks_like_canonical("8453:0x" + "a" * 40)
    assert not looks_like_canonical("pool_abc12345")


def test_large_integer_round_trip_python_json():
    big = 2**255 - 12345
    s = raw_str(big)
    assert parse_raw(s) == big
    assert json.loads(json.dumps({"v": s}))["v"] == s
    assert parse_raw(json.loads(json.dumps({"v": s}))["v"]) == big


def test_parse_raw_rejects_floats_and_junk():
    for bad in ("1.5", "abc", "", " ", "1e5", 1.5, True):
        with pytest.raises(QuantityError):
            parse_raw(bad)  # type: ignore[arg-type]
    with pytest.raises(QuantityError):
        parse_raw(2**256)


def test_decimal_conversions_exact():
    assert raw_to_decimal(1_500_000, 6) == 1.5
    assert decimal_to_raw("1.5", 6) == 1_500_000
    assert decimal_to_raw("0.0000019", 6) == 1  # rounds down, never up
    assert format_units(123456789012345678901234567890, 18) == "123456789012.345678901234567890"


def test_round_trip_through_typescript():
    """The TypeScript SDK path: JSON -> BigInt -> string must be lossless."""
    big = str(2**200 + 7)
    script = f'const v = {json.dumps({"amount_in_raw": big})}; const n = BigInt(v.amount_in_raw); console.log(JSON.stringify({{ back: n.toString(), plus: (n + 1n).toString() }}));'
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True, cwd=Path(__file__).parents[2])
    out = json.loads(r.stdout)
    assert out["back"] == big
    assert int(out["plus"]) == int(big) + 1
