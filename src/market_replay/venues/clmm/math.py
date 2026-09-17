"""Exact integer ports of the Uniswap v3-core math libraries.

Every function here reproduces the Solidity semantics bit for bit: unsigned 256-bit
intermediate behaviour, the same rounding direction as the original, and a
``ClMathError`` wherever the original ``require`` reverts. Nothing here uses floats.

Sources (Uniswap/v3-core, ``contracts/libraries``):

* ``FullMath.sol``       -> :func:`mul_div`, :func:`mul_div_rounding_up`
* ``UnsafeMath.sol``     -> :func:`div_rounding_up`
* ``TickMath.sol``       -> :func:`get_sqrt_ratio_at_tick`, :func:`get_tick_at_sqrt_ratio`
* ``SqrtPriceMath.sol``  -> ``get_amount0_delta``, ``get_amount1_delta``, ``get_next_sqrt_price_*``
* ``SwapMath.sol``       -> :func:`compute_swap_step`
* ``LiquidityMath.sol``  -> :func:`add_delta`
* ``Tick.sol``           -> :func:`tick_spacing_to_max_liquidity_per_tick`

Uniswap v4 (``v4-core/src/libraries``) keeps the same formulas for these libraries; the
differences (sign convention of ``amountSpecified``, a ``MAX_SWAP_FEE`` special case,
protocol-fee stacking) live in the pool layer and are documented there.
"""

from __future__ import annotations

MAX_UINT256 = (1 << 256) - 1
MAX_UINT160 = (1 << 160) - 1
MAX_UINT128 = (1 << 128) - 1
MAX_INT128 = (1 << 127) - 1
MIN_INT128 = -(1 << 127)
MAX_INT256 = (1 << 255) - 1
MIN_INT256 = -(1 << 255)

Q96_RESOLUTION = 96
Q96 = 1 << 96

MIN_TICK = -887272
MAX_TICK = 887272
MIN_SQRT_RATIO = 4295128739
MAX_SQRT_RATIO = 1461446703485210103287273052203988822378723970342

FEE_DENOMINATOR = 1_000_000  # 1e6 pips = 100%


class ClMathError(ValueError):
    """Raised with the short revert code of the Solidity ``require`` that would have failed."""


# ---------------------------------------------------------------------------
# Range checks. Solidity types bound every argument; Python ints do not, so each public
# function checks its inputs and refuses to compute with values the EVM could not hold.
# ---------------------------------------------------------------------------


