"""Mutable concentrated-liquidity (Uniswap v3 / v4) pool state with exact integer swap math.

The state is the subset of ``UniswapV3Pool`` storage that the swap and liquidity paths
read or write: ``slot0`` (sqrt price, tick), active ``liquidity`` and the per-tick
``liquidityNet``/``liquidityGross`` table. Fee growth, oracle observations, positions
and protocol fees are not modelled: they never influence the amounts a swap moves or the
price it ends at (protocol fee is treated as 0).

Conventions follow v3: ``amount_specified > 0`` is exact input, ``< 0`` exact output; the
returned ``(amount0, amount1)`` are pool deltas (positive = paid into the pool, negative
= sent out). v4 pools (``model == "uniswap_v4_cl"``) use the same tick, sqrt-price and
step formulas; v4's inverted ``amountSpecified`` sign and its ``BalanceDelta`` sign
convention are the caller's job to translate when it reads v4 events. v4 dynamic fees are
supported by passing ``fee_pips`` per swap.
"""

from __future__ import annotations

import hashlib
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from fractions import Fraction

from .math import (
    MAX_INT128,
    MAX_SQRT_RATIO,
    MAX_TICK,
    MAX_UINT128,
    MIN_INT128,
    MIN_SQRT_RATIO,
    MIN_TICK,
    Q96,
    ClMathError,
    add_delta,
    compute_swap_step,
    get_amount0_delta_signed,
    get_amount1_delta_signed,
    get_sqrt_ratio_at_tick,
    get_tick_at_sqrt_ratio,
    tick_spacing_to_max_liquidity_per_tick,
)

MODEL_UNISWAP_V3_CL = "uniswap_v3_cl"
MODEL_UNISWAP_V4_CL = "uniswap_v4_cl"
SUPPORTED_CLMM_MODELS = frozenset({MODEL_UNISWAP_V3_CL, MODEL_UNISWAP_V4_CL})

_MAX_FEE_PIPS = 1_000_000


class UnsupportedMechanics(ValueError):
    pass


@dataclass(slots=True, frozen=True)
class ClSwapQuote:
    """Result of an exact-input swap simulated on a copy of the pool (nothing was mutated)."""

    asset_in: str
    asset_out: str
    amount_in: int
    amount_out: int
    sqrt_price_after_x96: int
    tick_after: int
    liquidity_after: int
    fee_pips: int


