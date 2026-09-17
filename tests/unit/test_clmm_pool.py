"""CLMM pool: hand-derived scenario, tick crossing, properties, reconciliation."""

from __future__ import annotations

from fractions import Fraction
from math import ceil, floor

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from market_replay.venues.clmm.math import (
    MAX_SQRT_RATIO,
    MIN_SQRT_RATIO,
    MIN_TICK,
    ClMathError,
    get_next_sqrt_price_from_input,
    get_sqrt_ratio_at_tick,
    get_tick_at_sqrt_ratio,
)
from market_replay.venues.clmm.pool import ClPoolState, UnsupportedMechanics

Q96 = 2**96
L = 10**18


def fresh_pool(fee_pips: int = 3000, spacing: int = 60) -> ClPoolState:
    return ClPoolState.initialize("p", "A", "B", fee_pips, spacing, Q96)


# ------------------------------------------------------------------- scenario


def test_scenario_mint_swap_cross_drain_burn():
    """Numbers below are derived from the v3 formulas with exact rationals (see comments)."""
    pool = fresh_pool()
    assert pool.tick == 0 and pool.sqrt_price_x96 == 79228162514264337593543950336

    s_lower, s_upper = get_sqrt_ratio_at_tick(-120), get_sqrt_ratio_at_tick(120)
    assert s_lower == 78754240422856966435523493930
    assert s_upper == 79704936542881920863903188246

    # mint L over [-120, 120] with the price inside the range:
    #   amount0 = ceil(ceil(L * 2^96 * (sU - sP) / sU) / sP), amount1 = ceil(L * (sP - sL) / 2^96)
    amount0, amount1 = pool.apply_modify_liquidity(-120, 120, L)
    hand0 = ceil(Fraction(ceil(Fraction(L * Q96 * (s_upper - Q96), s_upper)), Q96))
    hand1 = ceil(Fraction(L * (Q96 - s_lower), Q96))
    assert (amount0, amount1) == (hand0, hand1) == (5981737760509663, 5981737760509663)
    assert pool.liquidity == L
    assert pool.ticks == {-120: (L, L), 120: (-L, L)}

    # exact-in 1e15 of token0 at fee 3000 stays inside the range:
    #   less_fee = 1e15 * 997000 / 1e6 = 997e12 < amount0 needed to reach tick -120 (~6e15)
    #   next = ceil(L * 2^96 / (L + less_fee))                 (getNextSqrtPriceFromAmount0RoundingUp)
    #   amount_in = ceil(ceil(L * 2^96 * (sP - next) / sP) / next)   rounded up
    #   amount_out = floor(L * (sP - next) / 2^96)                rounded down
    #   fee = 1e15 - amount_in (target not reached: the remainder is the fee)
    less_fee = 997 * 10**12
    nxt = ceil(Fraction(L * Q96, L + less_fee))
    hand_in = ceil(Fraction(ceil(Fraction(L * Q96 * (Q96 - nxt), Q96)), nxt))
    hand_out = floor(Fraction(L * (Q96 - nxt), Q96))
    assert nxt == 79149250711305166342700278159
    assert (hand_in, hand_out) == (997000000000000, 996006981039903)
    result = pool.swap(True, 10**15)
    assert result == (10**15, -996006981039903, nxt, L, -20)
    assert get_tick_at_sqrt_ratio(nxt) == -20
    assert (pool.sqrt_price_x96, pool.tick, pool.liquidity) == (nxt, -20, L)
    assert pool.spot_price_fraction("A") == Fraction(nxt * nxt, Q96 * Q96)

    # a much larger exact-in swap crosses -120: liquidity_net at -120 is +L, negated leftward -> 0.
    # Step 1 (to -120): amount_in = ceil(ceil(L*2^96*(next - sL)/next)/sL), fee = ceil(amount_in*3000/997000),
    # amount_out = floor(L*(next - sL)/2^96). Steps in zero liquidity move nothing and run the price
    # straight to the limit MIN_SQRT_RATIO + 1, where the loop stops; the tick is then
    # getTickAtSqrtRatio(MIN_SQRT_RATIO + 1) == MIN_TICK.
    step_in = ceil(Fraction(ceil(Fraction(L * Q96 * (nxt - s_lower), nxt)), s_lower))
    step_fee = ceil(Fraction(step_in * 3000, 997000))
    step_out = floor(Fraction(L * (nxt - s_lower), Q96))
    assert step_in + step_fee == 5035841794200769 and step_out == 4985730779469759
    result = pool.swap(True, 10**17)
    assert result == (5035841794200769, -4985730779469759, MIN_SQRT_RATIO + 1, 0, MIN_TICK)
    assert pool.liquidity == 0 and pool.tick == MIN_TICK and pool.sqrt_price_x96 == MIN_SQRT_RATIO + 1
    assert pool.ticks == {-120: (L, L), 120: (-L, L)}  # crossing does not touch the tick table

    # the pool holds every wei it was ever paid; only 1 wei of token1 remains after the drain
    paid0 = amount0 + 10**15 + 5035841794200769
    remaining1 = amount1 - 996006981039903 - 4985730779469759
    assert remaining1 == 1

    # burn with the price below the range: token0 only, rounded down:
    #   amount0 = -floor(floor(L * 2^96 * (sU - sL) / sU) / sL)
    burned = pool.apply_modify_liquidity(-120, 120, -L)
    hand_burn0 = -floor(Fraction(floor(Fraction(L * Q96 * (s_upper - s_lower), s_upper)), s_lower))
    assert burned == (hand_burn0, 0) == (-11999472029327827, 0)
    assert -burned[0] <= paid0
    assert pool.ticks == {} and pool.liquidity == 0


