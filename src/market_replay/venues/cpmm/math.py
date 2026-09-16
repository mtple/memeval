"""Constant-product exact-input arithmetic in integer units.

Mirrors the Uniswap v2 periphery library ``getAmountOut``:

    out = floor((a * f_n * y) / (x * f_d + a * f_n))

with ``f_n/f_d`` the retained fraction after the pool fee (997/1000 for the canonical
0.3% pool). The fee is embedded in the output; it is never charged a second time.
"""

from __future__ import annotations


class CpmmMathError(ValueError):
    pass


def get_amount_out(amount_in: int, reserve_in: int, reserve_out: int, fee_num: int = 997, fee_den: int = 1000) -> int:
    if amount_in <= 0:
        raise CpmmMathError("INSUFFICIENT_INPUT_AMOUNT")
    if reserve_in <= 0 or reserve_out <= 0:
        raise CpmmMathError("INSUFFICIENT_LIQUIDITY")
    if not (0 < fee_num <= fee_den):
        raise CpmmMathError("INVALID_FEE")
    amount_in_with_fee = amount_in * fee_num
    numerator = amount_in_with_fee * reserve_out
    denominator = reserve_in * fee_den + amount_in_with_fee
    return numerator // denominator


def get_amount_in(amount_out: int, reserve_in: int, reserve_out: int, fee_num: int = 997, fee_den: int = 1000) -> int:
    """Reference inverse (not exposed to agents: exact-output swaps are unsupported in v1)."""
    if amount_out <= 0:
        raise CpmmMathError("INSUFFICIENT_OUTPUT_AMOUNT")
    if reserve_in <= 0 or reserve_out <= 0 or amount_out >= reserve_out:
        raise CpmmMathError("INSUFFICIENT_LIQUIDITY")
    numerator = reserve_in * amount_out * fee_den
    denominator = (reserve_out - amount_out) * fee_num
    return numerator // denominator + 1
