"""CLMM math: exact vectors from the Uniswap v3-core test suite plus property checks.

Vectors come from ``test/TickMath.spec.ts`` (+ its snapshot), ``test/SwapMath.spec.ts`` and
``test/SqrtPriceMath.spec.ts``. ``encode_price_sqrt`` mirrors ``test/shared/utilities.ts``
(bignumber.js with DECIMAL_PLACES 40, half-up rounding, floored result) so the inputs are
the same integers the Solidity tests were fed.
"""

from __future__ import annotations

from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal, getcontext

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from market_replay.venues.clmm.math import (
    MAX_SQRT_RATIO,
    MAX_TICK,
    MAX_UINT128,
    MAX_UINT256,
    MIN_SQRT_RATIO,
    MIN_TICK,
    ClMathError,
    add_delta,
    compute_swap_step,
    get_amount0_delta,
    get_amount1_delta,
    get_next_sqrt_price_from_input,
    get_next_sqrt_price_from_output,
    get_sqrt_ratio_at_tick,
    get_tick_at_sqrt_ratio,
    mul_div,
    mul_div_rounding_up,
    tick_spacing_to_max_liquidity_per_tick,
)

getcontext().prec = 200
_DP40 = Decimal(1).scaleb(-40)


def encode_price_sqrt(reserve1: int, reserve0: int) -> int:
    """``encodePriceSqrt`` from v3-core ``test/shared/utilities.ts``: floor(sqrt(r1/r0) * 2^96)."""
    ratio = (Decimal(reserve1) / Decimal(reserve0)).quantize(_DP40, rounding=ROUND_HALF_UP)
    root = ratio.sqrt().quantize(_DP40, rounding=ROUND_HALF_UP)
    return int((root * Decimal(2) ** 96).to_integral_value(rounding=ROUND_FLOOR))


def e18(n: int) -> int:
    return n * 10**18


# --------------------------------------------------------------------------- TickMath

TICK_MATH_SNAPSHOT = {
    0: 79228162514264337593543950336,
    50: 79426470787362580746886972461,
    -50: 79030349367926598376800521322,
    100: 79625275426524748796330556128,
    -100: 78833030112140176575862854579,
    250: 80224679980005306637834519095,
    -250: 78244023372248365697264290337,
    500: 81233731461783161732293370115,
    -500: 77272108795590369356373805297,
    1000: 83290069058676223003182343270,
    -1000: 75364347830767020784054125655,
    2500: 89776708723587163891445672585,
    -2500: 69919044979842180277688105136,
    3000: 92049301871182272007977902845,
    -3000: 68192822843687888778582228483,
    4000: 96768528593268422080558758223,
    -4000: 64867181785621769311890333195,
    5000: 101729702841318637793976746270,
    -5000: 61703726247759831737814779831,
    50000: 965075977353221155028623082916,
    -50000: 6504256538020985011912221507,
    150000: 143194173941309278083010301478497,
    -150000: 43836292794701720435367485,
    250000: 21246587762933397357449903968194344,
    -250000: 295440463448801648376846,
    500000: 5697689776495288729098254600827762987878,
    -500000: 1101692437043807371,
    738203: 847134979253254120489401328389043031315994541,
    -738203: 7409801140451,
}


@pytest.mark.parametrize(("tick", "expected"), sorted(TICK_MATH_SNAPSHOT.items()))
def test_get_sqrt_ratio_at_tick_snapshot(tick, expected):
    assert get_sqrt_ratio_at_tick(tick) == expected


def test_get_sqrt_ratio_at_tick_bounds():
    assert get_sqrt_ratio_at_tick(MIN_TICK) == MIN_SQRT_RATIO == 4295128739
    assert get_sqrt_ratio_at_tick(MIN_TICK + 1) == 4295343490
    assert get_sqrt_ratio_at_tick(MAX_TICK - 1) == 1461373636630004318706518188784493106690254656249
    assert (
        get_sqrt_ratio_at_tick(MAX_TICK)
        == MAX_SQRT_RATIO
        == 1461446703485210103287273052203988822378723970342
    )
    assert MIN_TICK == -887272 and MAX_TICK == 887272
    for bad in (MIN_TICK - 1, MAX_TICK + 1):
        with pytest.raises(ClMathError, match="T"):
            get_sqrt_ratio_at_tick(bad)


GET_TICK_AT_SQRT_RATIO_SNAPSHOT = [
    (MIN_SQRT_RATIO, -887272),
    (encode_price_sqrt(10**12, 1), 276324),
    (encode_price_sqrt(10**6, 1), 138162),
    (encode_price_sqrt(1, 64), -41591),
    (encode_price_sqrt(1, 8), -20796),
    (encode_price_sqrt(1, 2), -6932),
    (encode_price_sqrt(1, 1), 0),
    (encode_price_sqrt(2, 1), 6931),
    (encode_price_sqrt(8, 1), 20795),
    (encode_price_sqrt(64, 1), 41590),
    (encode_price_sqrt(1, 10**6), -138163),
    (encode_price_sqrt(1, 10**12), -276325),
    (MAX_SQRT_RATIO - 1, 887271),
]


