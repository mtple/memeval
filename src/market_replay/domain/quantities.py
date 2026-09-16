"""Exact quantity handling.

Atomic token quantities are Python ``int`` values and are serialized as decimal
strings so they survive JSON, TypeScript and storage without losing precision.
Human-readable conversions use ``decimal.Decimal``; binary floats are never used
for balances, AMM arithmetic or fees.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal, getcontext
from fractions import Fraction

getcontext().prec = 80

Raw = int

_MAX_RAW = (1 << 256) - 1


class QuantityError(ValueError):
    """Raised for malformed or out-of-range raw quantities."""


def parse_raw(value: str | int) -> Raw:
    """Parse a raw quantity from a decimal string (or int) with strict validation."""
    if isinstance(value, bool):
        raise QuantityError("boolean is not a quantity")
    if isinstance(value, int):
        raw = value
    elif isinstance(value, str):
        s = value.strip()
        if not s or s.lstrip("-").isdigit() is False or s.count("-") > 1 or (s.startswith("-") and len(s) == 1):
            raise QuantityError(f"not a decimal integer string: {value!r}")
        if s.lstrip("-") != s.lstrip("-").lstrip("0") and s.lstrip("-") != "0":
            # leading zeros are tolerated but normalized
            pass
        raw = int(s)
    else:
        raise QuantityError(f"unsupported quantity type: {type(value).__name__}")
    if raw < -_MAX_RAW or raw > _MAX_RAW:
        raise QuantityError("quantity exceeds 256-bit range")
    return raw


def raw_str(value: Raw) -> str:
    """Serialize a raw quantity as a decimal string."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise QuantityError(f"raw quantity must be int, got {type(value).__name__}")
    return str(value)


def raw_to_decimal(value: Raw, decimals: int) -> Decimal:
    """Convert atomic units to a Decimal in whole-token units."""
    if decimals < 0 or decimals > 77:
        raise QuantityError(f"unsupported decimals: {decimals}")
    return Decimal(value).scaleb(-decimals)


def decimal_to_raw(value: Decimal | str, decimals: int) -> Raw:
    """Convert whole-token units to atomic units, rounding down (never up)."""
    d = Decimal(str(value))
    scaled = d.scaleb(decimals).quantize(Decimal(1), rounding=ROUND_DOWN)
    return int(scaled)


def format_units(value: Raw, decimals: int, places: int | None = None) -> str:
    """Human formatting of an atomic quantity in whole units with exact digits."""
    d = raw_to_decimal(value, decimals)
    if places is not None:
        d = d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_DOWN)
    s = format(d, "f")
    return s


def ratio(numerator: Raw, denominator: Raw) -> Fraction:
    """Exact rational ratio used for return calculations and recorded-output conventions."""
    if denominator == 0:
        raise QuantityError("division by zero")
    return Fraction(numerator, denominator)


def fraction_to_decimal_str(fr: Fraction, places: int = 12) -> str:
    """Render a Fraction as a decimal string with a fixed number of places (rounded down)."""
    d = Decimal(fr.numerator) / Decimal(fr.denominator)
    return format(d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_DOWN), "f")


def bps_of(value: Raw, bps: int) -> Raw:
    """Floor of ``value * bps / 10_000``."""
    return (value * bps) // 10_000