def _check_uint(value: int, max_value: int, code: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0 or value > max_value:
        raise ClMathError(code)


def _check_int(value: int, min_value: int, max_value: int, code: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < min_value or value > max_value:
        raise ClMathError(code)


# ---------------------------------------------------------------------------
# FullMath / UnsafeMath
# ---------------------------------------------------------------------------


def mul_div(a: int, b: int, denominator: int) -> int:
    """``FullMath.mulDiv``: floor(a * b / denominator) with 512-bit intermediate precision.

    Reverts (``ClMathError("MULDIV")``) when the denominator is zero or the result does not
    fit in a uint256, exactly like ``require(denominator > prod1)`` in the original.
    """
    _check_uint(a, MAX_UINT256, "U256")
    _check_uint(b, MAX_UINT256, "U256")
    _check_uint(denominator, MAX_UINT256, "U256")
    if denominator == 0:
        raise ClMathError("MULDIV")
    result = (a * b) // denominator
    if result > MAX_UINT256:
        raise ClMathError("MULDIV")
    return result


def mul_div_rounding_up(a: int, b: int, denominator: int) -> int:
    """``FullMath.mulDivRoundingUp``: ceil(a * b / denominator), reverting on uint256 overflow."""
    result = mul_div(a, b, denominator)
    if (a * b) % denominator != 0:
        if result == MAX_UINT256:
            raise ClMathError("MULDIV")
        result += 1
    return result


def div_rounding_up(x: int, y: int) -> int:
    """``UnsafeMath.divRoundingUp``: ceil(x / y).

    The Solidity version returns 0 on ``y == 0`` (EVM ``div`` semantics); every call site
    in v3-core guarantees ``y > 0``. We refuse to compute with a zero divisor instead of
    returning a silently wrong number.
    """
    _check_uint(x, MAX_UINT256, "U256")
    _check_uint(y, MAX_UINT256, "U256")
    if y == 0:
        raise ClMathError("DIV0")
    return x // y + (1 if x % y != 0 else 0)


# ---------------------------------------------------------------------------
# TickMath
# ---------------------------------------------------------------------------

# Constants from TickMath.getSqrtRatioAtTick: each is 2^128 / sqrt(1.0001)^(2^k), Q128.128.
_TICK_RATIO_FACTORS: tuple[tuple[int, int], ...] = (
    (0x2, 0xFFF97272373D413259A46990580E213A),
    (0x4, 0xFFF2E50F5F656932EF12357CF3C7FDCC),
    (0x8, 0xFFE5CACA7E10E4E61C3624EAA0941CD0),
    (0x10, 0xFFCB9843D60F6159C9DB58835C926644),
    (0x20, 0xFF973B41FA98C081472E6896DFB254C0),
    (0x40, 0xFF2EA16466C96A3843EC78B326B52861),
    (0x80, 0xFE5DEE046A99A2A811C461F1969C3053),
    (0x100, 0xFCBE86C7900A88AEDCFFC83B479AA3A4),
    (0x200, 0xF987A7253AC413176F2B074CF7815E54),
    (0x400, 0xF3392B0822B70005940C7A398E4B70F3),
    (0x800, 0xE7159475A2C29B7443B29C7FA6E889D9),
    (0x1000, 0xD097F3BDFD2022B8845AD8F792AA5825),
    (0x2000, 0xA9F746462D870FDF8A65DC1F90E061E5),
    (0x4000, 0x70D869A156D2A1B890BB3DF62BAF32F7),
    (0x8000, 0x31BE135F97D08FD981231505542FCFA6),
    (0x10000, 0x9AA508B5B7A84E1C677DE54F3E99BC9),
    (0x20000, 0x5D6AF8DEDB81196699C329225EE604),
    (0x40000, 0x2216E584F5FA1EA926041BEDFE98),
    (0x80000, 0x48A170391F7DC42444E8FA2),
)

_LOG_SQRT10001_FACTOR = 255738958999603826347141
_TICK_LOW_OFFSET = 3402992956809132418596140100660247210
_TICK_HI_OFFSET = 291339464771989622907027621153398088495


def get_sqrt_ratio_at_tick(tick: int) -> int:
    """``TickMath.getSqrtRatioAtTick``: sqrt(1.0001^tick) * 2^96 as a Q64.96 integer.

    Raises ``ClMathError("T")`` when ``|tick| > MAX_TICK``. The Q128.128 product is
    divided by 2^32 rounding up, exactly as the Solidity does, so that
    ``get_tick_at_sqrt_ratio`` of the output is always consistent.
    """
    _check_int(tick, MIN_INT256, MAX_INT256, "T")
    abs_tick = -tick if tick < 0 else tick
    if abs_tick > MAX_TICK:
        raise ClMathError("T")

    ratio = 0xFFFCB933BD6FAD37AA2D162D1A594001 if abs_tick & 0x1 else 0x100000000000000000000000000000000
    for bit, factor in _TICK_RATIO_FACTORS:
        if abs_tick & bit:
            ratio = (ratio * factor) >> 128

    if tick > 0:
        ratio = MAX_UINT256 // ratio

    # Q128.128 -> Q128.96, rounding up; fits in 160 bits by the tick bound.
    return (ratio >> 32) + (0 if ratio % (1 << 32) == 0 else 1)


def get_tick_at_sqrt_ratio(sqrt_price_x96: int) -> int:
    """``TickMath.getTickAtSqrtRatio``: greatest tick whose sqrt ratio is <= the input.

    Raises ``ClMathError("R")`` unless ``MIN_SQRT_RATIO <= sqrt_price_x96 < MAX_SQRT_RATIO``.
    The binary log is computed with the same 14 fixed-point squaring iterations as the
    Solidity, and the low/high candidates are resolved by an exact ``get_sqrt_ratio_at_tick``.
    """
    _check_uint(sqrt_price_x96, MAX_UINT160, "R")
    if not (MIN_SQRT_RATIO <= sqrt_price_x96 < MAX_SQRT_RATIO):
        raise ClMathError("R")
    ratio = sqrt_price_x96 << 32

    msb = ratio.bit_length() - 1  # same result as the 8-step branchless search
    r = ratio >> (msb - 127) if msb >= 128 else ratio << (127 - msb)

    log_2 = (msb - 128) << 64
    for shift in range(63, 49, -1):
        r = (r * r) >> 127  # r < 2^128 so r*r < 2^256: no wraparound to emulate
        f = r >> 128
        log_2 |= f << shift
        r >>= f

    log_sqrt10001 = log_2 * _LOG_SQRT10001_FACTOR  # 128.128 number

    tick_low = (log_sqrt10001 - _TICK_LOW_OFFSET) >> 128
    tick_hi = (log_sqrt10001 + _TICK_HI_OFFSET) >> 128

    if tick_low == tick_hi:
        return tick_low
    return tick_hi if get_sqrt_ratio_at_tick(tick_hi) <= sqrt_price_x96 else tick_low


# ---------------------------------------------------------------------------
# SqrtPriceMath
# ---------------------------------------------------------------------------


def get_next_sqrt_price_from_amount0_rounding_up(
    sqrt_p_x96: int, liquidity: int, amount: int, add: bool
) -> int:
    """``SqrtPriceMath.getNextSqrtPriceFromAmount0RoundingUp``.

    Computes ``liquidity * sqrtP / (liquidity +- amount * sqrtP)`` rounding up, falling back
    to ``liquidity / (liquidity / sqrtP +- amount)`` when the precise form would overflow a
    uint256, exactly as the Solidity does. Raises ``ClMathError("PRICE")`` where the original
    ``require`` (denominator underflow or uint160 overflow) reverts.
    """
    _check_uint(sqrt_p_x96, MAX_UINT160, "U160")
    _check_uint(liquidity, MAX_UINT128, "U128")
    _check_uint(amount, MAX_UINT256, "U256")
    if amount == 0:
        return sqrt_p_x96
    numerator1 = liquidity << Q96_RESOLUTION

    product = amount * sqrt_p_x96
    if add:
        if product <= MAX_UINT256:
            denominator = numerator1 + product
            if denominator <= MAX_UINT256:
                return mul_div_rounding_up(numerator1, sqrt_p_x96, denominator)  # always fits 160 bits
        divisor = numerator1 // sqrt_p_x96 + amount
        if divisor > MAX_UINT256:
            raise ClMathError("PRICE")
        return div_rounding_up(numerator1, divisor)

    if product > MAX_UINT256 or numerator1 <= product:
        raise ClMathError("PRICE")
    denominator = numerator1 - product
    result = mul_div_rounding_up(numerator1, sqrt_p_x96, denominator)
    if result > MAX_UINT160:
        raise ClMathError("PRICE")
    return result


def get_next_sqrt_price_from_amount1_rounding_down(
    sqrt_p_x96: int, liquidity: int, amount: int, add: bool
) -> int:
    """``SqrtPriceMath.getNextSqrtPriceFromAmount1RoundingDown``: ``sqrtP +- amount / liquidity``.

    Adding rounds the quotient down; removing rounds it up. Raises ``ClMathError("PRICE")``
    where the original reverts (uint160 overflow, or the price would not stay positive).
    """
    _check_uint(sqrt_p_x96, MAX_UINT160, "U160")
    _check_uint(liquidity, MAX_UINT128, "U128")
    _check_uint(amount, MAX_UINT256, "U256")
    if liquidity == 0:
        raise ClMathError("DIV0")
    if add:
        if amount <= MAX_UINT160:
            quotient = (amount << Q96_RESOLUTION) // liquidity
        else:
            quotient = mul_div(amount, Q96, liquidity)
        result = sqrt_p_x96 + quotient
        if result > MAX_UINT160:
            raise ClMathError("PRICE")
        return result

    if amount <= MAX_UINT160:
        quotient = div_rounding_up(amount << Q96_RESOLUTION, liquidity)
    else:
        quotient = mul_div_rounding_up(amount, Q96, liquidity)
    if sqrt_p_x96 <= quotient:
        raise ClMathError("PRICE")
    return sqrt_p_x96 - quotient


def get_next_sqrt_price_from_input(
    sqrt_p_x96: int, liquidity: int, amount_in: int, zero_for_one: bool
) -> int:
    """``SqrtPriceMath.getNextSqrtPriceFromInput``; rounds so the target price is never passed."""
    if sqrt_p_x96 <= 0:
        raise ClMathError("PRICE")
    if liquidity <= 0:
        raise ClMathError("LIQ")
    if zero_for_one:
        return get_next_sqrt_price_from_amount0_rounding_up(sqrt_p_x96, liquidity, amount_in, True)
    return get_next_sqrt_price_from_amount1_rounding_down(sqrt_p_x96, liquidity, amount_in, True)


def get_next_sqrt_price_from_output(
    sqrt_p_x96: int, liquidity: int, amount_out: int, zero_for_one: bool
) -> int:
    """``SqrtPriceMath.getNextSqrtPriceFromOutput``; rounds so the target price is always passed."""
    if sqrt_p_x96 <= 0:
        raise ClMathError("PRICE")
    if liquidity <= 0:
        raise ClMathError("LIQ")
    if zero_for_one:
        return get_next_sqrt_price_from_amount1_rounding_down(sqrt_p_x96, liquidity, amount_out, False)
    return get_next_sqrt_price_from_amount0_rounding_up(sqrt_p_x96, liquidity, amount_out, False)


def get_amount0_delta(sqrt_ratio_a_x96: int, sqrt_ratio_b_x96: int, liquidity: int, round_up: bool) -> int:
    """``SqrtPriceMath.getAmount0Delta`` (unsigned): ``L * (sqrtB - sqrtA) / (sqrtB * sqrtA)``.

    The two prices may be passed in either order. Rounds up via two ceiling divisions or
    down via two floor divisions, exactly as the original.
    """
    _check_uint(sqrt_ratio_a_x96, MAX_UINT160, "U160")
    _check_uint(sqrt_ratio_b_x96, MAX_UINT160, "U160")
    _check_uint(liquidity, MAX_UINT128, "U128")
    if sqrt_ratio_a_x96 > sqrt_ratio_b_x96:
        sqrt_ratio_a_x96, sqrt_ratio_b_x96 = sqrt_ratio_b_x96, sqrt_ratio_a_x96

    numerator1 = liquidity << Q96_RESOLUTION
    numerator2 = sqrt_ratio_b_x96 - sqrt_ratio_a_x96

    if sqrt_ratio_a_x96 <= 0:
        raise ClMathError("PRICE")

    if round_up:
        return div_rounding_up(
            mul_div_rounding_up(numerator1, numerator2, sqrt_ratio_b_x96), sqrt_ratio_a_x96
        )
    return mul_div(numerator1, numerator2, sqrt_ratio_b_x96) // sqrt_ratio_a_x96


def get_amount1_delta(sqrt_ratio_a_x96: int, sqrt_ratio_b_x96: int, liquidity: int, round_up: bool) -> int:
    """``SqrtPriceMath.getAmount1Delta`` (unsigned): ``L * (sqrtB - sqrtA) / 2^96``."""
    _check_uint(sqrt_ratio_a_x96, MAX_UINT160, "U160")
    _check_uint(sqrt_ratio_b_x96, MAX_UINT160, "U160")
    _check_uint(liquidity, MAX_UINT128, "U128")
    if sqrt_ratio_a_x96 > sqrt_ratio_b_x96:
        sqrt_ratio_a_x96, sqrt_ratio_b_x96 = sqrt_ratio_b_x96, sqrt_ratio_a_x96
    if round_up:
        return mul_div_rounding_up(liquidity, sqrt_ratio_b_x96 - sqrt_ratio_a_x96, Q96)
    return mul_div(liquidity, sqrt_ratio_b_x96 - sqrt_ratio_a_x96, Q96)


def get_amount0_delta_signed(sqrt_ratio_a_x96: int, sqrt_ratio_b_x96: int, liquidity_delta: int) -> int:
    """Signed ``SqrtPriceMath.getAmount0Delta``: rounds up for positive deltas, down for negative."""
    _check_int(liquidity_delta, MIN_INT128, MAX_INT128, "I128")
    if liquidity_delta < 0:
        return -get_amount0_delta(sqrt_ratio_a_x96, sqrt_ratio_b_x96, -liquidity_delta, False)
    return get_amount0_delta(sqrt_ratio_a_x96, sqrt_ratio_b_x96, liquidity_delta, True)


def get_amount1_delta_signed(sqrt_ratio_a_x96: int, sqrt_ratio_b_x96: int, liquidity_delta: int) -> int:
    """Signed ``SqrtPriceMath.getAmount1Delta``: rounds up for positive deltas, down for negative."""
    _check_int(liquidity_delta, MIN_INT128, MAX_INT128, "I128")
    if liquidity_delta < 0:
        return -get_amount1_delta(sqrt_ratio_a_x96, sqrt_ratio_b_x96, -liquidity_delta, False)
    return get_amount1_delta(sqrt_ratio_a_x96, sqrt_ratio_b_x96, liquidity_delta, True)


# ---------------------------------------------------------------------------
# SwapMath
# ---------------------------------------------------------------------------


def compute_swap_step(
    sqrt_ratio_current_x96: int,
    sqrt_ratio_target_x96: int,
    liquidity: int,
    amount_remaining: int,
    fee_pips: int,
) -> tuple[int, int, int, int]:
    """``SwapMath.computeSwapStep``: one swap step within a single liquidity range.

    ``amount_remaining >= 0`` is exact input (fee included), ``< 0`` exact output. Returns
    ``(sqrt_ratio_next_x96, amount_in, amount_out, fee_amount)`` with v3 rounding: the fee is
    the whole unspent remainder when the target is not reached, otherwise
    ``ceil(amount_in * fee / (1e6 - fee))``.
    """
    _check_uint(sqrt_ratio_current_x96, MAX_UINT160, "U160")
    _check_uint(sqrt_ratio_target_x96, MAX_UINT160, "U160")
    _check_uint(liquidity, MAX_UINT128, "U128")
    _check_int(amount_remaining, MIN_INT256, MAX_INT256, "I256")
    _check_uint(fee_pips, FEE_DENOMINATOR - 1, "F")

    zero_for_one = sqrt_ratio_current_x96 >= sqrt_ratio_target_x96
    exact_in = amount_remaining >= 0
    amount_in = 0
    amount_out = 0

    if exact_in:
        amount_remaining_less_fee = mul_div(amount_remaining, FEE_DENOMINATOR - fee_pips, FEE_DENOMINATOR)
        if zero_for_one:
            amount_in = get_amount0_delta(sqrt_ratio_target_x96, sqrt_ratio_current_x96, liquidity, True)
        else:
            amount_in = get_amount1_delta(sqrt_ratio_current_x96, sqrt_ratio_target_x96, liquidity, True)
        if amount_remaining_less_fee >= amount_in:
            sqrt_ratio_next_x96 = sqrt_ratio_target_x96
        else:
            sqrt_ratio_next_x96 = get_next_sqrt_price_from_input(
                sqrt_ratio_current_x96, liquidity, amount_remaining_less_fee, zero_for_one
            )
    else:
        if zero_for_one:
            amount_out = get_amount1_delta(sqrt_ratio_target_x96, sqrt_ratio_current_x96, liquidity, False)
        else:
            amount_out = get_amount0_delta(sqrt_ratio_current_x96, sqrt_ratio_target_x96, liquidity, False)
        if -amount_remaining >= amount_out:
            sqrt_ratio_next_x96 = sqrt_ratio_target_x96
        else:
            sqrt_ratio_next_x96 = get_next_sqrt_price_from_output(
                sqrt_ratio_current_x96, liquidity, -amount_remaining, zero_for_one
            )

    reached_target = sqrt_ratio_target_x96 == sqrt_ratio_next_x96

    if zero_for_one:
        if not (reached_target and exact_in):
            amount_in = get_amount0_delta(sqrt_ratio_next_x96, sqrt_ratio_current_x96, liquidity, True)
        if not (reached_target and not exact_in):
            amount_out = get_amount1_delta(sqrt_ratio_next_x96, sqrt_ratio_current_x96, liquidity, False)
    else:
        if not (reached_target and exact_in):
            amount_in = get_amount1_delta(sqrt_ratio_current_x96, sqrt_ratio_next_x96, liquidity, True)
        if not (reached_target and not exact_in):
            amount_out = get_amount0_delta(sqrt_ratio_current_x96, sqrt_ratio_next_x96, liquidity, False)

    # cap the output amount to not exceed the remaining output amount
    if not exact_in and amount_out > -amount_remaining:
        amount_out = -amount_remaining

    if exact_in and sqrt_ratio_next_x96 != sqrt_ratio_target_x96:
        # the target was not reached: the remainder of the input is taken as fee
        fee_amount = amount_remaining - amount_in
    else:
        fee_amount = mul_div_rounding_up(amount_in, fee_pips, FEE_DENOMINATOR - fee_pips)

    return sqrt_ratio_next_x96, amount_in, amount_out, fee_amount


# ---------------------------------------------------------------------------
# LiquidityMath / Tick
# ---------------------------------------------------------------------------


def add_delta(x: int, y: int) -> int:
    """``LiquidityMath.addDelta``: uint128 + int128 with ``LS`` (underflow) / ``LA`` (overflow) codes."""
    _check_uint(x, MAX_UINT128, "U128")
    _check_int(y, MIN_INT128, MAX_INT128, "I128")
    z = x + y
    if y < 0:
        if z < 0:
            raise ClMathError("LS")
    elif z > MAX_UINT128:
        raise ClMathError("LA")
    return z


def tick_spacing_to_max_liquidity_per_tick(tick_spacing: int) -> int:
    """``Tick.tickSpacingToMaxLiquidityPerTick``: uint128 max divided by the number of usable ticks.

    Solidity ``/`` truncates toward zero, which matters for the negative ``MIN_TICK`` term.
    """
    if tick_spacing <= 0:
        raise ClMathError("TS")
    min_tick = -((-MIN_TICK) // tick_spacing) * tick_spacing
    max_tick = (MAX_TICK // tick_spacing) * tick_spacing
    num_ticks = (max_tick - min_tick) // tick_spacing + 1
    return MAX_UINT128 // num_ticks