@pytest.mark.parametrize(("ratio", "expected"), GET_TICK_AT_SQRT_RATIO_SNAPSHOT)
def test_get_tick_at_sqrt_ratio_snapshot(ratio, expected):
    tick = get_tick_at_sqrt_ratio(ratio)
    assert tick == expected
    # boundary rule: the tick is the greatest one whose ratio is <= the input
    assert get_sqrt_ratio_at_tick(tick) <= ratio < get_sqrt_ratio_at_tick(tick + 1)


def test_get_tick_at_sqrt_ratio_bounds():
    assert get_tick_at_sqrt_ratio(4295343490) == MIN_TICK + 1
    assert get_tick_at_sqrt_ratio(1461373636630004318706518188784493106690254656249) == MAX_TICK - 1
    assert get_tick_at_sqrt_ratio(MAX_SQRT_RATIO - 1) == MAX_TICK - 1
    for bad in (MIN_SQRT_RATIO - 1, MAX_SQRT_RATIO):
        with pytest.raises(ClMathError, match="R"):
            get_tick_at_sqrt_ratio(bad)


def test_encode_price_sqrt_helper_matches_known_inputs():
    assert encode_price_sqrt(1, 1) == 2**96
    assert encode_price_sqrt(1, 4) == 2**95


@given(st.integers(min_value=MIN_TICK, max_value=MAX_TICK - 1))
@settings(max_examples=400)
def test_tick_sqrt_round_trip_and_monotonic(tick):
    ratio = get_sqrt_ratio_at_tick(tick)
    assert get_tick_at_sqrt_ratio(ratio) == tick
    assert ratio < get_sqrt_ratio_at_tick(tick + 1)
    if ratio + 1 < MAX_SQRT_RATIO:
        # one wei above a tick's ratio is still that tick unless the next tick starts there
        nxt = get_sqrt_ratio_at_tick(tick + 1)
        assert get_tick_at_sqrt_ratio(ratio + 1) == (tick + 1 if nxt == ratio + 1 else tick)


@given(st.integers(min_value=MIN_SQRT_RATIO, max_value=MAX_SQRT_RATIO - 1))
@settings(max_examples=400)
def test_tick_at_sqrt_ratio_is_greatest_tick_at_or_below(ratio):
    tick = get_tick_at_sqrt_ratio(ratio)
    assert MIN_TICK <= tick < MAX_TICK
    assert get_sqrt_ratio_at_tick(tick) <= ratio < get_sqrt_ratio_at_tick(tick + 1)


# ---------------------------------------------------------------------------- FullMath


def test_mul_div_reference_and_rounding():
    assert mul_div(2**200, 2**100, 2**50) == 2**250
    assert mul_div(MAX_UINT256, MAX_UINT256, MAX_UINT256) == MAX_UINT256
    assert mul_div_rounding_up(7, 3, 4) == 6 and mul_div(7, 3, 4) == 5
    assert mul_div_rounding_up(8, 3, 4) == 6
    with pytest.raises(ClMathError):
        mul_div(1, 1, 0)
    with pytest.raises(ClMathError):
        mul_div(2**255, 4, 1)
    with pytest.raises(ClMathError):
        mul_div_rounding_up(MAX_UINT256, MAX_UINT256, MAX_UINT256 - 1)
    with pytest.raises(ClMathError):
        mul_div(-1, 1, 1)


@given(
    st.integers(min_value=0, max_value=MAX_UINT256),
    st.integers(min_value=0, max_value=MAX_UINT256),
    st.integers(min_value=1, max_value=MAX_UINT256),
)
@settings(max_examples=200)
def test_mul_div_matches_exact_rational(a, b, d):
    exact = (a * b) // d
    if exact > MAX_UINT256:
        with pytest.raises(ClMathError):
            mul_div(a, b, d)
    else:
        assert mul_div(a, b, d) == exact
        up = exact + (1 if (a * b) % d else 0)
        if up > MAX_UINT256:
            with pytest.raises(ClMathError):
                mul_div_rounding_up(a, b, d)
        else:
            assert mul_div_rounding_up(a, b, d) == up


# ----------------------------------------------------------------------- SqrtPriceMath