@dataclass(slots=True)
class ClPoolState:
    key: str
    asset0: str
    asset1: str
    fee_pips: int
    tick_spacing: int
    sqrt_price_x96: int
    tick: int
    liquidity: int = 0
    # initialized tick -> (liquidity_net, liquidity_gross); entries with gross == 0 are absent
    ticks: dict[int, tuple[int, int]] = field(default_factory=dict)
    model: str = MODEL_UNISWAP_V3_CL
    halted: bool = False
    # Per-asset fixture restrictions (e.g. sell blocked) applied by tape events; key -> dict
    restrictions: dict[str, dict] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.model not in SUPPORTED_CLMM_MODELS:
            raise UnsupportedMechanics(f"pool model {self.model} is not supported by the CLMM adapter")
        if self.tick_spacing <= 0:
            raise ClMathError("TS")
        if not (0 <= self.fee_pips < _MAX_FEE_PIPS):
            raise ClMathError("F")
        if not (MIN_SQRT_RATIO <= self.sqrt_price_x96 < MAX_SQRT_RATIO):
            raise ClMathError("R")
        if not (MIN_TICK <= self.tick <= MAX_TICK):
            raise ClMathError("T")
        if not (0 <= self.liquidity <= MAX_UINT128):
            raise ClMathError("U128")
        for t, (net, gross) in self.ticks.items():
            if t % self.tick_spacing != 0 or not (MIN_TICK <= t <= MAX_TICK):
                raise ClMathError("TS")
            if gross <= 0 or gross > MAX_UINT128 or not (MIN_INT128 <= net <= MAX_INT128):
                raise ClMathError("TICK")

    @classmethod
    def initialize(
        cls,
        key: str,
        asset0: str,
        asset1: str,
        fee_pips: int,
        tick_spacing: int,
        sqrt_price_x96: int,
        model: str = MODEL_UNISWAP_V3_CL,
    ) -> ClPoolState:
        """``UniswapV3Pool.initialize``: the starting tick is ``getTickAtSqrtRatio(sqrtPriceX96)``."""
        return cls(
            key=key,
            asset0=asset0,
            asset1=asset1,
            fee_pips=fee_pips,
            tick_spacing=tick_spacing,
            sqrt_price_x96=sqrt_price_x96,
            tick=get_tick_at_sqrt_ratio(sqrt_price_x96),
            model=model,
        )

    def copy(self) -> ClPoolState:
        return ClPoolState(
            key=self.key,
            asset0=self.asset0,
            asset1=self.asset1,
            fee_pips=self.fee_pips,
            tick_spacing=self.tick_spacing,
            sqrt_price_x96=self.sqrt_price_x96,
            tick=self.tick,
            liquidity=self.liquidity,
            ticks=dict(self.ticks),
            model=self.model,
            halted=self.halted,
            restrictions={k: dict(v) for k, v in self.restrictions.items()},
        )

    # ------------------------------------------------------------------ assets

    def other(self, asset: str) -> str:
        if asset == self.asset0:
            return self.asset1
        if asset == self.asset1:
            return self.asset0
        raise ClMathError("ASSET_NOT_IN_POOL")

    # ------------------------------------------------------ common pool surface

    @property
    def fee_num(self) -> int:
        """Fee expressed the CPMM way: the share of input kept after the fee is ``(1e6 - fee_pips) / 1e6``."""
        return _MAX_FEE_PIPS - self.fee_pips

    @property
    def fee_den(self) -> int:
        return _MAX_FEE_PIPS

    def virtual_reserves(self) -> tuple[int, int]:
        """Virtual reserves of the active range at the current price, in raw units.

        From the v3 whitepaper (x_virtual = L / sqrt(P), y_virtual = L * sqrt(P)) with the
        Q64.96 sqrt price: ``x = L * 2^96 // sqrtP``, ``y = L * sqrtP // 2^96``. Both are zero
        when no liquidity is active. They are the depth the constant-product view of the
        current tick range would show; they are not token balances of the pool.
        """
        if self.liquidity == 0 or self.sqrt_price_x96 == 0:
            return 0, 0
        return (self.liquidity * Q96) // self.sqrt_price_x96, (self.liquidity * self.sqrt_price_x96) // Q96

    def depth_for(self, asset_in: str) -> tuple[int, int]:
        """``(depth_in, depth_out)``: the virtual reserves ordered by the input asset."""
        x, y = self.virtual_reserves()
        if asset_in == self.asset0:
            return x, y
        if asset_in == self.asset1:
            return y, x
        raise ClMathError("ASSET_NOT_IN_POOL")

    def ticks_digest(self) -> str:
        """Stable digest of the initialized tick table (sorted ``tick:net:gross`` lines)."""
        h = hashlib.sha256()
        for t in sorted(self.ticks):
            net, gross = self.ticks[t]
            h.update(f"{t}:{net}:{gross}\n".encode())
        return h.hexdigest()

    def ticks_near(self, count: int) -> list[tuple[int, int, int]]:
        """Up to ``count`` initialized ticks on each side of the current tick as ``(tick, net, gross)``, ascending."""
        below = sorted(t for t in self.ticks if t <= self.tick)[-count:] if count > 0 else []
        above = sorted(t for t in self.ticks if t > self.tick)[:count] if count > 0 else []
        return [(t, self.ticks[t][0], self.ticks[t][1]) for t in below + above]

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

    def spot_price_fraction(self, base_asset: str) -> Fraction | None:
        """Indicative marginal price of ``base_asset`` in the other asset: ``(sqrtP / 2^96)^2``. Not executable."""
        if self.sqrt_price_x96 == 0:
            return None
        price0 = Fraction(self.sqrt_price_x96 * self.sqrt_price_x96, Q96 * Q96)
        if base_asset == self.asset0:
            return price0
        if base_asset == self.asset1:
            return 1 / price0
        raise ClMathError("ASSET_NOT_IN_POOL")

    # --------------------------------------------------------------- liquidity

    def apply_modify_liquidity(
        self, tick_lower: int, tick_upper: int, liquidity_delta: int
    ) -> tuple[int, int]:
        """``UniswapV3Pool._modifyPosition`` + ``Tick.update`` for one position change.

        Returns ``(amount0, amount1)`` as pool deltas: positive amounts are owed to the pool
        (mint, rounded up), negative amounts are returned to the owner (burn, rounded down).
        Tick entries whose gross liquidity reaches zero are removed (``Tick.clear``). The
        active ``liquidity`` changes only when the current tick lies inside the range.
        All checks run before anything is mutated, so a failing call leaves the state intact.
        """
        if self.halted:
            raise ClMathError("POOL_HALTED")
        # checkTicks
        if tick_lower >= tick_upper:
            raise ClMathError("TLU")
        if tick_lower < MIN_TICK:
            raise ClMathError("TLM")
        if tick_upper > MAX_TICK:
            raise ClMathError("TUM")
        # TickBitmap.flipTick requires ticks on the spacing grid
        if tick_lower % self.tick_spacing != 0 or tick_upper % self.tick_spacing != 0:
            raise ClMathError("TS")
        if not (MIN_INT128 <= liquidity_delta <= MAX_INT128):
            raise ClMathError("I128")
        if liquidity_delta == 0:
            return 0, 0

        max_liquidity = tick_spacing_to_max_liquidity_per_tick(self.tick_spacing)
        lower_entry = self._updated_tick(
            tick_lower, liquidity_delta, upper=False, max_liquidity=max_liquidity
        )
        upper_entry = self._updated_tick(tick_upper, liquidity_delta, upper=True, max_liquidity=max_liquidity)

        amount0 = 0
        amount1 = 0
        liquidity_after = self.liquidity
        sqrt_lower = get_sqrt_ratio_at_tick(tick_lower)
        sqrt_upper = get_sqrt_ratio_at_tick(tick_upper)
        if self.tick < tick_lower:
            amount0 = get_amount0_delta_signed(sqrt_lower, sqrt_upper, liquidity_delta)
        elif self.tick < tick_upper:
            amount0 = get_amount0_delta_signed(self.sqrt_price_x96, sqrt_upper, liquidity_delta)
            amount1 = get_amount1_delta_signed(sqrt_lower, self.sqrt_price_x96, liquidity_delta)
            liquidity_after = add_delta(self.liquidity, liquidity_delta)
        else:
            amount1 = get_amount1_delta_signed(sqrt_lower, sqrt_upper, liquidity_delta)

        # commit
        for t, entry in ((tick_lower, lower_entry), (tick_upper, upper_entry)):
            if entry is None:
                self.ticks.pop(t, None)
            else:
                self.ticks[t] = entry
        self.liquidity = liquidity_after
        return amount0, amount1

    def _updated_tick(
        self, tick: int, liquidity_delta: int, *, upper: bool, max_liquidity: int
    ) -> tuple[int, int] | None:
        """``Tick.update`` without side effects: the new ``(net, gross)`` entry, or None when cleared."""
        net_before, gross_before = self.ticks.get(tick, (0, 0))
        gross_after = add_delta(gross_before, liquidity_delta)
        if gross_after > max_liquidity:
            raise ClMathError("LO")
        # when the lower (upper) tick is crossed left to right (right to left), liquidity is added (removed)
        net_after = net_before - liquidity_delta if upper else net_before + liquidity_delta
        if not (MIN_INT128 <= net_after <= MAX_INT128):
            raise ClMathError("I128")
        if gross_after == 0:
            return None
        return net_after, gross_after

    # -------------------------------------------------------------------- swaps

    def _next_initialized_tick_within_one_word(
        self, tick: int, zero_for_one: bool, sorted_ticks: list[int]
    ) -> tuple[int, bool]:
        """``TickBitmap.nextInitializedTickWithinOneWord`` evaluated on the sorted tick keys.

        The on-chain bitmap searches one 256-bit word at a time and, when the word holds no
        initialized tick, returns the word's boundary as an *uninitialized* step target. The
        swap loop then runs ``computeSwapStep`` to that boundary and continues from it. Because
        every step rounds its own amounts and fee, a swap that spans a word boundary inside
        one liquidity range is not always bit-identical to a single unsplit step, so the
        boundary segmentation is reproduced here rather than searching the keys directly.
        The word arithmetic: ``compressed = floor(tick / spacing)``, ``wordPos = compressed >> 8``,
        ``bitPos = compressed mod 256`` (two's complement, as ``uint8(int24 % 256)`` yields).
        """
        spacing = self.tick_spacing
        compressed = tick // spacing  # floor division, matching the Solidity round-toward-negative fix-up
        if zero_for_one:
            bit_pos = compressed % 256
            word_start = (compressed - bit_pos) * spacing  # lowest tick of this word
            # greatest initialized tick <= tick (in ticks, i.e. compressed * spacing) within the word
            idx = bisect_right(sorted_ticks, compressed * spacing) - 1
            if idx >= 0 and sorted_ticks[idx] >= word_start:
                return sorted_ticks[idx], True
            return word_start, False
        compressed += 1
        bit_pos = compressed % 256
        word_end = (compressed + (255 - bit_pos)) * spacing  # highest tick of this word
        # smallest initialized tick > tick, i.e. >= (compressed) * spacing, within the word
        idx = bisect_left(sorted_ticks, compressed * spacing)
        if idx < len(sorted_ticks) and sorted_ticks[idx] <= word_end:
            return sorted_ticks[idx], True
        return word_end, False

    def swap(
        self,
        zero_for_one: bool,
        amount_specified: int,
        sqrt_price_limit_x96: int | None = None,
        fee_pips: int | None = None,
    ) -> tuple[int, int, int, int, int]:
        """``UniswapV3Pool.swap`` with protocol fee 0 and no fee-growth bookkeeping.

        ``amount_specified > 0`` is exact input, ``< 0`` exact output. ``sqrt_price_limit_x96``
        defaults to the widest legal limit (``MIN_SQRT_RATIO + 1`` / ``MAX_SQRT_RATIO - 1``).
        ``fee_pips`` overrides the pool fee for this swap only (v4 dynamic fees).
        Returns ``(amount0, amount1, sqrt_price_after_x96, liquidity_after, tick_after)`` as the
        ``Swap`` event would log them, and commits that state. Raises ``ClMathError`` (``AS``,
        ``SPL``, ``F``, ``POOL_HALTED``) before mutating anything; the loop itself cannot fail
        part-way on a consistent state.
        """
        if self.halted:
            raise ClMathError("POOL_HALTED")
        if amount_specified == 0:
            raise ClMathError("AS")
        fee = self.fee_pips if fee_pips is None else fee_pips
        if not (0 <= fee < _MAX_FEE_PIPS):
            raise ClMathError("F")
        if sqrt_price_limit_x96 is None:
            sqrt_price_limit_x96 = MIN_SQRT_RATIO + 1 if zero_for_one else MAX_SQRT_RATIO - 1
        if zero_for_one:
            if not (MIN_SQRT_RATIO < sqrt_price_limit_x96 < self.sqrt_price_x96):
                raise ClMathError("SPL")
        elif not (self.sqrt_price_x96 < sqrt_price_limit_x96 < MAX_SQRT_RATIO):
            raise ClMathError("SPL")

        exact_input = amount_specified > 0
        amount_remaining = amount_specified
        amount_calculated = 0
        sqrt_price = self.sqrt_price_x96
        tick = self.tick
        liquidity = self.liquidity
        sorted_ticks = sorted(self.ticks)

        while amount_remaining != 0 and sqrt_price != sqrt_price_limit_x96:
            sqrt_price_start = sqrt_price
            tick_next, initialized = self._next_initialized_tick_within_one_word(
                tick, zero_for_one, sorted_ticks
            )
            # the bitmap is not aware of the tick bounds
            if tick_next < MIN_TICK:
                tick_next = MIN_TICK
            elif tick_next > MAX_TICK:
                tick_next = MAX_TICK
            sqrt_price_next = get_sqrt_ratio_at_tick(tick_next)

            if zero_for_one:
                target = sqrt_price_limit_x96 if sqrt_price_next < sqrt_price_limit_x96 else sqrt_price_next
            else:
                target = sqrt_price_limit_x96 if sqrt_price_next > sqrt_price_limit_x96 else sqrt_price_next

            sqrt_price, step_in, step_out, step_fee = compute_swap_step(
                sqrt_price, target, liquidity, amount_remaining, fee
            )

            if exact_input:
                amount_remaining -= step_in + step_fee
                amount_calculated -= step_out
            else:
                amount_remaining += step_out
                amount_calculated += step_in + step_fee

            if sqrt_price == sqrt_price_next:
                if initialized:
                    liquidity_net = self.ticks[tick_next][0]
                    # moving leftward, liquidityNet is interpreted with the opposite sign
                    if zero_for_one:
                        liquidity_net = -liquidity_net
                    liquidity = add_delta(liquidity, liquidity_net)
                tick = tick_next - 1 if zero_for_one else tick_next
            elif sqrt_price != sqrt_price_start:
                # recompute unless we are on a lower tick boundary (already transitioned) and have not moved
                tick = get_tick_at_sqrt_ratio(sqrt_price)

        if zero_for_one == exact_input:
            amount0, amount1 = amount_specified - amount_remaining, amount_calculated
        else:
            amount0, amount1 = amount_calculated, amount_specified - amount_remaining

        self.sqrt_price_x96 = sqrt_price
        self.tick = tick
        self.liquidity = liquidity
        return amount0, amount1, sqrt_price, liquidity, tick

    def apply_recorded_swap(
        self,
        amount0: int,
        amount1: int,
        sqrt_after: int,
        liquidity_after: int,
        tick_after: int,
        fee_pips: int | None = None,
    ) -> dict[str, int | str]:
        """Replay a recorded ``Swap`` event and reconcile the model against what the chain logged.

        Amounts use the v3 event convention (positive = paid into the pool). The direction is
        the sign of the amounts; the input is the positive one. A ``Swap`` event does not say
        whether the trade was exact-input or exact-output, so the recorded input is first
        replayed as exact input on a copy; if that does not reproduce the event exactly, the
        recorded output is replayed as exact output on another copy. The first variant that
        reproduces every field is committed. When neither does, the exact-input replay is
        committed (it is what a recorded input amount deterministically implies) and the
        returned dict carries the differences ``model - recorded`` for ``amount0``, ``amount1``,
        ``sqrt_price_x96``, ``liquidity`` and ``tick`` (all zero on an exact match), plus
        ``mode`` (``"exact_in"`` or ``"exact_out"``). The state is never adjusted toward the
        recording; the caller decides what a mismatch means.
        """
        if amount0 > 0 and amount1 <= 0:
            zero_for_one = True
        elif amount1 > 0 and amount0 <= 0:
            zero_for_one = False
        else:
            # Not a swap the v3 loop can replay (both legs paid in, both paid out, or nothing moved):
            # a hook took the whole leg (launch auctions, fee modules) or the event is a no-op. The
            # chain is the truth: anchor to the recorded after-state and report how far the model was.
            d_sqrt, d_liq, d_tick = self.anchor_to_recorded(sqrt_after, liquidity_after, tick_after)
            return {"mode": "anchored_degenerate", "amount0": 0, "amount1": 0, "sqrt_price_x96": d_sqrt, "liquidity": d_liq, "tick": d_tick}
        amount_in = amount0 if zero_for_one else amount1
        amount_out = -(amount1 if zero_for_one else amount0)

        recorded = (amount0, amount1, sqrt_after, liquidity_after, tick_after)
        candidates: list[tuple[str, int]] = [("exact_in", amount_in)]
        if amount_out > 0:
            candidates.append(("exact_out", -amount_out))

        first: tuple[str, ClPoolState, tuple[int, int, int, int, int]] | None = None
        for mode, specified in candidates:
            trial = self.copy()
            result = trial.swap(zero_for_one, specified, None, fee_pips)
            if first is None:
                first = (mode, trial, result)
            if result == recorded:
                first = (mode, trial, result)
                break
        assert first is not None
        mode, trial, result = first
        self.sqrt_price_x96 = trial.sqrt_price_x96
        self.tick = trial.tick
        self.liquidity = trial.liquidity
        return {
            "mode": mode,
            "amount0": result[0] - amount0,
            "amount1": result[1] - amount1,
            "sqrt_price_x96": result[2] - sqrt_after,
            "liquidity": result[3] - liquidity_after,
            "tick": result[4] - tick_after,
        }

    def anchor_to_recorded(self, sqrt_after: int, liquidity_after: int, tick_after: int) -> tuple[int, int, int]:
        """Set price, liquidity and tick to a recorded after-state (the chain is the truth). Returns the
        deltas ``model - recorded`` the state was off by, so a caller can classify the correction and apply
        the same one to any diverged copy."""
        if not (MIN_SQRT_RATIO <= sqrt_after < MAX_SQRT_RATIO) or liquidity_after < 0 or liquidity_after > MAX_UINT128:
            raise ClMathError("R")
        d = (self.sqrt_price_x96 - sqrt_after, self.liquidity - liquidity_after, self.tick - tick_after)
        self.sqrt_price_x96, self.liquidity, self.tick = sqrt_after, liquidity_after, tick_after
        return d

    def apply_state_delta(self, d_sqrt: int, d_liq: int) -> None:
        """Apply a correction (a price and liquidity delta) to this copy of the state, as when the
        reference was anchored to the chain: the private copy moves by the same amount so one unseen
        event never compounds. Raises ClMathError if the result leaves the valid range."""
        sqrt = self.sqrt_price_x96 + d_sqrt
        liq = self.liquidity + d_liq
        if not (MIN_SQRT_RATIO <= sqrt < MAX_SQRT_RATIO) or liq < 0 or liq > MAX_UINT128:
            raise ClMathError("R")
        self.sqrt_price_x96 = sqrt
        self.liquidity = liq
        self.tick = get_tick_at_sqrt_ratio(sqrt)

    # ------------------------------------------------------------------- broker

    def quote(self, asset_in: str, amount_in: int) -> ClSwapQuote:
        """Exact-input swap simulated on a copy; the pool is not mutated."""
        if amount_in <= 0:
            raise ClMathError("INSUFFICIENT_INPUT_AMOUNT")
        asset_out = self.other(asset_in)
        zero_for_one = asset_in == self.asset0
        trial = self.copy()
        amount0, amount1, sqrt_after, liquidity_after, tick_after = trial.swap(zero_for_one, amount_in)
        amount_out = -(amount1 if zero_for_one else amount0)
        return ClSwapQuote(
            asset_in=asset_in,
            asset_out=asset_out,
            amount_in=amount0 if zero_for_one else amount1,
            amount_out=amount_out,
            sqrt_price_after_x96=sqrt_after,
            tick_after=tick_after,
            liquidity_after=liquidity_after,
            fee_pips=self.fee_pips,
        )

    def max_out(self, asset_in: str, amount_in: int) -> int:
        """Largest output an exact-input swap of ``amount_in`` yields on the current state."""
        return self.quote(asset_in, amount_in).amount_out

    def apply_swap(self, asset_in: str, amount_in: int, output_ratio: Fraction | None = None) -> int:
        """Apply an exact-input swap and return ``amount_out``; the swap must produce output.

        ``output_ratio`` exists only so the call shape matches ``CpmmPoolState.apply_swap``.
        A concentrated-liquidity swap has no free output parameter (the path is fixed by the
        tick map), so any ratio other than 1 is refused with ``OUTPUT_RATIO_UNSUPPORTED``;
        external flow is replayed through :meth:`apply_intent` instead.
        """
        if output_ratio is not None and output_ratio != 1:
            raise ClMathError("OUTPUT_RATIO_UNSUPPORTED")
        if amount_in <= 0:
            raise ClMathError("INSUFFICIENT_INPUT_AMOUNT")
        zero_for_one = asset_in == self.asset0
        if not zero_for_one and asset_in != self.asset1:
            raise ClMathError("ASSET_NOT_IN_POOL")
        trial = self.copy()
        amount0, amount1, _, _, _ = trial.swap(zero_for_one, amount_in)
        out = -(amount1 if zero_for_one else amount0)
        if out <= 0:
            raise ClMathError("INSUFFICIENT_OUTPUT_AMOUNT")
        self.sqrt_price_x96 = trial.sqrt_price_x96
        self.tick = trial.tick
        self.liquidity = trial.liquidity
        return out

    def apply_intent(self, zero_for_one: bool, amount_specified: int, fee_pips: int | None = None) -> tuple[int, int]:
        """Re-execute a recorded swap intent on this state and return ``(amount_in, amount_out)``.

        ``amount_specified`` follows ``swap``: positive exact input, negative exact output.
        ``fee_pips`` is the fee the recorded event reported (v4 dynamic fees). The swap is
        run on a copy and committed only when both legs are non-zero; otherwise the state is
        untouched and ``INSUFFICIENT_INPUT_AMOUNT`` / ``INSUFFICIENT_OUTPUT_AMOUNT`` is raised,
        the same codes the CPMM adapter uses for an external swap it cannot honour.
        """
        trial = self.copy()
        amount0, amount1, _, _, _ = trial.swap(zero_for_one, amount_specified, None, fee_pips)
        amount_in = amount0 if zero_for_one else amount1
        amount_out = -(amount1 if zero_for_one else amount0)
        if amount_in <= 0:
            raise ClMathError("INSUFFICIENT_INPUT_AMOUNT")
        if amount_out <= 0:
            raise ClMathError("INSUFFICIENT_OUTPUT_AMOUNT")
        self.sqrt_price_x96 = trial.sqrt_price_x96
        self.tick = trial.tick
        self.liquidity = trial.liquidity
        return amount_in, amount_out
