"""Mutable CPMM pool state under the ``cpmm_fixed_flow_v1`` convention.

External (tape) swaps keep their recorded input direction and amount. Their output
is recomputed against this state. If the recorded output was below the formula's
maximum on the reference state, the recorded output/max-output ratio is preserved
(explicit external-order convention). Liquidity additions/removals are fixed
token transfers; a removal that would overdraw reserves raises ``FidelityLimit``
instead of producing negative reserves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction

from ...domain.status import SUPPORTED_CPMM_MODELS, PoolModel
from .math import CpmmMathError, get_amount_out


class UnsupportedMechanics(ValueError):
    pass


class FidelityLimit(RuntimeError):
    pass


@dataclass(slots=True)
class CpmmPoolState:
    key: str
    asset0: str
    asset1: str
    reserve0: int
    reserve1: int
    fee_num: int = 997
    fee_den: int = 1000
    model: PoolModel = PoolModel.UNISWAP_V2_PLAIN
    halted: bool = False
    # Per-asset fixture restrictions (e.g. sell blocked) applied by tape events; key -> dict
    restrictions: dict[str, dict] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.model not in SUPPORTED_CPMM_MODELS:
            raise UnsupportedMechanics(f"pool model {self.model} is not supported by the CPMM adapter")
        if self.reserve0 < 0 or self.reserve1 < 0:
            raise ValueError("negative reserves")

    def copy(self) -> CpmmPoolState:
        return CpmmPoolState(
            key=self.key,
            asset0=self.asset0,
            asset1=self.asset1,
            reserve0=self.reserve0,
            reserve1=self.reserve1,
            fee_num=self.fee_num,
            fee_den=self.fee_den,
            model=self.model,
            halted=self.halted,
            restrictions={k: dict(v) for k, v in self.restrictions.items()},
        )

    def reserves_for(self, asset_in: str) -> tuple[int, int]:
        if asset_in == self.asset0:
            return self.reserve0, self.reserve1
        if asset_in == self.asset1:
            return self.reserve1, self.reserve0
        raise CpmmMathError("ASSET_NOT_IN_POOL")

    def depth_for(self, asset_in: str) -> tuple[int, int]:
        """``(depth_in, depth_out)``: for a CPMM the depth is the reserves themselves (common pool surface)."""
        return self.reserves_for(asset_in)

    def other(self, asset: str) -> str:
        if asset == self.asset0:
            return self.asset1
        if asset == self.asset1:
            return self.asset0
        raise CpmmMathError("ASSET_NOT_IN_POOL")

    def max_out(self, asset_in: str, amount_in: int) -> int:
        rin, rout = self.reserves_for(asset_in)
        return get_amount_out(amount_in, rin, rout, self.fee_num, self.fee_den)

    def transfer_blocked(self, asset: str, direction: str) -> str | None:
        """Return a reason if the fixture token rules block this transfer direction ('sell' or 'buy')."""
        r = self.restrictions.get(asset)
        if not r:
            return None
        if direction == "sell" and r.get("sell_blocked"):
            return "FIXTURE_SELL_BLOCKED"
        if direction == "buy" and r.get("buy_blocked"):
            return "FIXTURE_BUY_BLOCKED"
        return None

    def apply_swap(self, asset_in: str, amount_in: int, output_ratio: Fraction | None = None) -> int:
        """Apply an exact-input swap. Returns amount_out. Raises CpmmMathError on invalid input."""
        if self.halted:
            raise CpmmMathError("POOL_HALTED")
        out = self.max_out(asset_in, amount_in)
        if output_ratio is not None and output_ratio != 1:
            out = (out * output_ratio.numerator) // output_ratio.denominator
        if out <= 0:
            raise CpmmMathError("INSUFFICIENT_OUTPUT_AMOUNT")
        if asset_in == self.asset0:
            if out >= self.reserve1:
                raise CpmmMathError("INSUFFICIENT_LIQUIDITY")
            self.reserve0 += amount_in
            self.reserve1 -= out
        else:
            if out >= self.reserve0:
                raise CpmmMathError("INSUFFICIENT_LIQUIDITY")
            self.reserve1 += amount_in
            self.reserve0 -= out
        return out

    def apply_mint(self, amount0: int, amount1: int) -> None:
        if amount0 < 0 or amount1 < 0:
            raise ValueError("negative mint")
        self.reserve0 += amount0
        self.reserve1 += amount1

    def apply_burn(self, amount0: int, amount1: int) -> None:
        if amount0 < 0 or amount1 < 0:
            raise ValueError("negative burn")
        if amount0 > self.reserve0 or amount1 > self.reserve1:
            raise FidelityLimit(
                f"counterfactual liquidity removal would overdraw reserves of {self.key}: "
                f"burn ({amount0},{amount1}) vs reserves ({self.reserve0},{self.reserve1})"
            )
        self.reserve0 -= amount0
        self.reserve1 -= amount1

    def reconcile_sync(self, reserve0: int, reserve1: int) -> tuple[int, int]:
        """Return (delta0, delta1) between this state and a recorded Sync checkpoint. Never mutates."""
        return (self.reserve0 - reserve0, self.reserve1 - reserve1)

    def spot_price_fraction(self, base_asset: str) -> Fraction | None:
        """Indicative marginal price of ``base_asset`` in the other asset (reserve ratio). Not executable."""
        rin, rout = self.reserves_for(base_asset)
        if rin == 0:
            return None
        return Fraction(rout, rin)