def test_swap_stops_at_explicit_price_limit():
    pool = fresh_pool()
    pool.apply_modify_liquidity(-120, 120, L)
    limit = get_sqrt_ratio_at_tick(-10)
    amount0, amount1, sqrt_after, liquidity_after, tick_after = pool.swap(
        True, 10**17, sqrt_price_limit_x96=limit
    )
    assert sqrt_after == limit and tick_after == -10 and liquidity_after == L
    assert 0 < amount0 < 10**17 and amount1 < 0
    with pytest.raises(ClMathError, match="SPL"):
        pool.swap(True, 1, sqrt_price_limit_x96=pool.sqrt_price_x96 + 1)
    with pytest.raises(ClMathError, match="SPL"):
        pool.swap(False, 1, sqrt_price_limit_x96=MAX_SQRT_RATIO)
    with pytest.raises(ClMathError, match="AS"):
        pool.swap(True, 0)


def test_exact_output_swap_and_direction_one_for_zero():
    pool = fresh_pool()
    pool.apply_modify_liquidity(-120, 120, L)
    amount0, amount1, sqrt_after, _, tick_after = pool.swap(False, -(10**15))
    assert amount0 == -(10**15)
    assert amount1 > 10**15  # input including fee
    assert sqrt_after > Q96 and tick_after > 0
    assert tick_after == get_tick_at_sqrt_ratio(sqrt_after)


def test_crossing_upward_adds_liquidity_from_outer_range():
    pool = fresh_pool()
    pool.apply_modify_liquidity(-60, 60, L)
    pool.apply_modify_liquidity(60, 600, 3 * L)
    assert pool.liquidity == L and pool.ticks[60] == (2 * L, 4 * L)
    _, _, sqrt_after, liquidity_after, tick_after = pool.swap(False, 10**16)
    assert liquidity_after == 3 * L
    assert tick_after >= 60 and sqrt_after > get_sqrt_ratio_at_tick(60)


def test_liquidity_out_of_range_moves_only_one_asset():
    pool = fresh_pool()
    a0, a1 = pool.apply_modify_liquidity(60, 120, L)
    assert a0 > 0 and a1 == 0 and pool.liquidity == 0
    a0, a1 = pool.apply_modify_liquidity(-120, -60, L)
    assert a0 == 0 and a1 > 0 and pool.liquidity == 0
    assert pool.ticks == {60: (L, L), 120: (-L, L), -120: (L, L), -60: (-L, L)}


def test_modify_liquidity_validation_leaves_state_intact():
    pool = fresh_pool()
    before = pool.copy()
    for lower, upper, delta, code in (
        (120, 60, L, "TLU"),
        (-887280, 0, L, "TLM"),
        (0, 887280, L, "TUM"),
        (-61, 60, L, "TS"),
        (-60, 60, -1, "LS"),
        (-60, 60, 2**127, "I128"),
    ):
        with pytest.raises(ClMathError, match=code):
            pool.apply_modify_liquidity(lower, upper, delta)
    assert pool.ticks == before.ticks and pool.liquidity == before.liquidity
    assert pool.apply_modify_liquidity(-60, 60, 0) == (0, 0)


def test_dynamic_fee_override_changes_output_only_for_that_swap():
    pool = fresh_pool(fee_pips=3000)
    pool.apply_modify_liquidity(-120, 120, L)
    cheap = pool.copy().swap(True, 10**15, fee_pips=500)
    default = pool.copy().swap(True, 10**15)
    assert -cheap[1] > -default[1]
    assert pool.fee_pips == 3000
    with pytest.raises(ClMathError, match="F"):
        pool.swap(True, 1, fee_pips=1_000_000)


