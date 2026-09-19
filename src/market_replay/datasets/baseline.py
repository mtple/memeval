"""What the market itself did on a recorded day, so a result can be read against it.

The baseline is a naive rule an agent can compare with, not a strategy: put a small fixed stake
(0.01 of the numeraire) into every pool that launched that day right after its first recorded swap,
hold to the end of the day, then sell back into the pool as it stands at the close. The same rule
is applied to the established pools (those already trading before the day). It pays no gas and
waits for nothing, so a real agent following it would do worse.

The sale is valued the way the pool would actually pay it, not at a mark price: a v2 pool pays
constant-product output from its closing reserves (a pulled pool pays dust, a pool someone bought
empty cannot pay more numeraire than it holds); a concentrated pool pays at its closing price
capped by the numeraire its active liquidity holds. Prices and reserves come from the tape alone.
Nothing here touches the network or the clock.

The result is written next to the pack as ``market_baseline.json``; the file is not one of the
pack's hashed objects, so adding or regenerating it does not change the pack id.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

from .pack import Pack

BASELINE_FILE = "market_baseline.json"
BASELINE_BASIS = "naive_fixed_stake_first_swap_sell_at_close_v2"
STAKE_DECIMALS_BELOW_UNIT = 2  # 0.01 of the numeraire per pool
Q96 = 1 << 96
DRAINED_SHARE = Fraction(
    1, 100
)  # a v2 pool whose numeraire reserve ends under 1% of its peak has been pulled

CAVEATS = [
    "Pays no gas. On a day with thousands of launches the gas alone would exceed the stakes, so an agent doing the same trades would do far worse.",
    "The buy is placed right after the first recorded swap, which nobody can do without seeing the launch first. It is a reference point, not an achievable trade.",
    "A concentrated pool is sold at its closing price, capped by the numeraire its active liquidity holds; that cap is an upper bound, so the value may be optimistic.",
    "Pools with no recorded swap in the day are left out, so the basket is the pools that traded at least once.",
    "Not a prediction and not a benchmark strategy: it says what a fixed small stake in everything would have seen that day.",
]


@dataclass(slots=True)
class _PoolTrack:
    numeraire_is_0: bool
    is_launch: bool
    cl: bool
    first: Fraction | None = None
    last: Fraction | None = None
    swaps: int = 0
    fee_num: int = 997
    fee_den: int = 1000
    entry_reserves: tuple[int, int] | None = None  # v2: (numeraire, token) right after the first swap
    exit_reserves: tuple[int, int] | None = None  # v2: at the close
    cl_liquidity_after: int = 0  # concentrated: active liquidity reported by the last swap
    cl_sqrt_after: int = 0
    peak_depth: int = 0  # v2: numeraire reserve
    depth: int = 0
    cl_liquidity: int = 0  # concentrated: net liquidity from the initial ticks plus every modify
    pending_swap: bool = False  # v2: a swap row was seen, price arrives with the sync that follows
    seen_modify: bool = False
    initial_liquidity_known: bool = field(default=False)

    def price_from_reserves(self, r0: int, r1: int) -> Fraction | None:
        num, tok = (r0, r1) if self.numeraire_is_0 else (r1, r0)
        if tok <= 0 or num <= 0:
            return None
        return Fraction(num, tok)

    def price_from_sqrt(self, sqrt_p: int) -> Fraction | None:
        if sqrt_p <= 0:
            return None
        p1_per_0 = Fraction(sqrt_p * sqrt_p, Q96 * Q96)  # token1 per token0
        return 1 / p1_per_0 if self.numeraire_is_0 else p1_per_0  # numeraire per token

    def drained(self) -> bool:
        if self.cl:
            return self.seen_modify and self.cl_liquidity <= 0
        return self.peak_depth > 0 and Fraction(self.depth, self.peak_depth) < DRAINED_SHARE

    def ret(self, stake: int) -> Fraction | None:
        """Return of ``stake`` numeraire put in after the first swap and sold back at the close."""
        if self.first is None or self.last is None or self.first <= 0 or stake <= 0:
            return None
        if not self.cl:
            if self.entry_reserves is None or self.exit_reserves is None:
                return None
            n_in, t_in = self.entry_reserves
            tokens = _cpmm_out(stake, n_in, t_in, self.fee_num, self.fee_den)
            n_out, t_out = self.exit_reserves
            value = _cpmm_out(tokens, t_out, n_out, self.fee_num, self.fee_den) if tokens > 0 else Fraction(0)
            return value / stake - 1
        if self.drained():
            return Fraction(-1)  # every position was burned after the last swap; nothing is left to sell into
        fee = Fraction(self.fee_den - self.fee_num, self.fee_den)
        tokens = Fraction(stake) * (1 - fee) / self.first
        value = tokens * self.last * (1 - fee)
        if self.cl_liquidity_after <= 0 or self.cl_sqrt_after <= 0:
            cap = Fraction(0)
        elif self.numeraire_is_0:
            cap = Fraction(self.cl_liquidity_after * Q96, self.cl_sqrt_after)
        else:
            cap = Fraction(self.cl_liquidity_after * self.cl_sqrt_after, Q96)
        return min(value, cap) / stake - 1


def _cpmm_out(
    amount_in: int | Fraction, reserve_in: int, reserve_out: int, fee_num: int, fee_den: int
) -> Fraction:
    if amount_in <= 0 or reserve_in <= 0 or reserve_out <= 0:
        return Fraction(0)
    with_fee = Fraction(amount_in) * fee_num
    return Fraction(with_fee * reserve_out) / (Fraction(reserve_in) * fee_den + with_fee)


def _fmt(x: Fraction | float | None) -> str | None:
    return None if x is None else f"{float(x):.6f}"


def _percentile(sorted_vals: list[Fraction], q: float) -> Fraction | None:
    if not sorted_vals:
        return None
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = Fraction(pos - lo).limit_denominator(1000)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


def _basket(tracks: list[_PoolTrack], stake: int) -> dict[str, Any]:
    valued = [(t, r) for t in tracks if (r := t.ret(stake)) is not None]
    rets = sorted(r for _t, r in valued)
    n = len(rets)
    if n == 0:
        return {"pools": len(tracks), "pools_priced": 0}
    mid = n // 2
    median = rets[mid] if n % 2 else (rets[mid - 1] + rets[mid]) / 2
    drained = sum(1 for t, _r in valued if t.drained())
    return {
        "pools": len(tracks),
        "pools_priced": n,
        "pools_one_swap": sum(1 for t, _r in valued if t.swaps == 1),
        "equal_weight_return": _fmt(sum(rets) / n),
        "median_return": _fmt(median),
        "share_up": _fmt(Fraction(sum(1 for r in rets if r > 0), n)),
        "share_down": _fmt(Fraction(sum(1 for r in rets if r < 0), n)),
        "share_drained": _fmt(Fraction(drained, n)),
        "p10_return": _fmt(_percentile(rets, 0.10)),
        "p90_return": _fmt(_percentile(rets, 0.90)),
        "best_return": _fmt(rets[-1]),
        "worst_return": _fmt(rets[0]),
    }


def compute_market_baseline(pack: Pack) -> dict[str, Any]:
    """Stream the tape once and summarise the day's launches and established pools. Pure."""
    numeraire = pack.numeraire
    start_ms = pack.manifest.period.start_utc_ms
    tracks: dict[str, _PoolTrack] = {}
    for key, pool in pack.pools.items():
        if pool.asset0 == numeraire:
            n0 = True
        elif pool.asset1 == numeraire:
            n0 = False
        else:
            continue
        cl = (
            str(pool.model).endswith("_cl")
            or bool(pool.supported_by_clmm)
            or pool.initial_sqrt_price_x96 is not None
        )
        t = _PoolTrack(
            numeraire_is_0=n0,
            is_launch=bool(pool.created_time_utc_ms and pool.created_time_utc_ms >= start_ms),
            cl=cl,
        )
        if cl:
            for tick in pool.initial_ticks:
                try:
                    t.cl_liquidity += (
                        int(tick[2]) // 2
                    )  # gross liquidity counts each position twice (both bounds)
                except (IndexError, TypeError, ValueError):
                    pass
            if t.cl_liquidity == 0 and pool.initial_liquidity:
                t.cl_liquidity = int(pool.initial_liquidity)
            t.seen_modify = t.cl_liquidity > 0
        elif pool.initial_reserve0 and pool.initial_reserve1:
            t.depth = int(pool.initial_reserve0 if n0 else pool.initial_reserve1)
            t.peak_depth = t.depth
        if cl and pool.fee_pips is not None:
            t.fee_num, t.fee_den = 1_000_000 - int(pool.fee_pips), 1_000_000
        else:
            t.fee_num, t.fee_den = int(pool.fee_numerator), int(pool.fee_denominator)
        tracks[key] = t
    stake = 10 ** max(int(pack.manifest.numeraire_decimals) - STAKE_DECIMALS_BELOW_UNIT, 0)
    for row in pack.iter_tape():
        t = tracks.get(row.get("pool", ""))
        if t is None:
            continue
        kind = row.get("kind")
        if kind == "swap":
            t.pending_swap = True
            t.swaps += 1
        elif kind == "sync":
            r0, r1 = int(row.get("reserve0") or 0), int(row.get("reserve1") or 0)
            t.depth = r0 if t.numeraire_is_0 else r1
            t.peak_depth = max(t.peak_depth, t.depth)
            if t.pending_swap:
                p = t.price_from_reserves(r0, r1)
                if p is not None:
                    reserves = (r0, r1) if t.numeraire_is_0 else (r1, r0)
                    if t.first is None:
                        t.first = p
                        t.entry_reserves = reserves
                    t.last = p
                    t.exit_reserves = reserves
                t.pending_swap = False
        elif kind == "cl_swap":
            t.swaps += 1
            sqrt_after = int(row.get("sqrt_price_x96_after") or 0)
            p = t.price_from_sqrt(sqrt_after)
            if p is not None:
                if t.first is None:
                    t.first = p
                t.last = p
                t.cl_sqrt_after = sqrt_after
                t.cl_liquidity_after = int(row.get("liquidity_after") or 0)
        elif kind == "cl_modify":
            t.seen_modify = True
            t.cl_liquidity += int(row.get("liquidity_delta") or 0)
    launches = [t for t in tracks.values() if t.is_launch]
    established = [t for t in tracks.values() if not t.is_launch]
    return {
        "basis": BASELINE_BASIS,
        "pack_id": pack.pack_id,
        "rule": "Put the same small stake into every pool right after its first recorded swap of the day, hold to the end, and sell back into the pool as it stands at the close. A pulled pool pays dust.",
        "stake_raw": str(stake),
        "stake": f"{stake / 10 ** int(pack.manifest.numeraire_decimals):g}",
        "numeraire_hold_return": "0.000000",
        "launches": _basket(launches, stake),
        "established": _basket(established, stake),
        "all_pools": _basket(launches + established, stake),
        "caveats": list(CAVEATS),
    }


