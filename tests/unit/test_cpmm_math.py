"""Acceptance 14, 15, 16, 17: CPMM arithmetic, unsupported curves, round trips, fee once."""

from __future__ import annotations

import random

import pytest

from market_replay.domain.status import PoolModel
from market_replay.venues.cpmm.math import CpmmMathError, get_amount_in, get_amount_out
from market_replay.venues.cpmm.pool import CpmmPoolState, UnsupportedMechanics


def reference(amount_in: int, rin: int, rout: int) -> int:
    # Literal transcription of UniswapV2Library.getAmountOut
    amount_in_with_fee = amount_in * 997
    numerator = amount_in_with_fee * rout
    denominator = rin * 1000 + amount_in_with_fee
    return numerator // denominator


def test_matches_reference_deterministic():
    cases = [(1, 10**18, 10**18), (10**18, 10**21, 10**24), (12345678901234567890, 5 * 10**20, 7 * 10**26), (999, 1000, 1000)]
    for a, x, y in cases:
        assert get_amount_out(a, x, y) == reference(a, x, y)


def test_matches_reference_randomized():
    rng = random.Random(1234)
    for _ in range(5000):
        x = rng.randint(1, 10**30)
        y = rng.randint(1, 10**30)
        a = rng.randint(1, x)
        assert get_amount_out(a, x, y) == reference(a, x, y)


def test_rejects_invalid_inputs():
    with pytest.raises(CpmmMathError):
        get_amount_out(0, 10, 10)
    with pytest.raises(CpmmMathError):
        get_amount_out(-1, 10, 10)
    with pytest.raises(CpmmMathError):
        get_amount_out(1, 0, 10)
    with pytest.raises(CpmmMathError):
        get_amount_out(1, 10, 0)


def test_amount_in_inverse_consistency():
    x, y = 10**24, 10**24
    out = get_amount_out(10**20, x, y)
    need = get_amount_in(out, x, y)
    assert need <= 10**20 and get_amount_out(need, x, y) >= out


def test_immediate_round_trip_cannot_mint_profit():
    pool = CpmmPoolState(key="p", asset0="A", asset1="B", reserve0=10**24, reserve1=10**24)
    for a in (10**6, 10**12, 10**18, 10**21):
        p = pool.copy()
        got_b = p.apply_swap("A", a)
        back_a = p.apply_swap("B", got_b)
        assert back_a < a
        # repeated round trips do not reset impact: reserves keep drifting against the trader
        second_b = p.apply_swap("A", a)
        assert second_b <= got_b


def test_fee_embedded_not_double_charged():
    pool = CpmmPoolState(key="p", asset0="A", asset1="B", reserve0=10**24, reserve1=10**24)
    a = 10**20
    out = pool.apply_swap("A", a)
    no_fee = (a * 10**24) // (10**24 + a)
    assert out < no_fee
    # Pool received the full input; no separate fee account exists to charge again
    assert pool.reserve0 == 10**24 + a
    assert pool.reserve1 == 10**24 - out


@pytest.mark.parametrize("model", [PoolModel.UNISWAP_V3, PoolModel.UNISWAP_V4_HOOKED, PoolModel.BONDING_CURVE, PoolModel.UNKNOWN])
def test_unsupported_curves_rejected_by_adapter(model):
    with pytest.raises(UnsupportedMechanics):
        CpmmPoolState(key="p", asset0="A", asset1="B", reserve0=1, reserve1=1, model=model)
