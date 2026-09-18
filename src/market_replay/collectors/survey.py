"""How much happens on Base in a week: every pool launch and every swap, per venue.

Read-only and budgeted like the collectors, but it records nothing. It answers the sizing
question behind an all-launches recording: how many pools launched in the window, how many of
them have an ETH leg, how many swaps they produced, how that volume is distributed, and how many
swaps the venues' pre-existing pools produced in the same week.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path
from typing import Any

from .base import Budget, HttpCollector, ProviderError, ReceiptStore
from .evm_rpc import (
    TOPIC_PAIR_CREATED,
    TOPIC_SWAP,
    TOPIC_V3_POOL_CREATED,
    TOPIC_V3_SWAP,
    TOPIC_V4_INITIALIZE,
    TOPIC_V4_SWAP,
    RpcClient,
    hex_to_int,
    topic_address,
)
from .historical import BLOCK_INTERVAL_MS, DEPLOYMENTS, iso_ms

BUCKETS = ((0, "0"), (1, "1-5"), (6, "6-20"), (21, "21-100"), (101, "101-1000"), (1001, "1001+"))


def bucket(n: int) -> str:
    label = BUCKETS[0][1]
    for lo, name in BUCKETS:
        if n >= lo:
            label = name
    return label


def _scan(rpc: RpcClient, *, address: str | None, topics: list[Any], start: int, end: int, chunk: int, on_logs, progress) -> int:
    """Walk [start, end) in chunks, halving the chunk on provider errors; returns the chunk size in use."""
    cursor = start
    while cursor < end:
        to_b = min(cursor + chunk - 1, end - 1)
        try:
            logs = rpc.get_logs(address=address, topics=topics, from_block=cursor, to_block=to_b)
        except ProviderError as e:
            if chunk > 25:
                chunk //= 2
                progress(f"chunk reduced to {chunk} after provider error: {str(e)[:120]}")
                continue
            raise
        on_logs(logs)
        cursor = to_b + 1
        progress(None)
    return chunk


def survey_week(*, rpc_url: str, chain: str, period_start_utc: str, period_end_utc: str, work: Path, max_requests: int = 60000, log_chunk_blocks: int = 1000, swap_chunk_blocks: int = 200, echo=None, transport=None) -> dict[str, Any]:
    echo = echo or (lambda s: None)
    work.mkdir(parents=True, exist_ok=True)
    budget = Budget(max_requests=max_requests, max_response_bytes=8 * 1024 * 1024 * 1024)
    http = HttpCollector(provider="evm_rpc", budget=budget, receipts=ReceiptStore(work / "receipts", store_bodies=False), errors_path=work / "errors.jsonl", transport=transport)
    rpc = RpcClient(rpc_url, http)
    deps = {p: DEPLOYMENTS[p][chain] for p in ("uniswap_v2", "uniswap_v3", "uniswap_v4")}
    wn = deps["uniswap_v2"]["wrapped_native"].lower()
    native = deps["uniswap_v4"].get("native_currency", "0x" + "0" * 40).lower()
    p_start, p_end = iso_ms(period_start_utc), iso_ms(period_end_utc)
    t0 = time.time()
    latest = rpc.block_number()
    lo = max(0, latest - (rpc.block_timestamp_ms(latest) - p_start) // BLOCK_INTERVAL_MS - 5000)
    b_s, _ = rpc.find_block_at_or_after(p_start, lo, latest)
    b_e, _ = rpc.find_block_at_or_after(p_end, b_s, latest)
    echo(f"period blocks [{b_s}, {b_e}): {b_e - b_s} blocks")
    counters = {"chunks": 0}

    def progress(msg):
        if msg:
            echo(msg)
        counters["chunks"] += 1
        if counters["chunks"] % 100 == 0:
            echo(f"  ... {counters['chunks']} ranges read, {budget.requests} requests, {budget.response_bytes // (1024 * 1024)} MB, {int(time.time() - t0)} s")

    # ---- launches inside the week, per venue: address (v2/v3) or pool id (v4) -> legs
    launches: dict[str, dict[str, dict[str, Any]]] = {"uniswap_v2": {}, "uniswap_v3": {}, "uniswap_v4": {}}

    def on_v2_created(logs):
        for lg in logs:
            pair = "0x" + lg["data"][2:][24:64].lower()
            launches["uniswap_v2"][pair] = {"t0": topic_address(lg["topics"][1]), "t1": topic_address(lg["topics"][2]), "block": hex_to_int(lg["blockNumber"])}

    def on_v3_created(logs):
        for lg in logs:
            pool = "0x" + lg["data"][2:][-40:].lower()
            launches["uniswap_v3"][pool] = {"t0": topic_address(lg["topics"][1]), "t1": topic_address(lg["topics"][2]), "block": hex_to_int(lg["blockNumber"])}

    def on_v4_init(logs):
        for lg in logs:
            launches["uniswap_v4"][lg["topics"][1].lower()] = {"t0": topic_address(lg["topics"][2]), "t1": topic_address(lg["topics"][3]), "block": hex_to_int(lg["blockNumber"]), "hooks": "0x" + lg["data"][2:][24:64].lower()}

    echo("reading pool launches")
    _scan(rpc, address=deps["uniswap_v2"]["factory"], topics=[TOPIC_PAIR_CREATED], start=b_s, end=b_e, chunk=log_chunk_blocks, on_logs=on_v2_created, progress=progress)
    _scan(rpc, address=deps["uniswap_v3"]["factory"], topics=[TOPIC_V3_POOL_CREATED], start=b_s, end=b_e, chunk=log_chunk_blocks, on_logs=on_v3_created, progress=progress)
    _scan(rpc, address=deps["uniswap_v4"]["pool_manager"], topics=[TOPIC_V4_INITIALIZE], start=b_s, end=b_e, chunk=log_chunk_blocks, on_logs=on_v4_init, progress=progress)
    for venue, d in launches.items():
        echo(f"  {venue}: {len(d)} launches")

    # ---- swaps inside the week: launched pools vs pre-existing pools, per venue
    swaps_launched: dict[str, Counter] = {v: Counter() for v in launches}
    swaps_existing: dict[str, Counter] = {v: Counter() for v in launches}

    def on_v4_swap(logs):
        for lg in logs:
            pid = lg["topics"][1].lower()
            (swaps_launched if pid in launches["uniswap_v4"] else swaps_existing)["uniswap_v4"][pid] += 1

    def on_v2_swap(logs):
        for lg in logs:
            a = lg["address"].lower()
            (swaps_launched if a in launches["uniswap_v2"] else swaps_existing)["uniswap_v2"][a] += 1

    def on_v3_swap(logs):
        for lg in logs:
            a = lg["address"].lower()
            (swaps_launched if a in launches["uniswap_v3"] else swaps_existing)["uniswap_v3"][a] += 1

    echo("reading v4 swaps (PoolManager)")
    _scan(rpc, address=deps["uniswap_v4"]["pool_manager"], topics=[TOPIC_V4_SWAP], start=b_s, end=b_e, chunk=log_chunk_blocks, on_logs=on_v4_swap, progress=progress)
    echo("reading v3 swaps (every emitter of the v3 Swap signature; non-Uniswap emitters counted as 'existing')")
    _scan(rpc, address=None, topics=[TOPIC_V3_SWAP], start=b_s, end=b_e, chunk=swap_chunk_blocks, on_logs=on_v3_swap, progress=progress)
    echo("reading v2 swaps (every emitter of the v2 Swap signature)")
    _scan(rpc, address=None, topics=[TOPIC_SWAP], start=b_s, end=b_e, chunk=swap_chunk_blocks, on_logs=on_v2_swap, progress=progress)

    out: dict[str, Any] = {"chain": chain, "period_start_utc": period_start_utc, "period_end_utc": period_end_utc, "blocks": [b_s, b_e], "venues": {}, "requests": budget.requests, "response_mb": round(budget.response_bytes / (1024 * 1024), 1), "seconds": int(time.time() - t0)}
    for venue, d in launches.items():
        eth = {k for k, v in d.items() if wn in (v["t0"], v["t1"]) or (venue == "uniswap_v4" and native in (v["t0"], v["t1"]))}
        sl = swaps_launched[venue]
        dist = Counter(bucket(sl.get(k, 0)) for k in d)
        dist_eth = Counter(bucket(sl.get(k, 0)) for k in eth)
        top = sl.most_common(10)
        out["venues"][venue] = {
            "launches": len(d),
            "launches_with_eth_leg": len(eth),
            "launches_with_any_swap": sum(1 for k in d if sl.get(k, 0) > 0),
            "eth_launches_with_any_swap": sum(1 for k in eth if sl.get(k, 0) > 0),
            "swaps_in_launched_pools": sum(sl.values()),
            "swaps_in_eth_launched_pools": sum(sl.get(k, 0) for k in eth),
            "swaps_in_preexisting_pools": sum(swaps_existing[venue].values()),
            "preexisting_pools_with_swaps": len(swaps_existing[venue]),
            "launched_pools_by_swap_count": dict(sorted(dist.items(), key=lambda kv: BUCKETS[[b[1] for b in BUCKETS].index(kv[0])][0])),
            "eth_launched_pools_by_swap_count": dict(sorted(dist_eth.items(), key=lambda kv: BUCKETS[[b[1] for b in BUCKETS].index(kv[0])][0])),
            "top_launched_pools_by_swaps": top,
            "hooked_launches": sum(1 for v in d.values() if v.get("hooks") and int(v["hooks"], 16) != 0) if venue == "uniswap_v4" else None,
        }
    (work / "survey.json").write_text(json.dumps(out, indent=2))
    return out
