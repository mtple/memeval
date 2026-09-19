"""The Base ecosystem on a recorded day: a fixed basket of the chain's large tokens, priced against
ETH at the first and last block of the day through the read-only RPC endpoint.

The launch basket in ``datasets/baseline.py`` says what buying every new coin would have done, and on
Base that is a rug-pull statistic. Owners asked to compare with the market as a whole, which is this:
DEGEN, BRETT, TOSHI, AERO, VIRTUAL and cbBTC against ETH, plus ETH itself in dollars through the
USDC pool. Each token's deepest Uniswap v3 pool against WETH is found through the factory, its
symbol is checked on chain, and its price is read from ``slot0`` at the two blocks. About 120 RPC
requests per day. Read-only and budgeted like every collector; never imported by the engine.
"""

from __future__ import annotations

import json
import os
from fractions import Fraction
from pathlib import Path
from typing import Any

import yaml

from ..datasets.baseline import BASELINE_FILE
from .base import Budget, HttpCollector, ProviderError, ReceiptStore
from .evm_rpc import RpcClient, hex_to_int, word

ECOSYSTEM_BASIS = "base_large_token_basket_vs_eth_v1"
WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
V3_FACTORY = "0x33128a8fC17869897dcE68Ed026d694621f6FDfD"
FEES = (500, 3000, 10000)
Q96 = 1 << 96
# symbol as the contract reports it, address; a token whose on-chain symbol differs is left out and noted
BASKET: tuple[tuple[str, str], ...] = (
    ("DEGEN", "0x4ed4e862860bed51a9570b96d89af5e1b0efefed"),
    ("BRETT", "0x532f27101965dd16442e59d40670faf5ebb142e4"),
    ("TOSHI", "0xac1bd2486aaf3b5c0fc3fd868558b082a531b2b4"),
    ("AERO", "0x940181a94a35a4569e4529a3cdfb74e38fd98631"),
    ("VIRTUAL", "0x0b3e328455c4059eeb9e3f84b5543f74e24e7e1b"),
    ("cbBTC", "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf"),
)
SEL_SYMBOL = "0x95d89b41"
SEL_DECIMALS = "0x313ce567"
SEL_GET_POOL = "0x1698ee82"
SEL_LIQUIDITY = "0x1a686502"
SEL_SLOT0 = "0x3850c7bd"

CAVEATS = [
    "A fixed basket of six large Base tokens against ETH, not a published index and not weighted by market value; the depth-weighted line weights each token by the ETH its deepest v3 pool held at the start of the day.",
    "Prices are the mid price of one Uniswap v3 pool at the day's first and last block, so they include no fees or slippage.",
    "ETH in dollars comes from the deepest WETH/USDC v3 pool on Base, not from an exchange.",
    "Returns against ETH are what an agent's final ETH return can be compared with; the dollar line only says what ETH itself did.",
]


def _pad_addr(addr: str) -> str:
    return addr.lower().replace("0x", "").rjust(64, "0")


def _decode_string(data_hex: str) -> str:
    raw = bytes.fromhex(data_hex[2:] if data_hex.startswith("0x") else data_hex)
    if len(raw) >= 64:
        offset = int.from_bytes(raw[:32], "big")
        length = int.from_bytes(raw[offset : offset + 32], "big")
        return raw[offset + 32 : offset + 32 + length].decode("utf-8", "replace")
    return raw.rstrip(b"\x00").decode("utf-8", "replace")


def _fmt(x: Fraction | None) -> str | None:
    return None if x is None else f"{float(x):.6f}"


def _token_price_in_weth(sqrt_p: int, weth_is_token0: bool) -> Fraction:
    p1_per_0 = Fraction(sqrt_p * sqrt_p, Q96 * Q96)
    return 1 / p1_per_0 if weth_is_token0 else p1_per_0


def _eth_depth(liquidity: int, sqrt_p: int, weth_is_token0: bool) -> int:
    """ETH the active liquidity holds from the current price to the end of the range, an upper bound."""
    if liquidity <= 0 or sqrt_p <= 0:
        return 0
    return (liquidity * Q96) // sqrt_p if weth_is_token0 else (liquidity * sqrt_p) // Q96


def _deepest_pool(rpc: RpcClient, token: str, block: int) -> tuple[str, int, int] | None:
    """(pool, fee, liquidity) of the token's deepest WETH pool on the v3 factory at ``block``."""
    a, b = sorted([token.lower(), WETH])
    best: tuple[str, int, int] | None = None
    for fee in FEES:
        data = SEL_GET_POOL + _pad_addr(a) + _pad_addr(b) + hex(fee)[2:].rjust(64, "0")
        res = rpc.eth_call(V3_FACTORY, data, block)
        pool = "0x" + res[-40:] if res and len(res) >= 42 else None
        if not pool or int(pool, 16) == 0:
            continue
        liq_hex = rpc.eth_call(pool, SEL_LIQUIDITY, block)
        liq = hex_to_int(liq_hex) if liq_hex and liq_hex != "0x" else 0
        if best is None or liq > best[2]:
            best = (pool, fee, liq)
    return best


def _sqrt_price(rpc: RpcClient, pool: str, block: int) -> int:
    res = rpc.eth_call(pool, SEL_SLOT0, block)
    if not res or res == "0x":
        raise ProviderError(f"slot0 returned nothing for {pool}")
    return word(res, 0)