def test_halted_and_restrictions_and_model():
    pool = fresh_pool()
    pool.apply_modify_liquidity(-120, 120, L)
    pool.restrictions["A"] = {"sell_blocked": True}
    assert pool.transfer_blocked("A", "sell") == "FIXTURE_SELL_BLOCKED"
    assert pool.transfer_blocked("A", "buy") is None
    assert pool.transfer_blocked("B", "sell") is None
    pool.halted = True
    with pytest.raises(ClMathError, match="POOL_HALTED"):
        pool.swap(True, 1)
    with pytest.raises(ClMathError, match="POOL_HALTED"):
        pool.apply_modify_liquidity(-120, 120, 1)
    with pytest.raises(UnsupportedMechanics):
        ClPoolState("p", "A", "B", 3000, 60, Q96, 0, model="uniswap_v2_plain")
    v4 = ClPoolState("p", "A", "B", 3000, 60, Q96, 0, model="uniswap_v4_cl")
    assert v4.model == "uniswap_v4_cl"


def test_broker_helpers_do_not_mutate():
    pool = fresh_pool()
    pool.apply_modify_liquidity(-120, 120, L)
    snapshot = pool.copy()
    q = pool.quote("A", 10**15)
    assert (q.asset_in, q.asset_out, q.amount_in, q.amount_out) == ("A", "B", 10**15, 996006981039903)
    assert pool.max_out("A", 10**15) == 996006981039903
    assert pool.max_out("B", 10**15) > 0
    assert (pool.sqrt_price_x96, pool.tick, pool.liquidity) == (
        snapshot.sqrt_price_x96,
        snapshot.tick,
        snapshot.liquidity,
    )
    assert pool.apply_swap("A", 10**15) == 996006981039903
    assert pool.sqrt_price_x96 == q.sqrt_price_after_x96 and pool.tick == q.tick_after
    with pytest.raises(ClMathError, match="ASSET_NOT_IN_POOL"):
        pool.quote("C", 1)
    assert pool.spot_price_fraction("B") == 1 / pool.spot_price_fraction("A")


def test_copy_independence():
    pool = fresh_pool()
    pool.apply_modify_liquidity(-120, 120, L)
    pool.restrictions["A"] = {"sell_blocked": True}
    clone = pool.copy()
    clone.swap(True, 10**15)
    clone.apply_modify_liquidity(-120, 120, -L)
    clone.restrictions["A"]["buy_blocked"] = True
    clone.halted = True
    assert pool.sqrt_price_x96 == Q96 and pool.tick == 0 and pool.liquidity == L
    assert pool.ticks == {-120: (L, L), 120: (-L, L)}
    assert pool.restrictions == {"A": {"sell_blocked": True}} and pool.halted is False


# ------------------------------------------------------------------ properties

amounts = st.integers(min_value=1, max_value=10**19)
liquidities = st.integers(min_value=10**6, max_value=10**24)


@given(amounts, liquidities, st.booleans())
@settings(max_examples=150, deadline=None)
def test_exact_in_never_pays_more_than_the_pool_holds(amount_in, liquidity, zero_for_one):
    pool = fresh_pool()
    minted0, minted1 = pool.apply_modify_liquidity(-1200, 1200, liquidity)
    amount0, amount1, sqrt_after, liquidity_after, _ = pool.swap(zero_for_one, amount_in)
    if zero_for_one:
        assert 0 < amount0 <= amount_in and -amount1 <= minted1
    else:
        assert 0 < amount1 <= amount_in and -amount0 <= minted0
    # once out of range the pool has nothing left to sell and the price sits at the limit or inside the range
    assert liquidity_after in (0, liquidity)
    if liquidity_after == 0:
        assert sqrt_after == (MIN_SQRT_RATIO + 1 if zero_for_one else MAX_SQRT_RATIO - 1)


