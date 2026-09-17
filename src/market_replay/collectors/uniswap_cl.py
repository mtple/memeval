"""Historical Base / Uniswap concentrated-liquidity connector (v3 pools, v4 PoolManager) and tape normalizer.

Same contract as the v2 connector in ``historical.py``: opt-in, read-only, explicitly budgeted,
resumable after every chunk (including the backward scan for the last swap before the window),
sliceable by a wall-clock ``deadline``. Produces an immutable ``historical_reconstruction`` pack
or a precise rejection reason.

Config (YAML) is the v2 config with ``protocol: uniswap_v3`` or ``protocol: uniswap_v4`` and,
for v4 only, an optional ``hooks_allowlist`` (list of hook addresses; default: no filter, the hook
address is recorded on every pool).

Initial state for a pool that exists before the window: a concentrated-liquidity pool cannot be
reconstructed from a reserve snapshot, so liquidity events (v4 ``ModifyLiquidity``, v3 ``Mint``/
``Burn``) are fetched from the pool's creation block, ``Swap`` events only from the prehistory
start. The pack's initial state is the full tick map folded from every liquidity event before the
prehistory start, the sqrt price / tick from the last ``Swap`` before it (v3: ``slot0`` at that
block when the endpoint has archive state; ``Initialize`` when no swap ever happened), and the
active liquidity summed from the tick map at that tick. ``initial_state_basis`` records which.

Sign convention on the tape (``cl_swap``): ``amount0``/``amount1`` are pool deltas, positive when
paid into the pool. This is the v3 event convention; v4 emits user deltas and is negated.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from ..domain.models import Rights, Universe
from ..domain.status import AvailabilityBasis, CoverageState, DataOrigin, PoolModel, TokenBehavior, UseStatus
from .base import (
    Budget,
    BudgetExhausted,
    Checkpoints,
    CoverageLedger,
    HttpCollector,
    ProviderError,
    ReceiptStore,
    now_ms,
)
from .evm_rpc import (
    SEL_DECIMALS,
    SEL_SLOT0,
    TOPIC_V3_BURN,
    TOPIC_V3_INITIALIZE,
    TOPIC_V3_MINT,
    TOPIC_V3_POOL_CREATED,
    TOPIC_V3_SWAP,
    TOPIC_V4_INITIALIZE,
    TOPIC_V4_MODIFY_LIQUIDITY,
    TOPIC_V4_SWAP,
    RpcClient,
    hex_to_int,
    sword,
    topic_address,
    topic_int,
    word,
)
from .historical import (
    BLOCK_INTERVAL_MS,
    CL_PROTOCOLS,
    DEPLOYMENTS,
    CollectionBlocked,
    LogFile,
    TimeSliceExpired,
    iso_ms,
    load_config,
)

CL_MODELS = {"uniswap_v3": PoolModel.UNISWAP_V3_CL, "uniswap_v4": PoolModel.UNISWAP_V4_CL}
FEE_DENOMINATOR_PIPS = 1_000_000
ACTIVITY_BATCH = 400  # pool ids / addresses per eth_getLogs filter in the activity scan (request-body caps)
DYNAMIC_FEE_FLAG = 0x800000  # v4 LPFeeLibrary.DYNAMIC_FEE_FLAG: the hook sets the fee per swap


# ---------------------------------------------------------------------- normalizer
def normalize_cl_logs(logs: list[dict[str, Any]], *, protocol: str, pool_key: str, fee_pips: int, block_time_ms) -> list[dict[str, Any]]:
    """Turn raw Initialize/ModifyLiquidity/Mint/Burn/Swap logs into ``cl_init``/``cl_modify``/``cl_swap`` rows.

    Every integer is an exact decimal string; signed ABI words are decoded as two's complement.
    Unknown topics are ignored (the ranges are requested by topic, so nothing else is expected).
    """
    rows: list[dict[str, Any]] = []
    ordered = sorted(logs, key=lambda l: (hex_to_int(l["blockNumber"]), hex_to_int(l["logIndex"])))
    seq = 0
    for lg in ordered:
        topic0 = lg["topics"][0].lower()
        block = hex_to_int(lg["blockNumber"])
        log_index = hex_to_int(lg["logIndex"])
        tx = lg.get("transactionHash")
        d = lg["data"]
        base = {"block": block, "log_index": log_index, "time_utc_ms": block_time_ms(block), "pool": pool_key, "tx": tx, "available_utc_ms": None, "availability_basis": str(AvailabilityBasis.RECONSTRUCTED_WITH_DELAY_MODEL), "received_utc_ms": now_ms()}
        row: dict[str, Any] | None = None
        if protocol == "uniswap_v4":
            if topic0 == TOPIC_V4_INITIALIZE:
                row = {**base, "kind": "cl_init", "sqrt_price_x96": str(word(d, 3)), "tick": sword(d, 4)}
            elif topic0 == TOPIC_V4_MODIFY_LIQUIDITY:
                row = {**base, "kind": "cl_modify", "tick_lower": sword(d, 0), "tick_upper": sword(d, 1), "liquidity_delta": str(sword(d, 2)), "wallet": topic_address(lg["topics"][2]) if len(lg["topics"]) > 2 else None}
            elif topic0 == TOPIC_V4_SWAP:
                # v4 amounts are user deltas (negative = paid into the pool); the tape keeps pool deltas.
                row = {**base, "kind": "cl_swap", "amount0": str(-sword(d, 0)), "amount1": str(-sword(d, 1)), "sqrt_price_x96_after": str(word(d, 2)), "liquidity_after": str(word(d, 3)), "tick_after": sword(d, 4), "fee_pips": word(d, 5), "wallet": topic_address(lg["topics"][2]) if len(lg["topics"]) > 2 else None}
        else:
            if topic0 == TOPIC_V3_INITIALIZE:
                row = {**base, "kind": "cl_init", "sqrt_price_x96": str(word(d, 0)), "tick": sword(d, 1)}
            elif topic0 == TOPIC_V3_MINT:
                row = {**base, "kind": "cl_modify", "tick_lower": topic_int(lg["topics"][2]), "tick_upper": topic_int(lg["topics"][3]), "liquidity_delta": str(word(d, 1)), "amount0": str(word(d, 2)), "amount1": str(word(d, 3)), "wallet": topic_address(lg["topics"][1])}
            elif topic0 == TOPIC_V3_BURN:
                row = {**base, "kind": "cl_modify", "tick_lower": topic_int(lg["topics"][2]), "tick_upper": topic_int(lg["topics"][3]), "liquidity_delta": str(-word(d, 0)), "amount0": str(word(d, 1)), "amount1": str(word(d, 2)), "wallet": topic_address(lg["topics"][1])}
            elif topic0 == TOPIC_V3_SWAP:
                row = {**base, "kind": "cl_swap", "amount0": str(sword(d, 0)), "amount1": str(sword(d, 1)), "sqrt_price_x96_after": str(word(d, 2)), "liquidity_after": str(word(d, 3)), "tick_after": sword(d, 4), "fee_pips": fee_pips, "wallet": topic_address(lg["topics"][1]) if len(lg["topics"]) > 1 else None}
        if row is None:
            continue
        seq += 1
        row["seq"] = seq
        rows.append(row)
    return rows


def fold_tick_map(modify_rows: list[dict[str, Any]]) -> list[list[str]]:
    """Fold ``cl_modify`` rows into the initialized tick list ``[tick, liquidity_net, liquidity_gross]`` (strings)."""
    net: dict[int, int] = {}
    gross: dict[int, int] = {}
    for r in modify_rows:
        delta = int(r["liquidity_delta"])
        lo, hi = int(r["tick_lower"]), int(r["tick_upper"])
        net[lo] = net.get(lo, 0) + delta
        net[hi] = net.get(hi, 0) - delta
        gross[lo] = gross.get(lo, 0) + delta
        gross[hi] = gross.get(hi, 0) + delta
    return [[str(t), str(net[t]), str(gross[t])] for t in sorted(net) if gross[t] != 0]


def active_liquidity(ticks: list[list[str]], tick: int) -> int:
    """Active liquidity at ``tick``: the sum of liquidity_net over initialized ticks at or below it."""
    return sum(int(net) for t, net, _g in ticks if int(t) <= tick)


# ---------------------------------------------------------------------- pipeline
def run_cl_collection(
    config_path: Path,
    data_dir: Path,
    *,
    transport: httpx.BaseTransport | None = None,
    rpc_url_override: str | None = None,
    sleep=None,
    deadline: float | None = None,
    store_bodies: bool = True,
    budget_used: int = 0,
) -> dict[str, Any]:
    """Collect one interval of v3 or v4 concentrated-liquidity pools. Same resumability contract as
    ``historical.run_collection``: every chunk and every scan cursor is checkpointed."""
    import os
    import time as _time

    cfg = load_config(config_path)
    protocol = str(cfg["protocol"])
    if protocol not in CL_PROTOCOLS:
        raise CollectionBlocked(f"protocol {protocol!r} is not a concentrated-liquidity protocol")
    out_dir = Path(cfg["out_dir"]) if Path(cfg["out_dir"]).is_absolute() else data_dir / cfg["out_dir"]
    work = out_dir.parent / (out_dir.name + "_work")
    work.mkdir(parents=True, exist_ok=True)
    rpc_url = rpc_url_override or os.environ.get(cfg["rpc_url_env"])
    dep = DEPLOYMENTS.get(protocol, {}).get(cfg["chain"])
    budget = Budget(max_requests=int(cfg["max_requests"]), requests=int(budget_used))
    receipts = ReceiptStore(work / "receipts", store_bodies=store_bodies)

    progress = {"chunks": 0}

    def slice_check() -> None:
        # A slice always completes at least one chunk, so short slices still make progress.
        if deadline is not None and progress["chunks"] > 0 and _time.monotonic() >= deadline:
            raise TimeSliceExpired("time slice expired; checkpoints saved")

    http = HttpCollector(provider="evm_rpc", budget=budget, receipts=receipts, errors_path=work / "errors.jsonl", transport=transport)
    if sleep is not None:
        http.sleep = sleep
    rpc = RpcClient(rpc_url or "", http)
    ck = Checkpoints(work / "checkpoints.json")
    ledger = CoverageLedger(work / "coverage_ledger.json")
    log: list[str] = ck.get("decision_log", [])
    result: dict[str, Any] = {"status": "in_progress", "work_dir": str(work), "budget": budget.as_dict()}

    def note(s: str) -> None:
        log.append(s)
        ck.set("decision_log", log)

    try:
        if not rpc_url:
            raise CollectionBlocked(f"environment variable {cfg['rpc_url_env']} is not set; no endpoint is configured (nothing was requested)")
        if dep is None:
            raise CollectionBlocked(f"no versioned deployment reference for {protocol} on {cfg['chain']}")
        is_v4 = protocol == "uniswap_v4"
        emitter = (dep["pool_manager"] if is_v4 else dep["factory"]).lower()
        # 1. Chain and contract verification
        if ck.get("chain_verified") is None:
            cid = rpc.chain_id()
            if cid != dep["chain_id"]:
                raise CollectionBlocked(f"endpoint chain id {cid} != expected {dep['chain_id']}")
            fac = rpc.verify_pool_manager(dep["pool_manager"]) if is_v4 else rpc.verify_v3_factory(dep["factory"])
            if not fac.get("interface_ok"):
                raise CollectionBlocked(f"{'PoolManager' if is_v4 else 'factory'} interface check failed: {fac}")
            ck.set("chain_verified", {"chain_id": cid, "contract": fac})
            note(f"chain id {cid} and {'PoolManager' if is_v4 else 'v3 factory'} interface verified against configured deployment reference")
        # 2. Fixed interval and cohort (no returns consulted)
        p_start, p_end = iso_ms(cfg["period_start_utc"]), iso_ms(cfg["period_end_utc"])
        d_start = iso_ms(cfg["discovery_window_start_utc"])
        pre_ms = int(cfg.get("prehistory_hours", 1)) * 3_600_000
        # 3. Block boundaries
        if ck.get("blocks") is None:
            latest = rpc.block_number()
            latest_ts = rpc.block_timestamp_ms(latest)
            if latest_ts is None or latest_ts < p_end:
                raise CollectionBlocked("period end is not yet finalized on this endpoint")
            est_span = (latest_ts - d_start) // BLOCK_INTERVAL_MS + 5000
            lo = max(0, latest - est_span)
            b_d, _ = rpc.find_block_at_or_after(d_start, lo, latest)
            b_s, ts_s = rpc.find_block_at_or_after(p_start, b_d, latest)
            b_e, _ = rpc.find_block_at_or_after(p_end, b_s, latest)
            b_pre, _ = rpc.find_block_at_or_after(p_start - pre_ms, b_d, latest)
            ck.set("blocks", {"discovery_start": b_d, "prehistory_start": b_pre, "period_start": b_s, "period_start_ts": ts_s, "period_end": b_e})
            note(f"block boundaries fixed: discovery {b_d}, prehistory {b_pre}, period [{b_s}, {b_e})")
        blocks = ck.get("blocks")
        anchor_block, anchor_ts = blocks["period_start"], blocks["period_start_ts"]

        def block_time_ms(b: int) -> int:
            return anchor_ts + (b - anchor_block) * BLOCK_INTERVAL_MS

        # Verify fixed interval with sampled headers
        if ck.get("interval_check") is None:
            samples = [blocks["discovery_start"], blocks["prehistory_start"], blocks["period_end"] - 1]
            mism = []
            for b in samples:
                ts = rpc.block_timestamp_ms(b)
                if ts is not None and ts != block_time_ms(b):
                    mism.append({"block": b, "header_ms": ts, "modeled_ms": block_time_ms(b)})
            ck.set("interval_check", {"samples": samples, "mismatches": mism})
            note(f"block interval {BLOCK_INTERVAL_MS} ms verified on {len(samples)} sampled headers; mismatches={len(mism)}")
        interval_check = ck.get("interval_check")
        if interval_check["mismatches"]:
            raise CollectionBlocked(f"fixed block interval assumption failed on sampled headers: {interval_check['mismatches']}")
        chunk = int(cfg.get("log_chunk_blocks", 2000))

        def scan(ref: str, field_name: str, key: str, *, address: str | list[str] | None, topics: list[Any], start: int, end_exclusive: int) -> list[dict[str, Any]]:
            """Forward log scan over [start, end_exclusive), chunked, halved on provider error, checkpointed per chunk."""
            cur = int(ck.get(key + ":cursor", start))
            logs_file = LogFile(work / "logs" / f"{key.replace(':', '_')}.jsonl")
            legacy = ck.get(key + ":logs")
            if legacy is not None:  # work directories written before logs moved out of the checkpoint file
                logs_file.replace(legacy)
                del ck.data[key + ":logs"]
                ck.flush()
            c = chunk
            while cur < end_exclusive:
                slice_check()
                to_b = min(cur + c - 1, end_exclusive - 1)
                ledger.set(ref, field_name, cur, to_b, CoverageState.PENDING, "requested")
                try:
                    logs = rpc.get_logs(address=address, topics=topics, from_block=cur, to_block=to_b)
                except ProviderError as e:
                    ledger.set(ref, field_name, cur, to_b, CoverageState.FAILED, str(e))
                    if c > 100:
                        c //= 2
                        note(f"{field_name} chunk reduced to {c} after provider error")
                        continue
                    raise
                logs_file.append(logs)
                ledger.set(ref, field_name, cur, to_b, CoverageState.COMPLETED_AND_CHECKED, f"{len(logs)} logs; range returned without provider truncation error")
                cur = to_b + 1
                ck.set(key + ":cursor", cur)
                progress["chunks"] += 1
            return logs_file.read()

        # 4. Pool creation / initialization logs over the discovery window, chunked and checkpointed
        pools: dict[str, dict[str, Any]] = {}
        if is_v4:
            for lg in scan("pool_manager", "initialize", "discovery", address=emitter, topics=[TOPIC_V4_INITIALIZE], start=blocks["discovery_start"], end_exclusive=blocks["period_end"]):
                pid = lg["topics"][1].lower()
                d = lg["data"]
                pools[pid] = {"token0": topic_address(lg["topics"][2]), "token1": topic_address(lg["topics"][3]), "fee": word(d, 0), "tick_spacing": sword(d, 1), "hooks": "0x" + d[2:][2 * 64 + 24 : 3 * 64].lower(), "created_block": hex_to_int(lg["blockNumber"]), "tx": lg.get("transactionHash"), "init_log": lg}
        else:
            for lg in scan("factory", "pool_created", "discovery", address=emitter, topics=[TOPIC_V3_POOL_CREATED], start=blocks["discovery_start"], end_exclusive=blocks["period_end"]):
                d = lg["data"]
                pool = "0x" + d[2:][1 * 64 + 24 : 2 * 64].lower()
                pools[pool] = {"token0": topic_address(lg["topics"][1]), "token1": topic_address(lg["topics"][2]), "fee": hex_to_int(lg["topics"][3]), "tick_spacing": sword(d, 0), "hooks": None, "created_block": hex_to_int(lg["blockNumber"]), "tx": lg.get("transactionHash")}
        # 5. Scope selection: wrapped-native (or native ETH) legs, optional hook allowlist, earliest created first,
        #    frozen before any flow is read
        wn = dep["wrapped_native"].lower()
        native = str(dep.get("native_currency", "")).lower() or None
        quote_legs = {wn} | ({native} if native else set())
        hooks_allow = {str(h).lower() for h in (cfg.get("hooks_allowlist") or [])}
        candidates = sorted(pools.items(), key=lambda kv: (kv[1]["created_block"], kv[0]))
        excluded: list[dict[str, Any]] = []
        in_scope: list[tuple[str, dict[str, Any]]] = []
        for a, m in candidates:
            if not quote_legs & {m["token0"], m["token1"]}:
                excluded.append({"pool": a, "reason": "no wrapped-native leg"})
            elif hooks_allow and (m["hooks"] or "") not in hooks_allow:
                excluded.append({"pool": a, "reason": f"hooks {m['hooks']} not in configured allowlist"})
            else:
                in_scope.append((a, m))
        max_pairs = int(cfg.get("max_pairs", 3))
        rule = str(cfg.get("selection_rule", "earliest_created_wrapped_native_pairs_v1"))
        inactive_out: list[dict[str, Any]] = []
        if rule == "active_before_window_earliest_created_v1":
            # Activity strictly before the prehistory window is a fact known at the window start; it never
            # depends on what happens during the period. One Swap scan over the discovery range.
            act_key = "active_before_window"
            active = ck.get(act_key)
            if active is None:
                ids = [a for a, _ in in_scope]
                lookback = int(cfg.get("activity_lookback_blocks", 0))
                first = blocks["discovery_start"] if lookback <= 0 else max(blocks["discovery_start"], blocks["prehistory_start"] - lookback)
                found: set[str] = set()
                # Providers cap the request body, not just the block range: thousands of pool ids in one
                # filter come back as HTTP 413. Scan the id list in batches, each checkpointed on its own.
                for bi in range(0, len(ids), ACTIVITY_BATCH):
                    batch = ids[bi : bi + ACTIVITY_BATCH]
                    key = f"{act_key}:b{bi // ACTIVITY_BATCH}"
                    if is_v4:
                        logs = scan("pool_manager", "activity", key, address=emitter, topics=[TOPIC_V4_SWAP, batch], start=first, end_exclusive=blocks["prehistory_start"])
                        found |= {lg["topics"][1].lower() for lg in logs}
                    else:
                        logs = scan("factory", "activity", key, address=batch, topics=[TOPIC_V3_SWAP], start=first, end_exclusive=blocks["prehistory_start"])
                        found |= {lg["address"].lower() for lg in logs}
                active = sorted(found)
                ck.set(act_key, active)
                note(f"activity scan before the window (lookback_blocks={lookback or 'full discovery range'}): {len(active)} of {len(in_scope)} in-scope pools had at least one swap before prehistory start")
            active_set = set(active)
            inactive_out = [{"pool": a, "reason": "no swap observed before the window start under the frozen activity rule"} for a, _ in in_scope if a not in active_set]
            in_scope = [(a, m) for a, m in in_scope if a in active_set]
        selected = in_scope[:max_pairs]
        sampled_out = [{"pool": a, "reason": f"beyond max_pairs under frozen rule {rule}"} for a, _ in in_scope[max_pairs:]] + inactive_out
        if ck.get("selection") is None:
            ck.set("selection", {"candidates": len(candidates), "in_scope": len(in_scope), "selected": [a for a, _ in selected], "rule": rule})
            note(f"universe frozen: {len(candidates)} candidates, {len(in_scope)} in scope after rule {rule}, {len(selected)} selected")
        # 6. Events and initial state per selected pool
        assets: dict[str, dict[str, Any]] = ck.get("assets", {})
        pools_out: list[dict[str, Any]] = []
        tape: list[dict[str, Any]] = []
        missing: list[dict[str, Any]] = []
        native_pools: list[str] = []
        delay_ms = int(cfg.get("availability_delay_ms", 4000))
        for addr, meta in selected:
            pool_key = f"{dep['chain_id']}:{protocol}:{addr}"
            legs = []
            for tok in (meta["token0"], meta["token1"]):
                if native is not None and tok == native:
                    # Native ETH is the wrapped-native numeraire for pricing purposes; recorded in the inventory.
                    if pool_key not in native_pools:
                        native_pools.append(pool_key)
                    tok = wn
                    if tok not in assets:
                        assets[tok] = {"decimals": 18, "basis": "native_currency_recorded_as_wrapped_native"}
                        ck.set("assets", assets)
                legs.append(tok)
                if tok not in assets:
                    try:
                        dec = hex_to_int(rpc.eth_call(tok, SEL_DECIMALS))
                    except (ProviderError, ValueError):
                        dec = None
                    assets[tok] = {"decimals": dec, "basis": "eth_call decimals()" if dec is not None else "unknown"}
                    ck.set("assets", assets)
            if assets[legs[0]]["decimals"] is None or assets[legs[1]]["decimals"] is None:
                missing.append({"pool": pool_key, "reason": "token decimals unavailable"})
                continue
            pk = f"poollogs:{addr}"
            pre_start = blocks["prehistory_start"]
            # Liquidity events from creation (the tick map needs every one), swaps only from the prehistory start.
            if is_v4:
                liq_logs = scan(pool_key, "liquidity", pk + ":liq", address=emitter, topics=[[TOPIC_V4_MODIFY_LIQUIDITY], addr], start=meta["created_block"], end_exclusive=blocks["period_end"])
                swap_logs = scan(pool_key, "swaps", pk + ":swaps", address=emitter, topics=[[TOPIC_V4_SWAP], addr], start=max(meta["created_block"], pre_start), end_exclusive=blocks["period_end"])
                raw_logs = [meta["init_log"]] + liq_logs + swap_logs
            else:
                liq_logs = scan(pool_key, "liquidity", pk + ":liq", address=addr, topics=[[TOPIC_V3_INITIALIZE, TOPIC_V3_MINT, TOPIC_V3_BURN]], start=meta["created_block"], end_exclusive=blocks["period_end"])
                swap_logs = scan(pool_key, "swaps", pk + ":swaps", address=addr, topics=[[TOPIC_V3_SWAP]], start=max(meta["created_block"], pre_start), end_exclusive=blocks["period_end"])
                raw_logs = liq_logs + swap_logs
            rows = normalize_cl_logs(raw_logs, protocol=protocol, pool_key=pool_key, fee_pips=int(meta["fee"]), block_time_ms=block_time_ms)
            # v4 pools may carry the dynamic-fee flag (0x800000) instead of a fee: the hook sets the fee per swap
            # and every Swap event reports it. The pool's own fee for agent fills is then the last fee observed
            # before the window (or the first one inside it); the basis is recorded on the pool.
            pool_fee = int(meta["fee"])
            fee_basis = "pool_fee_from_initialize"
            if pool_fee & DYNAMIC_FEE_FLAG:
                observed = [(r["block"], int(r["fee_pips"])) for r in rows if r["kind"] == "cl_swap" and r.get("fee_pips") is not None]
                before = [f for b, f in observed if b < pre_start]
                if before:
                    pool_fee, fee_basis = before[-1], "dynamic_hook_fee_last_observed_before_window"
                elif observed:
                    pool_fee, fee_basis = observed[0][1], "dynamic_hook_fee_first_observed_inside_window"
                else:
                    pool_fee, fee_basis = 0, "dynamic_hook_fee_never_observed"
                if not (0 <= pool_fee < FEE_DENOMINATOR_PIPS):
                    pool_fee, fee_basis = FEE_DENOMINATOR_PIPS - 1, "dynamic_hook_fee_out_of_range_clamped"
            init_rows = [r for r in rows if r["kind"] == "cl_init"]
            init_block = init_rows[0]["block"] if init_rows else None
            # Initial state at the block before the prehistory window (pools initialized inside the window start from their cl_init row).
            state_block = pre_start - 1
            init_sqrt: str | None = None
            init_tick: int | None = None
            init_liq: str | None = None
            init_ticks: list[list[str]] = []
            init_basis: str
            if init_block is None:
                init_basis = "pool_created_but_not_initialized"
                missing.append({"pool": pool_key, "reason": "no Initialize event observed since creation"})
            elif init_block >= pre_start:
                init_basis = "pool_initialized_inside_window"
            else:
                init_ticks = fold_tick_map([r for r in rows if r["kind"] == "cl_modify" and r["block"] < pre_start])
                cached = ck.get(pk + ":init")
                if cached:
                    init_sqrt, init_tick, price_basis = cached["sqrt_price_x96"], cached["tick"], cached["basis"]
                else:
                    price_basis = None
                    if not is_v4:
                        try:
                            res = rpc.eth_call(addr, SEL_SLOT0, state_block)
                            init_sqrt, init_tick, price_basis = str(word(res, 0)), sword(res, 1), f"slot0_at_block_{state_block}"
                            note(f"{addr}: initial price from slot0 at block {state_block}")
                        except (ProviderError, ValueError) as e:
                            note(f"{addr}: slot0 at {state_block} unavailable ({str(e)[:80]}); scanning back for the last Swap")
                    if price_basis is None:
                        # Backward scan for the last Swap before the window, down to the Initialize block; cursor checkpointed.
                        hi = int(ck.get(pk + ":lastswap:hi", state_block))
                        lchunk = chunk
                        last_swap = ck.get(pk + ":lastswap")
                        while hi >= init_block and last_swap is None:
                            slice_check()
                            fb = max(init_block, hi - lchunk + 1)
                            try:
                                if is_v4:
                                    swaps = rpc.get_logs(address=emitter, topics=[[TOPIC_V4_SWAP], addr], from_block=fb, to_block=hi)
                                else:
                                    swaps = rpc.get_logs(address=addr, topics=[[TOPIC_V3_SWAP]], from_block=fb, to_block=hi)
                            except ProviderError:
                                if lchunk > 100:
                                    lchunk //= 2
                                    note(f"last-swap scan chunk reduced to {lchunk} after provider error")
                                    continue
                                raise
                            if swaps:
                                last = max(swaps, key=lambda l: (hex_to_int(l["blockNumber"]), hex_to_int(l["logIndex"])))
                                # Both Swap layouts carry sqrtPriceX96 at word 2 and tick at word 4.
                                last_swap = {"sqrt_price_x96": str(word(last["data"], 2)), "tick": sword(last["data"], 4), "block": hex_to_int(last["blockNumber"])}
                                ck.set(pk + ":lastswap", last_swap)
                            hi = fb - 1
                            ck.set(pk + ":lastswap:hi", hi)
                            progress["chunks"] += 1
                        if last_swap is not None:
                            init_sqrt, init_tick, price_basis = last_swap["sqrt_price_x96"], int(last_swap["tick"]), f"last_swap_before_prehistory_at_block_{last_swap['block']}"
                        else:
                            init_sqrt, init_tick, price_basis = init_rows[0]["sqrt_price_x96"], int(init_rows[0]["tick"]), f"initialize_at_block_{init_block}_no_swap_before_prehistory"
                        note(f"{addr}: initial price basis {price_basis}")
                    ck.set(pk + ":init", {"sqrt_price_x96": init_sqrt, "tick": init_tick, "basis": price_basis})
                init_liq = str(active_liquidity(init_ticks, int(init_tick)))
                init_basis = f"{price_basis}; tick_map_and_liquidity_folded_from_liquidity_events_through_block_{state_block}"
            rows = [r for r in rows if r["block"] >= pre_start]
            for r in rows:
                r["available_utc_ms"] = r["time_utc_ms"] + delay_ms
            tape.extend(rows)
            supported = init_block is not None
            pools_out.append(
                {
                    "key": pool_key,
                    "chain_id": dep["chain_id"],
                    "protocol": protocol,
                    "address": addr,
                    "model": str(CL_MODELS[protocol]),
                    "asset0": f"{dep['chain_id']}:{legs[0]}",
                    "asset1": f"{dep['chain_id']}:{legs[1]}",
                    "fee_numerator": FEE_DENOMINATOR_PIPS - pool_fee,
                    "fee_denominator": FEE_DENOMINATOR_PIPS,
                    "fee_pips": pool_fee,
                    "fee_basis": fee_basis,
                    "tick_spacing": int(meta["tick_spacing"]),
                    "hooks": meta["hooks"],
                    "created_block": meta["created_block"],
                    "created_time_utc_ms": block_time_ms(meta["created_block"]),
                    "discovery_available_utc_ms": block_time_ms(meta["created_block"]) + delay_ms,
                    "initial_reserve0": None,
                    "initial_reserve1": None,
                    "initial_sqrt_price_x96": init_sqrt,
                    "initial_tick": init_tick,
                    "initial_liquidity": init_liq,
                    "initial_ticks": init_ticks,
                    "initial_state_block": state_block if init_sqrt is not None else None,
                    "initial_state_basis": init_basis,
                    "factory": emitter,
                    "supported_by_cpmm": False,
                    "supported_by_clmm": supported,
                    "unsupported_reason": None if supported else "pool never initialized",
                }
            )
        # 7-10. Build pack. Pools are flagged supported_by_clmm only: the CLMM adapter serves them and the
        # validator reconciles every cl_swap against the v3 swap loop (sqrt price, liquidity, tick).
        if ck.get("clmm_note") is None:
            note("pools are supported_by_clmm=True and supported_by_cpmm=False: served by the CLMM adapter; the validator replays every cl_swap as a reconciliation checkpoint (sqrt price, liquidity, tick) and applies cl_modify/cl_init rows")
            ck.set("clmm_note", True)
        tape.sort(key=lambda r: (r["block"], r["log_index"], r["seq"]))
        for i, r in enumerate(tape, start=1):
            r["seq"] = i
        assets_rows = []
        for tok, a in assets.items():
            assets_rows.append({"key": f"{dep['chain_id']}:{tok}", "chain_id": dep["chain_id"], "address": tok, "decimals": a["decimals"] if a["decimals"] is not None else 18, "symbol": None, "name": None, "is_numeraire": tok == wn, "created_block": None, "created_time_utc_ms": None, "discovery_available_utc_ms": block_time_ms(blocks["discovery_start"]), "fixture_rules": {"decimals_basis": a.get("basis", "unknown")}})
        if not any(r["is_numeraire"] for r in assets_rows):
            assets_rows.append({"key": f"{dep['chain_id']}:{wn}", "chain_id": dep["chain_id"], "address": wn, "decimals": 18, "symbol": "WETH", "name": None, "is_numeraire": True, "created_block": None, "created_time_utc_ms": None, "discovery_available_utc_ms": block_time_ms(blocks["discovery_start"]), "fixture_rules": {}})
        cov_intervals = []
        for p in pools_out:
            if p["supported_by_clmm"]:
                cov_intervals.append({"object_ref": p["key"], "field": "swaps", "start_utc_ms": block_time_ms(blocks["prehistory_start"]), "end_utc_ms": block_time_ms(blocks["period_end"]), "state": "completed_and_checked" if not ledger.pending(p["key"], "swaps") else "partial", "evidence": "eth_getLogs ranges returned without truncation error; provider indexing completeness not independently established"})
                cov_intervals.append({"object_ref": p["key"], "field": "liquidity", "start_utc_ms": p["created_time_utc_ms"], "end_utc_ms": block_time_ms(blocks["period_end"]), "state": "completed_and_checked" if not ledger.pending(p["key"], "liquidity") else "partial", "evidence": "liquidity events fetched from pool creation so the tick map at the window start is complete"})
        coverage = {"schema": "coverage_v1", "basis": "evm_rpc_logs", "intervals": cov_intervals, "ledger": ledger.rows, "interval_check": interval_check, "budget": budget.as_dict(), "unsupported_events": []}
        from ..datasets.builder import build_pack, make_period
        from ..datasets.execution_params import historical_research_params

        params = historical_research_params(block_interval_ms=BLOCK_INTERVAL_MS, availability_delay_ms=delay_ms)
        pack = build_pack(
            out_dir,
            origin=DataOrigin.HISTORICAL_RECONSTRUCTION,
            chain=cfg["chain"],
            chain_id=dep["chain_id"],
            scope_label="base_uniswap_cl_native_pairs_research_v1",
            title_private=f"Base {protocol} concentrated-liquidity research slice {cfg['period_start_utc']}..{cfg['period_end_utc']}",
            period=make_period(block_time_ms(blocks["period_start"]), block_time_ms(blocks["period_end"]), block_time_ms(blocks["prehistory_start"])),
            universe=Universe(
                factories=[emitter],
                pool_models=[CL_MODELS[protocol]],
                quote_asset=f"{dep['chain_id']}:{wn}",
                selection_rule_version=rule,
                indexed_block_ranges=[[blocks["discovery_start"], blocks["period_end"] - 1]],
                excluded_or_unsupported_counts={"no_wrapped_native_leg_or_hooks": len(excluded), "beyond_max_pairs": len(sampled_out), "missing_state": len(missing)},
                candidate_count=len(candidates),
                selected_count=len(selected),
                unsupported_count=len(excluded),
                missing_count=len(missing),
                description=f"{'PoolManager Initialize' if is_v4 else 'factory PoolCreated'} cohort from the declared discovery window; wrapped-native (or native ETH) legs only{'; hooks allowlist applied' if hooks_allow else ''}; sampling rule {rule} frozen before period flows were read.",
            ),
            assets=assets_rows,
            pools=pools_out,
            tape=tape,
            params=params,
            coverage=coverage,
            numeraire=f"{dep['chain_id']}:{wn}",
            numeraire_alias="NATIVE",
            numeraire_decimals=18,
            token_behavior=TokenBehavior.ASSUMED_STANDARD_TRANSFER,
            availability_model={"kind": "constant_delay_from_block_time", "delay_ms": delay_ms, "acquisition_utc_ms": now_ms(), "note": "acquired later than the events; original provider availability not established"},
            rights=Rights(storage_basis="public_chain_data_via_configured_rpc; endpoint terms not reviewed here", local_processing_basis="research", redistribution="not_cleared", simulator_serving="local_only", notes=cfg["authorization_note"]),
            qualification=UseStatus.RESEARCH,
            inventory={"unsupported": excluded, "excluded_by_sampling": sampled_out, "missing": missing, "native_currency_pools": native_pools, "candidate_count": len(candidates), "selected_count": len(selected)},
            provenance_notes=[
                f"HISTORICAL RECONSTRUCTION of {protocol} concentrated-liquidity pools from eth_getLogs. Token sellability/restrictions unknown (assumed standard transfer). Not a full week unless the period says so.",
                "cl_swap amounts are pool deltas (positive = paid into the pool); v4 user deltas were negated to match the v3 convention.",
            ],
            decision_log=log,
        )
        result.update({"status": "pack_built", "pack_id": pack.pack_id, "pack_dir": str(out_dir), "qualification": pack.validation["resulting_qualification"], "tape_events": len(tape), "pools": len(pools_out), "missing": missing, "unsupported_events": 0, "budget": budget.as_dict(), "decision_log": log})
        return result
    except TimeSliceExpired as e:
        result.update({"status": "in_progress_resumable", "reason": str(e), "budget": budget.as_dict(), "checkpoints": ck.data.get("blocks"), "decision_log": log})
        return result
    except BudgetExhausted as e:
        result.update({"status": "budget_exhausted_resumable", "reason": str(e), "budget": budget.as_dict(), "checkpoints": ck.data.get("blocks"), "decision_log": log})
        return result
    except CollectionBlocked as e:
        result.update({"status": "blocked", "reason": str(e), "budget": budget.as_dict(), "decision_log": log})
        return result
    except ProviderError as e:
        result.update({"status": "provider_error_resumable", "reason": str(e), "budget": budget.as_dict(), "decision_log": log})
        return result
    finally:
        (work / "last_result.json").write_text(json.dumps(result, indent=2, sort_keys=True, default=str))