def collect_ecosystem(pack_dir: Path | str, *, rpc_url_override: str | None = None, rpc_url_env: str = "BASE_RPC_URL", transport=None, sleep=None, max_requests: int = 600) -> dict[str, Any]:
    """Read the basket at the pack's first and last block and return the ecosystem section. Pure apart
    from the budgeted RPC reads; the caller decides where it is written."""
    p = Path(pack_dir)
    manifest = yaml.safe_load((p / "manifest.yaml").read_text())
    period = manifest["period"]
    start_ms, end_ms = int(period["start_utc_ms"]), int(period["end_utc_ms"])
    ranges = [(int(r[0]), int(r[1])) for r in (manifest.get("universe") or {}).get("indexed_block_ranges") or []]
    rpc_url = rpc_url_override or os.environ.get(rpc_url_env)
    if not rpc_url:
        raise ProviderError(f"environment variable {rpc_url_env} is not set; no endpoint is configured")
    work = p.parent / (p.name + "_work")
    work.mkdir(parents=True, exist_ok=True)
    budget = Budget(max_requests=max_requests, max_response_bytes=64 * 1024 * 1024)
    http = HttpCollector(provider="evm_rpc", budget=budget, receipts=ReceiptStore(work / "receipts_ecosystem", store_bodies=False), errors_path=work / "errors_ecosystem.jsonl", transport=transport)
    if sleep is not None:
        http.sleep = sleep
    rpc = RpcClient(rpc_url, http)
    if int(manifest.get("chain_id") or 0) != 8453 or rpc.chain_id() != 8453:
        raise ProviderError("the ecosystem basket is defined for Base (chain 8453) only")
    if ranges:
        lo = max(min(r[0] for r in ranges) - 2000, 1)
        hi = max(r[1] for r in ranges) + 2000
    else:
        hi = rpc.block_number()
        lo = max(hi - 400_000, 1)
    hi = min(hi, rpc.block_number())
    start_block, start_ts = rpc.find_block_at_or_after(start_ms, lo, hi)
    end_block, end_ts = rpc.find_block_at_or_after(end_ms, start_block, hi)
    end_block = max(start_block, end_block - 1)  # the last block inside the day
    notes: list[str] = []
    tokens: list[dict[str, Any]] = []
    for expected, addr in BASKET:
        try:
            symbol = _decode_string(rpc.eth_call(addr, SEL_SYMBOL, start_block)).strip()
        except (ProviderError, ValueError) as e:
            notes.append(f"{expected}: symbol() failed ({e}); left out")
            continue
        if symbol.lower() != expected.lower():
            notes.append(f"{expected}: contract reports symbol {symbol!r}; left out")
            continue
        pool = _deepest_pool(rpc, addr, start_block)
        if pool is None or pool[2] <= 0:
            notes.append(f"{expected}: no WETH pool with liquidity on the v3 factory; left out")
            continue
        pool_addr, fee, liq = pool
        weth0 = WETH < addr.lower()
        sp0, sp1 = _sqrt_price(rpc, pool_addr, start_block), _sqrt_price(rpc, pool_addr, end_block)
        p0, p1 = _token_price_in_weth(sp0, weth0), _token_price_in_weth(sp1, weth0)
        tokens.append({"symbol": symbol, "pool_fee_pips": fee, "eth_depth_start_raw": str(_eth_depth(liq, sp0, weth0)), "return_vs_eth": _fmt(p1 / p0 - 1)})
    eth_usd: str | None = None
    try:
        pool = _deepest_pool(rpc, USDC, start_block)
        if pool is not None:
            weth0 = WETH < USDC
            sp0, sp1 = _sqrt_price(rpc, pool[0], start_block), _sqrt_price(rpc, pool[0], end_block)
            usd0 = 1 / _token_price_in_weth(sp0, weth0) * Fraction(10**12)  # USDC (6 dp) per ETH (18 dp)
            usd1 = 1 / _token_price_in_weth(sp1, weth0) * Fraction(10**12)
            eth_usd = _fmt(usd1 / usd0 - 1)
            notes.append(f"ETH/USD read from the {pool[1] / 10000:g}% WETH/USDC v3 pool: {float(usd0):.2f} at the start, {float(usd1):.2f} at the end")
    except (ProviderError, ValueError, ZeroDivisionError) as e:
        notes.append(f"ETH/USD unavailable: {e}")
    rets = [Fraction(t["return_vs_eth"]) for t in tokens]
    weights = [int(t["eth_depth_start_raw"]) for t in tokens]
    equal = sum(rets) / len(rets) if rets else None
    weighted = sum(r * w for r, w in zip(rets, weights, strict=True)) / sum(weights) if rets and sum(weights) > 0 else None
    return {
        "basis": ECOSYSTEM_BASIS,
        "tokens": tokens,
        "tokens_expected": [s for s, _a in BASKET],
        "start_block": start_block,
        "end_block": end_block,
        "start_utc_ms": start_ts,
        "end_utc_ms": end_ts,
        "equal_weight_return_vs_eth": _fmt(equal),
        "depth_weighted_return_vs_eth": _fmt(weighted),
        "eth_usd_return": eth_usd,
        "numeraire_hold_return": "0.000000",
        "notes": notes,
        "caveats": list(CAVEATS),
        "budget": budget.as_dict(),
    }


def write_ecosystem(pack_dir: Path | str, **kw: Any) -> dict[str, Any]:
    """Merge the ecosystem section into the pack's market_baseline.json (kept if present, created otherwise)."""
    p = Path(pack_dir)
    section = collect_ecosystem(p, **kw)
    fp = p / BASELINE_FILE
    try:
        data = json.loads(fp.read_text())
    except (OSError, ValueError):
        data = {}
    data["ecosystem"] = section
    fp.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    return section