def test_get_next_sqrt_price_from_input_vectors():
    p = encode_price_sqrt(1, 1)
    assert get_next_sqrt_price_from_input(p, e18(1), e18(1) // 10, False) == 87150978765690771352898345369
    assert get_next_sqrt_price_from_input(p, e18(1), e18(1) // 10, True) == 72025602285694852357767227579
    assert get_next_sqrt_price_from_input(p, e18(10), 2**100, True) == 624999999995069620
    assert get_next_sqrt_price_from_input(p, 1, MAX_UINT256 // 2, True) == 1
    assert get_next_sqrt_price_from_input(1, 1, 2**255, True) == 1
    assert get_next_sqrt_price_from_input(p, e18(1) // 10, 0, True) == p
    assert get_next_sqrt_price_from_input(p, e18(1) // 10, 0, False) == p
    sqrt_p = 2**160 - 1
    max_amount_no_overflow = MAX_UINT256 - ((MAX_UINT128 << 96) // sqrt_p)
    assert get_next_sqrt_price_from_input(sqrt_p, MAX_UINT128, max_amount_no_overflow, True) == 1
    with pytest.raises(ClMathError):
        get_next_sqrt_price_from_input(0, 0, e18(1) // 10, False)
    with pytest.raises(ClMathError):
        get_next_sqrt_price_from_input(1, 0, e18(1) // 10, True)
    with pytest.raises(ClMathError):
        get_next_sqrt_price_from_input(2**160 - 1, 1024, 1024, False)


def test_get_next_sqrt_price_from_output_vectors():
    p = encode_price_sqrt(1, 1)
    price = 20282409603651670423947251286016
    assert get_next_sqrt_price_from_output(p, e18(1), e18(1) // 10, False) == 88031291682515930659493278152
    assert get_next_sqrt_price_from_output(p, e18(1), e18(1) // 10, True) == 71305346262837903834189555302
    assert get_next_sqrt_price_from_output(price, 1024, 262143, True) == 77371252455336267181195264
    assert get_next_sqrt_price_from_output(p, e18(1) // 10, 0, True) == p
    for liquidity, amount_out, zero_for_one in (
        (1024, 4, False),
        (1024, 5, False),
        (1024, 262145, True),
        (1024, 262144, True),
    ):
        with pytest.raises(ClMathError):
            get_next_sqrt_price_from_output(price, liquidity, amount_out, zero_for_one)
    for zero_for_one in (True, False):
        with pytest.raises(ClMathError):
            get_next_sqrt_price_from_output(p, 1, MAX_UINT256, zero_for_one)
    with pytest.raises(ClMathError):
        get_next_sqrt_price_from_output(0, 0, 1, False)


def test_amount_delta_vectors():
    p, q = encode_price_sqrt(1, 1), encode_price_sqrt(121, 100)
    assert get_amount0_delta(p, encode_price_sqrt(2, 1), 0, True) == 0
    assert get_amount0_delta(p, p, 0, True) == 0
    assert get_amount0_delta(p, q, e18(1), True) == 90909090909090910
    assert get_amount0_delta(p, q, e18(1), False) == 90909090909090909
    big_a, big_b = encode_price_sqrt(2**90, 1), encode_price_sqrt(2**96, 1)
    assert get_amount0_delta(big_a, big_b, e18(1), True) == get_amount0_delta(big_a, big_b, e18(1), False) + 1
    assert get_amount1_delta(p, q, e18(1), True) == 100000000000000000
    assert get_amount1_delta(p, q, e18(1), False) == 99999999999999999
    # argument order is irrelevant
    assert get_amount0_delta(q, p, e18(1), True) == 90909090909090910
    with pytest.raises(ClMathError):
        get_amount0_delta(0, q, 1, True)


def test_swap_computation_overflowing_product():
    sqrt_p = 1025574284609383690408304870162715216695788925244
    liquidity = 50015962439936049619261659728067971248
    sqrt_q = get_next_sqrt_price_from_input(sqrt_p, liquidity, 406, True)
    assert sqrt_q == 1025574284609383582644711336373707553698163132913
    assert get_amount0_delta(sqrt_q, sqrt_p, liquidity, True) == 406


# ---------------------------------------------------------------------------- SwapMath


def test_swap_step_exact_in_capped_at_target_one_for_zero():
    price, target, liquidity, amount = encode_price_sqrt(1, 1), encode_price_sqrt(101, 100), e18(2), e18(1)
    sqrt_q, amount_in, amount_out, fee = compute_swap_step(price, target, liquidity, amount, 600)
    assert (amount_in, fee, amount_out) == (9975124224178055, 5988667735148, 9925619580021728)
    assert amount_in + fee < amount
    assert sqrt_q == target
    assert sqrt_q < get_next_sqrt_price_from_input(price, liquidity, amount, False)


def test_swap_step_exact_out_capped_at_target_one_for_zero():
    price, target, liquidity, amount = encode_price_sqrt(1, 1), encode_price_sqrt(101, 100), e18(2), -e18(1)
    sqrt_q, amount_in, amount_out, fee = compute_swap_step(price, target, liquidity, amount, 600)
    assert (amount_in, fee, amount_out) == (9975124224178055, 5988667735148, 9925619580021728)
    assert amount_out < -amount
    assert sqrt_q == target
    assert sqrt_q < get_next_sqrt_price_from_output(price, liquidity, -amount, False)


def test_swap_step_exact_in_fully_spent_one_for_zero():
    price, target, liquidity, amount = encode_price_sqrt(1, 1), encode_price_sqrt(1000, 100), e18(2), e18(1)
    sqrt_q, amount_in, amount_out, fee = compute_swap_step(price, target, liquidity, amount, 600)
    assert (amount_in, fee, amount_out) == (999400000000000000, 600000000000000, 666399946655997866)
    assert amount_in + fee == amount
    assert sqrt_q < target
    assert sqrt_q == get_next_sqrt_price_from_input(price, liquidity, amount - fee, False)


def test_swap_step_exact_out_fully_received_one_for_zero():
    price, target, liquidity, amount = encode_price_sqrt(1, 1), encode_price_sqrt(10000, 100), e18(2), -e18(1)
    sqrt_q, amount_in, amount_out, fee = compute_swap_step(price, target, liquidity, amount, 600)
    assert (amount_in, fee, amount_out) == (2000000000000000000, 1200720432259356, e18(1))
    assert sqrt_q < target
    assert sqrt_q == get_next_sqrt_price_from_output(price, liquidity, -amount, False)


def test_swap_step_amount_out_capped_at_desired():
    sqrt_q, amount_in, amount_out, fee = compute_swap_step(
        417332158212080721273783715441582, 1452870262520218020823638996, 159344665391607089467575320103, -1, 1
    )
    assert (amount_in, fee, amount_out) == (1, 1, 1)  # amount_out would be 2 if not capped
    assert sqrt_q == 417332158212080721273783715441581


def test_swap_step_target_price_of_one_uses_partial_input():
    remaining = 3915081100057732413702495386755767
    sqrt_q, amount_in, amount_out, fee = compute_swap_step(2, 1, 1, remaining, 1)
    assert (amount_in, fee) == (39614081257132168796771975168, 39614120871253040049813)
    assert amount_in + fee <= remaining
    assert (amount_out, sqrt_q) == (0, 1)


def test_swap_step_entire_input_taken_as_fee():
    sqrt_q, amount_in, amount_out, fee = compute_swap_step(
        2413, 79887613182836312, 1985041575832132834610021537970, 10, 1872
    )
    assert (amount_in, fee, amount_out, sqrt_q) == (0, 10, 0, 2413)


def test_swap_step_intermediate_insufficient_liquidity_exact_output():
    sqrt_p = 20282409603651670423947251286016
    # virtual reserves of token1 are only 4
    sqrt_q, amount_in, amount_out, fee = compute_swap_step(sqrt_p, sqrt_p * 11 // 10, 1024, -4, 3000)
    assert (amount_out, sqrt_q, amount_in, fee) == (0, sqrt_p * 11 // 10, 26215, 79)
    # virtual reserves of token0 are only 262144
    sqrt_q, amount_in, amount_out, fee = compute_swap_step(sqrt_p, sqrt_p * 9 // 10, 1024, -263000, 3000)
    assert (amount_out, sqrt_q, amount_in, fee) == (26214, sqrt_p * 9 // 10, 1, 1)


def test_swap_step_rejects_invalid_fee():
    with pytest.raises(ClMathError, match="F"):
        compute_swap_step(2**96, 2**95, 1, 1, 1_000_000)


# ---------------------------------------------------------------- LiquidityMath / Tick


def test_add_delta():
    assert add_delta(1, 0) == 1
    assert add_delta(1, -1) == 0
    assert add_delta(1, 1) == 2
    assert add_delta(2**128 - 15, 14) == 2**128 - 1
    with pytest.raises(ClMathError, match="LA"):
        add_delta(2**128 - 15, 15)
    with pytest.raises(ClMathError, match="LS"):
        add_delta(0, -1)
    with pytest.raises(ClMathError, match="LS"):
        add_delta(3, -4)


def test_tick_spacing_to_max_liquidity_per_tick():
    # values from v3-core Tick.spec.ts
    assert tick_spacing_to_max_liquidity_per_tick(10) == 1917569901783203986719870431555990
    assert tick_spacing_to_max_liquidity_per_tick(60) == 11505743598341114571880798222544994
    assert tick_spacing_to_max_liquidity_per_tick(200) == 38350317471085141830651933667504588
    assert tick_spacing_to_max_liquidity_per_tick(887272) == (2**128 - 1) // 3
    assert tick_spacing_to_max_liquidity_per_tick(2302) == 441351967472034323558203122479595605
