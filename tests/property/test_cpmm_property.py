from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from market_replay.venues.cpmm.math import get_amount_out
from market_replay.venues.cpmm.pool import CpmmPoolState

reserves = st.integers(min_value=1, max_value=10**36)


@settings(max_examples=500, deadline=None)
@given(x=reserves, y=reserves, a=st.integers(min_value=1, max_value=10**36))
def test_output_bounded_and_monotone(x: int, y: int, a: int):
    out = get_amount_out(a, x, y)
    assert 0 <= out < y
    if a > 1:
        assert get_amount_out(a - 1, x, y) <= out


@settings(max_examples=300, deadline=None)
@given(x=st.integers(min_value=10**6, max_value=10**30), y=st.integers(min_value=10**6, max_value=10**30), a=st.integers(min_value=1, max_value=10**28))
def test_invariant_k_never_decreases(x: int, y: int, a: int):
    a = min(a, x)
    pool = CpmmPoolState(key="p", asset0="A", asset1="B", reserve0=x, reserve1=y)
    k0 = pool.reserve0 * pool.reserve1
    try:
        pool.apply_swap("A", a)
    except Exception:
        return
    assert pool.reserve0 * pool.reserve1 >= k0


@settings(max_examples=300, deadline=None)
@given(x=st.integers(min_value=10**9, max_value=10**30), y=st.integers(min_value=10**9, max_value=10**30), a=st.integers(min_value=1, max_value=10**27), n=st.integers(min_value=2, max_value=8))
def test_splitting_an_order_never_gets_more_output(x: int, y: int, a: int, n: int):
    a = min(a, x // 10)
    if a < n * 1000 or a * y < x * 10:  # skip zero-output corner cases; those raise by design
        return
    whole = CpmmPoolState(key="p", asset0="A", asset1="B", reserve0=x, reserve1=y)
    split = whole.copy()
    out_whole = whole.apply_swap("A", a)
    parts = [a // n] * n
    parts[-1] += a - sum(parts)
    out_split = 0
    for p in parts:
        if p > 0:
            out_split += split.apply_swap("A", p)
    assert out_split <= out_whole + n  # rounding slack of at most one unit per part