@given(st.integers(min_value=1, max_value=250_000), liquidities)
@settings(max_examples=150, deadline=None)
def test_k_invariance_inside_one_tick_range(input_ppm, liquidity):
    """Inside one range the swap is a constant-product move of the virtual reserves.

    With x = L / sqrtP and y = L * sqrtP (virtual reserves), the exact post-trade sqrt price is
    q* = L * 2^96 * sqrtP / (L * 2^96 + in_net * sqrtP). v3 rounds q up so the pool never
    over-pays: q* <= q <= q* + 1, out == floor(L * (sqrtP - q) / 2^96), and liquidity is unchanged.
    The input is at most 25% of L so the price stays inside the starting 256-tick bitmap word:
    the on-chain loop (reproduced here) takes one rounded step per word it traverses.
    """
    amount_in = max(1, liquidity * input_ppm // 1_000_000)
    pool = fresh_pool(fee_pips=3000, spacing=60)
    pool.apply_modify_liquidity(-887220, 887220, liquidity)
    sqrt_before = pool.sqrt_price_x96
    amount0, amount1, sqrt_after, liquidity_after, tick_after = pool.swap(True, amount_in)
    assert liquidity_after == liquidity and amount0 == amount_in
    in_net = amount_in * 997000 // 1_000_000
    q_exact = Fraction(liquidity * Q96 * sqrt_before, liquidity * Q96 + in_net * sqrt_before)
    assert q_exact <= sqrt_after <= q_exact + 1
    assert sqrt_after == get_next_sqrt_price_from_input(sqrt_before, liquidity, in_net, True)
    assert -amount1 == floor(Fraction(liquidity * (sqrt_before - sqrt_after), Q96))
    if sqrt_after != sqrt_before:
        assert tick_after == get_tick_at_sqrt_ratio(sqrt_after)
    else:
        # v3 leaves tick at tickNext - 1 when a zero-for-one step ends exactly on a tick boundary
        assert tick_after in (0, -1)
    assert get_sqrt_ratio_at_tick(tick_after) <= sqrt_after <= get_sqrt_ratio_at_tick(tick_after + 1)
    # k (in exact arithmetic) does not decrease for the pool: x' * y' >= x * y with fees retained
    x_after = Fraction(liquidity * Q96, sqrt_after) + Fraction(amount_in - in_net)
    y_after = Fraction(liquidity * sqrt_after, Q96)
    assert x_after * y_after >= Fraction(liquidity * Q96, sqrt_before) * Fraction(
        liquidity * sqrt_before, Q96
    )


@given(st.integers(min_value=1, max_value=10**17), st.booleans())
@settings(max_examples=100, deadline=None)
def test_swap_round_trip_cannot_mint_profit(amount, zero_for_one):
    pool = fresh_pool()
    pool.apply_modify_liquidity(-6000, 6000, 10**21)
    a0, a1, *_ = pool.swap(zero_for_one, amount)
    got = -(a1 if zero_for_one else a0)
    if got == 0:
        return
    b0, b1, *_ = pool.swap(not zero_for_one, got)
    back = -(b0 if zero_for_one else b1)
    assert back < amount


# --------------------------------------------------------------- reconciliation


def test_apply_recorded_swap_reproduces_engine_exactly():
    pool = fresh_pool()
    pool.apply_modify_liquidity(-120, 120, L)
    pool.apply_modify_liquidity(-600, -120, 2 * L)
    reference = pool.copy()
    recorded = reference.swap(True, 3 * 10**16)  # crosses -120 into the deeper range
    diff = pool.apply_recorded_swap(*recorded, fee_pips=3000)
    assert diff == {
        "mode": "exact_in",
        "amount0": 0,
        "amount1": 0,
        "sqrt_price_x96": 0,
        "liquidity": 0,
        "tick": 0,
    }
    assert (pool.sqrt_price_x96, pool.tick, pool.liquidity) == (
        reference.sqrt_price_x96,
        reference.tick,
        reference.liquidity,
    )

    # an exact-output trade is recognised when exact-input replay does not match the event
    pool2 = fresh_pool()
    pool2.apply_modify_liquidity(-120, 120, L)
    reference2 = pool2.copy()
    recorded2 = reference2.swap(False, -(10**15))
    assert pool2.copy().swap(False, recorded2[1]) != recorded2  # exact-in of the same input differs
    diff2 = pool2.apply_recorded_swap(*recorded2)
    assert diff2["mode"] == "exact_out"
    assert all(diff2[k] == 0 for k in ("amount0", "amount1", "sqrt_price_x96", "liquidity", "tick"))


def test_apply_recorded_swap_reports_perturbed_liquidity():
    pool = fresh_pool()
    pool.apply_modify_liquidity(-120, 120, L)
    amount0, amount1, sqrt_after, liquidity_after, tick_after = pool.copy().swap(True, 10**15)
    diff = pool.apply_recorded_swap(amount0, amount1, sqrt_after, liquidity_after - 12345, tick_after, 3000)
    assert diff["liquidity"] == 12345
    assert diff["amount0"] == diff["amount1"] == diff["sqrt_price_x96"] == diff["tick"] == 0
    assert diff["mode"] == "exact_in"
    # the model state is what the swap produced, not the perturbed recording
    assert pool.liquidity == liquidity_after and pool.sqrt_price_x96 == sqrt_after
    # a swap the loop cannot replay (both legs paid in: a hook took a leg) is anchored to the recording
    anchored = pool.apply_recorded_swap(1, 1, sqrt_after + 7, liquidity_after + 5, tick_after)
    assert anchored["mode"] == "anchored_degenerate" and anchored["sqrt_price_x96"] == -7 and anchored["liquidity"] == -5
    assert pool.sqrt_price_x96 == sqrt_after + 7 and pool.liquidity == liquidity_after + 5