def write_market_baseline(pack_dir: Path | str) -> dict[str, Any]:
    p = Path(pack_dir)
    pack = Pack.load(p, verify_hashes=False)
    out = compute_market_baseline(pack)
    previous = read_market_baseline(p) or {}
    for key in ("ecosystem", "crypto_market"):  # read by the collectors through the network; the tape cannot rebuild them
        if previous.get(key):
            out[key] = previous[key]
    (p / BASELINE_FILE).write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
    return out


def read_market_baseline(pack_dir: Path | str) -> dict[str, Any] | None:
    fp = Path(pack_dir) / BASELINE_FILE
    try:
        data = json.loads(fp.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def market_lines(b: dict[str, Any] | None) -> dict[str, Any] | None:
    """The three numbers shown for a day: Base ecosystem, ETH and the crypto market, each as a
    fractional change in dollars over the day, with the ecosystem also against ETH. None where a
    line has not been read."""
    if not b:
        return None
    eco = b.get("ecosystem") or {}
    tokens = eco.get("base_tokens") or {}
    vs_eth = tokens.get("depth_weighted_return_vs_eth")
    eth_usd = eco.get("eth_usd_return")
    base_usd = None
    if vs_eth is not None and eth_usd is not None:
        base_usd = f"{(1 + float(vs_eth)) * (1 + float(eth_usd)) - 1:.6f}"
    crypto = b.get("crypto_market") or {}
    return {
        "base_ecosystem_usd": base_usd,
        "base_ecosystem_vs_eth": vs_eth,
        "base_ecosystem_tokens": tokens.get("tokens"),
        "base_ecosystem_median_vs_eth": tokens.get("median_return_vs_eth"),
        "base_ecosystem_share_up": tokens.get("share_up"),
        "eth_usd": eth_usd,
        "btc_usd": eco.get("btc_usd_return"),
        "crypto_market_usd": crypto.get("return"),
        "crypto_market_coins": len(crypto.get("coins") or []) or None,
    }


def baseline_sentence(b: dict[str, Any] | None) -> str | None:
    """One plain sentence for a leaderboard or an episode list: what Base, ETH and the crypto market
    did that day. Never a verdict."""
    lines = market_lines(b)
    if not lines:
        return None
    pct = lambda s: f"{float(s) * 100:+.1f}%" if s is not None else "not read"  # noqa: E731
    if lines["base_ecosystem_usd"] is None and lines["eth_usd"] is None and lines["crypto_market_usd"] is None:
        return None
    parts = [f"Market that day: the Base ecosystem {pct(lines['base_ecosystem_usd'])} in dollars ({pct(lines['base_ecosystem_vs_eth'])} against ETH, {lines['base_ecosystem_tokens'] or 0} tokens weighted by pool depth)"]
    parts.append(f"ETH {pct(lines['eth_usd'])}")
    parts.append(f"the crypto market {pct(lines['crypto_market_usd'])}")
    return ", ".join(parts) + ". Holding ETH returned 0% in ETH terms, which is what results are scored in."
