"""The Base market on a recorded day, as separate reference lines an agent's result can be read
against, each from a rule rather than a list of coins:

* ``base_tokens``: every Base-native token with an ETH pool on Uniswap v2 or v3 that traded in both the
  first and the last minutes of the day, against ETH, weighted by the ETH its pools hold (plus the
  equal-weight and median token). Stablecoins and wrapped majors are excluded so the line means
  Base's own tokens; nothing else is chosen by hand.
* ``eth_usd`` and ``btc_usd``: what ETH and BTC did in dollars, from the deepest WETH/USDC and
  cbBTC pools on the chain. Together they are most of the crypto market.
* ``tvl`` (see ``tvl.py``): the value locked on Base at the start and end of the day.

Prices come from Swap and Sync events in the day's edge blocks, because public endpoints prune old
state but keep logs; token pairs of the pools are read at the latest block through Multicall3.
About 100 to 200 read-only requests per day, budgeted like every collector; never imported by the
engine.
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
from .evm_rpc import (
    SEL_TOKEN0,
    SEL_TOKEN1,
    TOPIC_SWAP,
    TOPIC_SYNC,
    TOPIC_V3_SWAP,
    RpcClient,
    hex_to_int,
    word,
)

ECOSYSTEM_BASIS = "base_native_tokens_vs_eth_all_eth_pools_v2"
WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
CBBTC = "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf"
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
SEL_AGGREGATE3 = "0x82ad56cb"
Q96 = 1 << 96
EDGE_BLOCKS = 600  # the first and last 20 minutes of the day
MIN_ETH_DEPTH = 10**17  # pools holding under 0.1 ETH are dust and their prices are noise
WEIGHT_CAP = Fraction(1, 10)  # no token is more than a tenth of the depth-weighted line, as capped indexes do
# not Base-native: stablecoins and bridged or wrapped majors, excluded from the token line by address
NOT_NATIVE: dict[str, str] = {
    WETH: "WETH",
    USDC: "USDC",
    CBBTC: "cbBTC",
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca": "USDbC",
    "0x50c5725949a6f0c72e6c4a641f24049a917db0cb": "DAI",
    "0xfde4c96c8593536e31f229ea8f37b2ada2699bb2": "USDT",
    "0x60a3e35cc302bfa44cb288bc5a4f316fdb1adb42": "EURC",
    "0x2ae3f1ec7f1f5012cfeab0185bfc7aa3cf0dec22": "cbETH",
    "0xc1cba3fcea344f92d9239c08c0568f6f2f0ee452": "wstETH",
    "0x04c0599ae5a44757c0af6f9ec3b93da8976c150a": "weETH",
    "0xb6fe221fe9eef5aba221c348ba20a1bf5e73624c": "rETH",
    "0x2416092f143378750bb29b79ed961ab195cceea5": "ezETH",
    "0x4c80e24119cfb836cdf0a6b53dc23f04f7e652ca": "USD+",
}

CAVEATS = [
    "Base tokens: every token with an ETH pool on Uniswap v2 or v3 that traded in both the first and the last 20 minutes of the day; a token that went quiet at either end is not counted. Weighted by the ETH its pools held at the start, with no token above a tenth of the total, so the line follows the big tokens without one of them deciding it; the equal-weight and median lines give the small ones their say.",
    "Prices are pool prices after a swap, with no fees or slippage; the ETH depth of a concentrated pool is an upper bound from its active liquidity.",
    "ETH and BTC in dollars come from the deepest WETH/USDC and cbBTC/USDC or cbBTC/WETH pools on Base, not from an exchange.",
    "Returns against ETH are what an agent's final ETH return can be compared with; the dollar lines say what the majors did, which an ETH-denominated result does not see.",
]


def _pad_addr(addr: str) -> str:
    return addr.lower().replace("0x", "").rjust(64, "0")


def _fmt(x: Fraction | None) -> str | None:
    return None if x is None else f"{float(x):.6f}"


def _price_in_weth_from_sqrt(sqrt_p: int, weth_is_token0: bool) -> Fraction:
    p1_per_0 = Fraction(sqrt_p * sqrt_p, Q96 * Q96)
    return 1 / p1_per_0 if weth_is_token0 else p1_per_0


def _eth_depth_cl(liquidity: int, sqrt_p: int, weth_is_token0: bool) -> int:
    if liquidity <= 0 or sqrt_p <= 0:
        return 0
    return (liquidity * Q96) // sqrt_p if weth_is_token0 else (liquidity * sqrt_p) // Q96


def encode_aggregate3(calls: list[tuple[str, str]]) -> str:
    """Multicall3.aggregate3((address target, bool allowFailure, bytes callData)[]) with 4-byte calldata."""
    n = len(calls)
    head = SEL_AGGREGATE3 + (32).to_bytes(32, "big").hex() + n.to_bytes(32, "big").hex()
    offsets = "".join((32 * n + 160 * i).to_bytes(32, "big").hex() for i in range(n))
    body = ""
    for target, data in calls:
        raw = bytes.fromhex(data[2:])
        body += _pad_addr(target) + (1).to_bytes(32, "big").hex() + (96).to_bytes(32, "big").hex() + len(raw).to_bytes(32, "big").hex() + raw.ljust(32, b"\x00").hex()
    return head + offsets + body


def decode_aggregate3(result_hex: str) -> list[bytes | None]:
    """The returnData of each call, None where the call failed."""
    raw = bytes.fromhex(result_hex[2:] if result_hex.startswith("0x") else result_hex)
    arr = int.from_bytes(raw[0:32], "big")
    n = int.from_bytes(raw[arr : arr + 32], "big")
    base = arr + 32
    out: list[bytes | None] = []
    for i in range(n):
        off = base + int.from_bytes(raw[base + 32 * i : base + 32 * i + 32], "big")
        ok = int.from_bytes(raw[off : off + 32], "big") == 1
        doff = off + int.from_bytes(raw[off + 32 : off + 64], "big")
        length = int.from_bytes(raw[doff : doff + 32], "big")
        out.append(raw[doff + 32 : doff + 32 + length] if ok else None)
    return out


def _scan_all(rpc: RpcClient, *, topics: list[Any], start: int, end: int, chunk: int = 200) -> list[dict[str, Any]]:
    """Every log with these topics in [start, end], any address, halving the range on provider errors."""
    out: list[dict[str, Any]] = []
    cur = start
    while cur <= end:
        to_b = min(cur + chunk - 1, end)
        try:
            out.extend(rpc.get_logs(address=None, topics=topics, from_block=cur, to_block=to_b))
        except ProviderError as e:
            if chunk > 10:
                chunk //= 2
                continue
            raise ProviderError(f"log range [{cur}, {to_b}] failed even at {chunk} blocks: {e}") from e
        cur = to_b + 1
    return out


def _pool_tokens(rpc: RpcClient, pools: list[str]) -> dict[str, tuple[str, str]]:
    """token0 and token1 of each pool through Multicall3 at the latest block, 60 pools per request."""
    out: dict[str, tuple[str, str]] = {}
    for i in range(0, len(pools), 60):
        batch = pools[i : i + 60]
        calls = [(p, sel) for p in batch for sel in (SEL_TOKEN0, SEL_TOKEN1)]
        res = decode_aggregate3(rpc.eth_call(MULTICALL3, encode_aggregate3(calls), "latest"))
        for j, p in enumerate(batch):
            t0, t1 = res[2 * j], res[2 * j + 1]
            if t0 and t1 and len(t0) >= 32 and len(t1) >= 32:
                out[p] = ("0x" + t0[12:32].hex(), "0x" + t1[12:32].hex())
    return out


def _edge_prices(rpc: RpcClient, start_block: int, end_block: int) -> dict[str, dict[str, Any]]:
    """Per pool address: price observations (sqrt price and liquidity for v3, reserves for v2) at both
    edges of the day, for every pool that swapped in both windows."""
    s_lo, s_hi = start_block, min(start_block + EDGE_BLOCKS - 1, end_block)
    e_lo, e_hi = max(end_block - EDGE_BLOCKS + 1, start_block), end_block
    obs: dict[str, dict[str, Any]] = {}
    for edge, lo, hi in (("start", s_lo, s_hi), ("end", e_lo, e_hi)):
        v3 = _scan_all(rpc, topics=[TOPIC_V3_SWAP], start=lo, end=hi)
        v3.sort(key=lambda lg: (hex_to_int(lg["blockNumber"]), hex_to_int(lg["logIndex"])))
        for lg in v3:
            d = obs.setdefault(lg["address"].lower(), {"kind": "v3"})
            if d["kind"] != "v3":
                continue
            key = (lg["data"], "first") if edge == "start" else (lg["data"], "last")
            if edge == "start" and "start" in d:
                continue  # the first swap of the day
            d[edge] = (word(key[0], 2), word(key[0], 3))  # (sqrtPriceX96, liquidity) after the swap; the end keeps the last
        v2 = _scan_all(rpc, topics=[[TOPIC_SWAP, TOPIC_SYNC]], start=lo, end=hi)
        v2.sort(key=lambda lg: (hex_to_int(lg["blockNumber"]), hex_to_int(lg["logIndex"])))
        swapped: set[str] = set()
        for lg in v2:
            addr = lg["address"].lower()
            topic = lg["topics"][0].lower()
            if topic == TOPIC_SWAP.lower():
                swapped.add(addr)
                continue
            d = obs.setdefault(addr, {"kind": "v2"})
            if d["kind"] != "v2" or addr not in swapped:
                continue
            reserves = (word(lg["data"], 0), word(lg["data"], 1))
            if edge == "start" and "start" in d:
                continue
            d[edge] = reserves  # the sync right after the first swap; the end keeps the last
    return {a: d for a, d in obs.items() if "start" in d and "end" in d}


def _pool_line(d: dict[str, Any], weth_is_token0: bool) -> tuple[Fraction, Fraction, int] | None:
    """(price at start, price at end, ETH depth at start) of one pool in WETH per token."""
    if d["kind"] == "v3":
        (sp0, liq0), (sp1, _liq1) = d["start"], d["end"]
        if sp0 <= 0 or sp1 <= 0:
            return None
        return _price_in_weth_from_sqrt(sp0, weth_is_token0), _price_in_weth_from_sqrt(sp1, weth_is_token0), _eth_depth_cl(liq0, sp0, weth_is_token0)
    (r0a, r1a), (r0b, r1b) = d["start"], d["end"]
    num_a, tok_a = (r0a, r1a) if weth_is_token0 else (r1a, r0a)
    num_b, tok_b = (r0b, r1b) if weth_is_token0 else (r1b, r0b)
    if min(num_a, tok_a, num_b, tok_b) <= 0:
        return None
    return Fraction(num_a, tok_a), Fraction(num_b, tok_b), num_a


def capped_weights(weights: list[int], cap: Fraction = WEIGHT_CAP) -> list[Fraction]:
    """Normalised weights with none above ``cap`` (or 1/n when the cap cannot be met); the excess of a
    capped weight is spread over the uncapped ones in proportion, until nothing is above the cap."""
    n = len(weights)
    total = sum(weights)
    if n == 0 or total <= 0:
        return []
    cap = max(cap, Fraction(1, n))
    w = [Fraction(x, total) for x in weights]
    fixed: set[int] = set()
    for _round in range(n):
        over = [i for i in range(n) if i not in fixed and w[i] > cap]
        if not over:
            break
        fixed.update(over)
        for i in over:
            w[i] = cap
        free = [i for i in range(n) if i not in fixed]
        room = 1 - sum(w[i] for i in fixed)
        free_total = sum(w[i] for i in free)
        if not free or free_total <= 0:
            break
        for i in free:
            w[i] = w[i] / free_total * room
    return w


def _basket(rows: list[tuple[Fraction, int]]) -> dict[str, Any]:
    """rows: (return, weight) per token."""
    if not rows:
        return {"tokens": 0}
    rets = sorted(r for r, _w in rows)
    n = len(rets)
    mid = n // 2
    median = rets[mid] if n % 2 else (rets[mid - 1] + rets[mid]) / 2
    total_w = sum(w for _r, w in rows)
    cw = capped_weights([w for _r, w in rows])
    return {
        "tokens": n,
        "depth_weighted_return_vs_eth": _fmt(sum(r * x for (r, _w), x in zip(rows, cw, strict=True))) if cw else None,
        "weight_cap": str(float(WEIGHT_CAP)),
        "largest_weight": _fmt(max(cw)) if cw else None,
        "equal_weight_return_vs_eth": _fmt(sum(rets) / n),
        "median_return_vs_eth": _fmt(median),
        "share_up": _fmt(Fraction(sum(1 for r in rets if r > 0), n)),
        "share_down": _fmt(Fraction(sum(1 for r in rets if r < 0), n)),
        "p10_return_vs_eth": _fmt(rets[max(0, int(0.1 * (n - 1)))]),
        "p90_return_vs_eth": _fmt(rets[min(n - 1, int(round(0.9 * (n - 1))))]),
        "eth_depth_total_raw": str(total_w),
    }


def collect_ecosystem(pack_dir: Path | str, *, rpc_url_override: str | None = None, rpc_url_env: str = "BASE_RPC_URL", transport=None, sleep=None, max_requests: int = 1500) -> dict[str, Any]:
    """Read the market lines for the pack's day. Pure apart from the budgeted RPC reads."""
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
    budget = Budget(max_requests=max_requests, max_response_bytes=512 * 1024 * 1024)
    http = HttpCollector(provider="evm_rpc", budget=budget, receipts=ReceiptStore(work / "receipts_ecosystem", store_bodies=False), errors_path=work / "errors_ecosystem.jsonl", transport=transport)
    if sleep is not None:
        http.sleep = sleep
    rpc = RpcClient(rpc_url, http)
    if int(manifest.get("chain_id") or 0) != 8453 or rpc.chain_id() != 8453:
        raise ProviderError("the market lines are defined for Base (chain 8453) only")
    head = rpc.block_number()
    if ranges:
        lo = max(min(r[0] for r in ranges) - 2000, 1)
        hi = min(max(r[1] for r in ranges) + 2000, head)
    else:
        hi = head
        lo = max(hi - 400_000, 1)
    start_block, start_ts = rpc.find_block_at_or_after(start_ms, lo, hi)
    end_block, end_ts = rpc.find_block_at_or_after(end_ms, start_block, hi)
    end_block = max(start_block, end_block - 1)  # the last block inside the day
    obs = _edge_prices(rpc, start_block, end_block)
    pairs = _pool_tokens(rpc, sorted(obs))
    # per token: pools against WETH, each a (price start, price end, depth)
    by_token: dict[str, list[tuple[Fraction, Fraction, int]]] = {}
    for addr, d in obs.items():
        t = pairs.get(addr)
        if t is None or WETH not in t:
            continue
        token = t[1] if t[0] == WETH else t[0]
        line = _pool_line(d, weth_is_token0=t[0] == WETH)
        if line is None or line[2] < MIN_ETH_DEPTH:
            continue
        by_token.setdefault(token, []).append(line)

    def token_return(lines: list[tuple[Fraction, Fraction, int]]) -> tuple[Fraction, int]:
        w = sum(depth for _a, _b, depth in lines)
        return sum((b / a - 1) * depth for a, b, depth in lines) / w, w

    native_rows = [token_return(v) for tok, v in by_token.items() if tok not in NOT_NATIVE]
    base_tokens = _basket(native_rows)
    base_tokens["pools"] = sum(len(v) for tok, v in by_token.items() if tok not in NOT_NATIVE)
    base_tokens["excluded_not_native"] = sorted(NOT_NATIVE[tok] for tok in by_token if tok in NOT_NATIVE)
    notes: list[str] = []

    def usd_price_of_eth() -> tuple[Fraction, Fraction] | None:
        lines = by_token.get(USDC)
        if not lines:
            return None
        a, b, _d = max(lines, key=lambda x: x[2])  # deepest pool: WETH per USDC raw
        return 1 / a * Fraction(10**12), 1 / b * Fraction(10**12)  # USDC (6 dp) per ETH (18 dp)

    eth_usd = usd_price_of_eth()
    eth_usd_return = _fmt(eth_usd[1] / eth_usd[0] - 1) if eth_usd else None
    if eth_usd:
        notes.append(f"ETH in dollars from the deepest WETH/USDC pool: {float(eth_usd[0]):.2f} at the start, {float(eth_usd[1]):.2f} at the end")
    else:
        notes.append("no WETH/USDC pool traded at both ends of the day; ETH in dollars unavailable")
    btc_usd_return: str | None = None
    btc_lines = by_token.get(CBBTC)
    if btc_lines and eth_usd:
        a, b, _d = max(btc_lines, key=lambda x: x[2])  # WETH per cbBTC raw
        btc_usd_return = _fmt((b / a) * (eth_usd[1] / eth_usd[0]) - 1)
        notes.append(f"BTC in dollars from the deepest cbBTC/WETH pool times ETH in dollars: {float(a * eth_usd[0] / 10**10):.0f} at the start, {float(b * eth_usd[1] / 10**10):.0f} at the end")
    else:
        notes.append("no cbBTC/WETH pool traded at both ends of the day; BTC in dollars unavailable")
    return {
        "basis": ECOSYSTEM_BASIS,
        "start_block": start_block,
        "end_block": end_block,
        "start_utc_ms": start_ts,
        "end_utc_ms": end_ts,
        "edge_blocks": EDGE_BLOCKS,
        "pools_observed": len(obs),
        "base_tokens": base_tokens,
        "eth_usd_return": eth_usd_return,
        "btc_usd_return": btc_usd_return,
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
