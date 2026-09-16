"""Historical Base / Uniswap v2 connector (Section 9 algorithm) and tape normalizer.

Opt-in and read-only. Requires an explicitly configured RPC endpoint and request budget.
Produces either an immutable ``historical_reconstruction`` research pack or a precise
rejection reason. Never fabricates state from candles and never rewrites acquisition
timestamps to look contemporaneous.

Config (YAML)::

    rpc_url_env: BASE_RPC_URL          # name of the env var holding the endpoint (never the URL itself)
    chain: base
    protocol: uniswap_v2
    period_start_utc: "2026-09-07T00:00:00Z"
    period_end_utc:   "2026-09-07T01:00:00Z"
    discovery_window_start_utc: "2026-09-06T00:00:00Z"
    prehistory_hours: 1
    max_pairs: 3
    max_requests: 400
    log_chunk_blocks: 2000
    out_dir: data/packs/historical/base_v2_slice_1
    authorization_note: "who authorized this read-only collection and under which terms"
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

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
    SEL_GET_RESERVES,
    TOPIC_BURN,
    TOPIC_MINT,
    TOPIC_PAIR_CREATED,
    TOPIC_SWAP,
    TOPIC_SYNC,
    RpcClient,
    hex_to_int,
    topic_address,
    word,
)

DEPLOYMENTS = yaml.safe_load((Path(__file__).parent / "deployments.yaml").read_text())
BLOCK_INTERVAL_MS = 2000


def iso_ms(s: str) -> int:
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)


class CollectionBlocked(RuntimeError):
    """Raised with the exact unmet prerequisite."""


def load_config(path: Path) -> dict[str, Any]:
    cfg = yaml.safe_load(path.read_text())
    required = ["rpc_url_env", "chain", "protocol", "period_start_utc", "period_end_utc", "discovery_window_start_utc", "max_requests", "out_dir", "authorization_note"]
    missing = [k for k in required if k not in cfg]
    if missing:
        raise CollectionBlocked(f"collection config missing required keys: {missing}")
    if "rpc_url" in cfg:
        raise CollectionBlocked("put the endpoint in an environment variable named by rpc_url_env; never in the config file")
    return cfg


# ---------------------------------------------------------------------- normalizer
def normalize_pair_logs(logs: list[dict[str, Any]], *, pool_key: str, token0: str, token1: str, block_time_ms) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Turn raw Swap/Mint/Burn/Sync logs into primitive tape rows.

    Uniswap v2 emits ``Sync`` *before* the economic event in the same transaction. The Sync is a
    checkpoint of the state after that event, so it is re-ordered to follow its partner and is
    never applied as an economic action. Unsupported shapes (two inputs, two outputs) are
    reported and excluded.
    """
    rows: list[dict[str, Any]] = []
    unsupported: list[dict[str, Any]] = []
    ordered = sorted(logs, key=lambda l: (hex_to_int(l["blockNumber"]), hex_to_int(l["logIndex"])))
    pending_sync: dict[str, Any] | None = None
    seq = 0
    for lg in ordered:
        topic0 = lg["topics"][0].lower()
        block = hex_to_int(lg["blockNumber"])
        log_index = hex_to_int(lg["logIndex"])
        tx = lg.get("transactionHash")
        base = {"block": block, "time_utc_ms": block_time_ms(block), "pool": pool_key, "tx": tx, "available_utc_ms": None, "availability_basis": str(AvailabilityBasis.RECONSTRUCTED_WITH_DELAY_MODEL), "received_utc_ms": now_ms()}
        if topic0 == TOPIC_SYNC:
            pending_sync = {**base, "kind": "sync", "log_index": log_index, "reserve0": str(word(lg["data"], 0)), "reserve1": str(word(lg["data"], 1))}
            continue
        econ: dict[str, Any] | None = None
        if topic0 == TOPIC_SWAP:
            a0in, a1in, a0out, a1out = (word(lg["data"], i) for i in range(4))
            sender = topic_address(lg["topics"][1]) if len(lg["topics"]) > 1 else None
            if (a0in > 0 and a1in > 0) or (a0out > 0 and a1out > 0) or (a0in == 0 and a1in == 0):
                unsupported.append({"block": block, "log_index": log_index, "tx": tx, "reason": "multi-input or multi-output swap shape is not a supported primitive"})
                pending_sync = None
                continue
            if a0in > 0:
                econ = {**base, "kind": "swap", "log_index": log_index, "asset_in": token0, "amount_in": str(a0in), "amount_out_recorded": str(a1out), "wallet": sender}
            else:
                econ = {**base, "kind": "swap", "log_index": log_index, "asset_in": token1, "amount_in": str(a1in), "amount_out_recorded": str(a0out), "wallet": sender}
        elif topic0 == TOPIC_MINT:
            econ = {**base, "kind": "mint", "log_index": log_index, "amount0": str(word(lg["data"], 0)), "amount1": str(word(lg["data"], 1)), "wallet": topic_address(lg["topics"][1]) if len(lg["topics"]) > 1 else None}
        elif topic0 == TOPIC_BURN:
            econ = {**base, "kind": "burn", "log_index": log_index, "amount0": str(word(lg["data"], 0)), "amount1": str(word(lg["data"], 1)), "wallet": topic_address(lg["topics"][1]) if len(lg["topics"]) > 1 else None}
        else:
            continue
        seq += 1
        econ["seq"] = seq
        rows.append(econ)
        if pending_sync is not None and pending_sync["block"] == block and pending_sync["tx"] == tx:
            seq += 1
            # Place the checkpoint immediately after the economic event it describes.
            sync_row = {**pending_sync, "seq": seq, "log_index": log_index}
            econ["log_index"] = pending_sync["log_index"]  # keep original relative order: econ takes the sync's slot
            rows.append(sync_row)
        pending_sync = None
    return rows, unsupported


# ---------------------------------------------------------------------- pipeline
def run_collection(config_path: Path, data_dir: Path, *, transport: httpx.BaseTransport | None = None, rpc_url_override: str | None = None, sleep=None) -> dict[str, Any]:
    import os

    cfg = load_config(config_path)
    out_dir = Path(cfg["out_dir"]) if Path(cfg["out_dir"]).is_absolute() else data_dir / cfg["out_dir"]
    work = out_dir.parent / (out_dir.name + "_work")
    work.mkdir(parents=True, exist_ok=True)
    rpc_url = rpc_url_override or os.environ.get(cfg["rpc_url_env"])
    dep = DEPLOYMENTS.get(cfg["protocol"], {}).get(cfg["chain"])
    budget = Budget(max_requests=int(cfg["max_requests"]))
    receipts = ReceiptStore(work / "receipts")
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
            raise CollectionBlocked(f"no versioned deployment reference for {cfg['protocol']} on {cfg['chain']}")
        # 1. Chain and factory verification
        if ck.get("chain_verified") is None:
            cid = rpc.chain_id()
            if cid != dep["chain_id"]:
                raise CollectionBlocked(f"endpoint chain id {cid} != expected {dep['chain_id']}")
            fac = rpc.verify_factory(dep["factory"])
            if not fac.get("interface_ok"):
                raise CollectionBlocked(f"factory interface check failed: {fac}")
            ck.set("chain_verified", {"chain_id": cid, "factory": fac})
            note(f"chain id {cid} and factory interface verified against configured deployment reference")
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
        # 4. Pool creation logs, chunked and checkpointed
        chunk = int(cfg.get("log_chunk_blocks", 2000))
        cursor = ck.get("pair_cursor", blocks["discovery_start"])
        pairs: dict[str, dict[str, Any]] = ck.get("pairs", {})
        while cursor < blocks["period_end"]:
            to_b = min(cursor + chunk - 1, blocks["period_end"] - 1)
            ledger.set("factory", "pair_created", cursor, to_b, CoverageState.PENDING, "requested")
            try:
                logs = rpc.get_logs(address=dep["factory"], topics=[TOPIC_PAIR_CREATED], from_block=cursor, to_block=to_b)
            except ProviderError as e:
                ledger.set("factory", "pair_created", cursor, to_b, CoverageState.FAILED, str(e))
                if chunk > 100:
                    chunk //= 2
                    note(f"pair_created chunk reduced to {chunk} after provider error")
                    continue
                raise
            for lg in logs:
                t0, t1 = topic_address(lg["topics"][1]), topic_address(lg["topics"][2])
                pair = "0x" + lg["data"][2:][24:64].lower()
                pairs[pair] = {"token0": t0, "token1": t1, "created_block": hex_to_int(lg["blockNumber"]), "tx": lg.get("transactionHash")}
            ledger.set("factory", "pair_created", cursor, to_b, CoverageState.COMPLETED_AND_CHECKED, f"{len(logs)} logs; range returned without provider truncation error")
            cursor = to_b + 1
            ck.set("pair_cursor", cursor)
            ck.set("pairs", pairs)
        # 5. Scope selection: wrapped-native pairs, earliest created first, frozen before any flow is read
        wn = dep["wrapped_native"].lower()
        candidates = sorted(pairs.items(), key=lambda kv: (kv[1]["created_block"], kv[0]))
        in_scope = [(a, m) for a, m in candidates if wn in (m["token0"], m["token1"])]
        excluded = [{"pool": a, "reason": "no wrapped-native leg"} for a, m in candidates if wn not in (m["token0"], m["token1"])]
        max_pairs = int(cfg.get("max_pairs", 3))
        rule = str(cfg.get("selection_rule", "earliest_created_wrapped_native_pairs_v1"))
        inactive_out: list[dict[str, Any]] = []
        if rule == "active_before_window_earliest_created_v1":
            # Activity strictly before the prehistory window is a fact known at the window start; it never
            # depends on what happens during the period. One address-list Swap scan over the discovery range.
            act_key = "active_before_window"
            active = ck.get(act_key)
            if active is None:
                found: set[str] = set()
                addrs = [a for a, _ in in_scope]
                cur_a = blocks["discovery_start"]
                achunk = chunk
                while cur_a < blocks["prehistory_start"] and addrs:
                    to_a = min(cur_a + achunk - 1, blocks["prehistory_start"] - 1)
                    try:
                        logs = rpc.get_logs(address=addrs, topics=[TOPIC_SWAP], from_block=cur_a, to_block=to_a)
                    except ProviderError:
                        if achunk > 100:
                            achunk //= 2
                            continue
                        raise
                    for lg in logs:
                        found.add(lg["address"].lower())
                    cur_a = to_a + 1
                active = sorted(found)
                ck.set(act_key, active)
                note(f"activity scan before the window: {len(active)} of {len(in_scope)} in-scope pairs had at least one swap before prehistory start")
            active_set = set(active)
            inactive_out = [{"pool": a, "reason": "no swap observed before the window start under the frozen activity rule"} for a, _ in in_scope if a not in active_set]
            in_scope = [(a, m) for a, m in in_scope if a in active_set]
        selected = in_scope[:max_pairs]
        sampled_out = [{"pool": a, "reason": f"beyond max_pairs under frozen rule {rule}"} for a, _ in in_scope[max_pairs:]] + inactive_out
        if ck.get("selection") is None:
            ck.set("selection", {"candidates": len(candidates), "in_scope": len(in_scope), "selected": [a for a, _ in selected], "rule": rule})
            note(f"universe frozen: {len(candidates)} candidates, {len(in_scope)} in scope after rule {rule}, {len(selected)} selected")
        # 6. Events and initial state per selected pair
        assets: dict[str, dict[str, Any]] = ck.get("assets", {})
        pools_out: list[dict[str, Any]] = []
        tape: list[dict[str, Any]] = []
        unsupported_events: list[dict[str, Any]] = []
        missing: list[dict[str, Any]] = []
        for addr, meta in selected:
            pool_key = f"{dep['chain_id']}:uniswap_v2:{addr}"
            for tok in (meta["token0"], meta["token1"]):
                if tok not in assets:
                    try:
                        dec = hex_to_int(rpc.eth_call(tok, SEL_DECIMALS))
                    except (ProviderError, ValueError):
                        dec = None
                    assets[tok] = {"decimals": dec}
                    ck.set("assets", assets)
            if assets[meta["token0"]]["decimals"] is None or assets[meta["token1"]]["decimals"] is None:
                missing.append({"pool": pool_key, "reason": "token decimals unavailable"})
                continue
            pk = f"pairlogs:{addr}"
            from_b = max(meta["created_block"], blocks["prehistory_start"])
            cur = ck.get(pk + ":cursor", from_b)
            pair_logs: list[dict[str, Any]] = ck.get(pk + ":logs", [])
            pchunk = chunk
            while cur < blocks["period_end"]:
                to_b = min(cur + pchunk - 1, blocks["period_end"] - 1)
                ledger.set(pool_key, "events", cur, to_b, CoverageState.PENDING, "requested")
                try:
                    logs = rpc.get_logs(address=addr, topics=[[TOPIC_SWAP, TOPIC_MINT, TOPIC_BURN, TOPIC_SYNC]], from_block=cur, to_block=to_b)
                except ProviderError as e:
                    ledger.set(pool_key, "events", cur, to_b, CoverageState.FAILED, str(e))
                    if pchunk > 100:
                        pchunk //= 2
                        continue
                    raise
                pair_logs.extend(logs)
                ledger.set(pool_key, "events", cur, to_b, CoverageState.COMPLETED_AND_CHECKED, f"{len(logs)} logs")
                cur = to_b + 1
                ck.set(pk + ":cursor", cur)
                ck.set(pk + ":logs", pair_logs)
            # Initial state at the block before the prehistory window (pairs created inside the window start empty).
            init = None
            init_basis = None
            state_block = blocks["prehistory_start"] - 1
            if meta["created_block"] >= blocks["prehistory_start"]:
                init, init_basis = ("0", "0"), "pair_created_inside_window"
            else:
                cached = ck.get(pk + ":init")
                if cached:
                    init, init_basis = tuple(cached["reserves"]), cached["basis"]
                else:
                    try:
                        res = rpc.eth_call(addr, SEL_GET_RESERVES, state_block)
                        init, init_basis = (str(word(res, 0)), str(word(res, 1))), "getReserves_at_block_before_prehistory"
                        note(f"{addr}: initial state from getReserves at block {state_block}")
                    except ProviderError as e:
                        # No archive state: scan backwards for the last Sync checkpoint before the window.
                        note(f"{addr}: getReserves at {state_block} unavailable ({str(e)[:80]}); scanning back for the last Sync")
                        lookback = int(cfg.get("initial_state_lookback_blocks", 20_000))
                        lo = max(meta["created_block"], state_block - lookback)
                        hi = state_block
                        found = None
                        lchunk = chunk
                        while hi >= lo and found is None:
                            fb = max(lo, hi - lchunk + 1)
                            try:
                                syncs = rpc.get_logs(address=addr, topics=[TOPIC_SYNC], from_block=fb, to_block=hi)
                            except ProviderError as e2:
                                if lchunk > 100:
                                    lchunk //= 2
                                    continue
                                raise e2
                            if syncs:
                                last = max(syncs, key=lambda l: (hex_to_int(l["blockNumber"]), hex_to_int(l["logIndex"])))
                                found = (str(word(last["data"], 0)), str(word(last["data"], 1)), hex_to_int(last["blockNumber"]))
                            hi = fb - 1
                        if found is not None:
                            init, init_basis = (found[0], found[1]), f"last_sync_before_prehistory_at_block_{found[2]}"
                            # Events between that Sync and the window start must also be replayed: extend the tape backwards.
                            extra_from, extra_to = found[2] + 1, state_block
                            if extra_to >= extra_from:
                                extra_logs: list[dict[str, Any]] = []
                                c2 = extra_from
                                echunk = chunk
                                while c2 <= extra_to:
                                    tb2 = min(c2 + echunk - 1, extra_to)
                                    try:
                                        extra_logs.extend(rpc.get_logs(address=addr, topics=[[TOPIC_SWAP, TOPIC_MINT, TOPIC_BURN, TOPIC_SYNC]], from_block=c2, to_block=tb2))
                                    except ProviderError:
                                        if echunk > 100:
                                            echunk //= 2
                                            continue
                                        raise
                                    c2 = tb2 + 1
                                pair_logs = extra_logs + pair_logs
                                pack_start_override = found[2] + 1
                                blocks["prehistory_start"] = min(blocks["prehistory_start"], pack_start_override)
                                note(f"{addr}: prehistory extended back to block {pack_start_override} to replay from the Sync checkpoint")
                        else:
                            missing.append({"pool": pool_key, "reason": f"initial state unavailable: no archive state and no Sync within {lookback} blocks before the window"})
                    if init is not None:
                        ck.set(pk + ":init", {"reserves": list(init), "basis": init_basis})
            rows, unsup = normalize_pair_logs(pair_logs, pool_key=pool_key, token0=f"{dep['chain_id']}:{meta['token0']}", token1=f"{dep['chain_id']}:{meta['token1']}", block_time_ms=block_time_ms)
            unsupported_events.extend(unsup)
            rows = [r for r in rows if r["block"] >= blocks["prehistory_start"]]
            for r in rows:
                if r["kind"] != "sync":
                    r["available_utc_ms"] = r["time_utc_ms"] + int(cfg.get("availability_delay_ms", 4000))
            tape.extend(rows)
            pools_out.append(
                {
                    "key": pool_key,
                    "chain_id": dep["chain_id"],
                    "protocol": "uniswap_v2",
                    "address": addr,
                    "model": str(PoolModel.UNISWAP_V2_PLAIN),
                    "asset0": f"{dep['chain_id']}:{meta['token0']}",
                    "asset1": f"{dep['chain_id']}:{meta['token1']}",
                    "fee_numerator": dep["fee_numerator"],
                    "fee_denominator": dep["fee_denominator"],
                    "created_block": meta["created_block"],
                    "created_time_utc_ms": block_time_ms(meta["created_block"]),
                    "discovery_available_utc_ms": block_time_ms(meta["created_block"]) + int(cfg.get("availability_delay_ms", 4000)),
                    "initial_reserve0": init[0] if init else None,
                    "initial_reserve1": init[1] if init else None,
                    "initial_state_block": blocks["prehistory_start"] - 1 if init else None,
                    "initial_state_basis": init_basis,
                    "factory": dep["factory"].lower(),
                    "supported_by_cpmm": init is not None,
                    "unsupported_reason": None if init is not None else "initial state unavailable",
                }
            )
        # 7-10. Build pack (validator runs the no-agent reconciliation and decides qualification)
        tape.sort(key=lambda r: (r["block"], r["log_index"], r["seq"]))
        for i, r in enumerate(tape, start=1):
            r["seq"] = i
        assets_rows = []
        for tok, a in assets.items():
            assets_rows.append({"key": f"{dep['chain_id']}:{tok}", "chain_id": dep["chain_id"], "address": tok, "decimals": a["decimals"] if a["decimals"] is not None else 18, "symbol": None, "name": None, "is_numeraire": tok == wn, "created_block": None, "created_time_utc_ms": None, "discovery_available_utc_ms": block_time_ms(blocks["discovery_start"]), "fixture_rules": {"decimals_basis": "eth_call decimals()" if a["decimals"] is not None else "unknown"}})
        if not any(r["is_numeraire"] for r in assets_rows):
            assets_rows.append({"key": f"{dep['chain_id']}:{wn}", "chain_id": dep["chain_id"], "address": wn, "decimals": 18, "symbol": "WETH", "name": None, "is_numeraire": True, "created_block": None, "created_time_utc_ms": None, "discovery_available_utc_ms": block_time_ms(blocks["discovery_start"]), "fixture_rules": {}})
        cov_intervals = []
        for p in pools_out:
            if p["supported_by_cpmm"]:
                cov_intervals.append({"object_ref": p["key"], "field": "swaps", "start_utc_ms": block_time_ms(blocks["prehistory_start"]), "end_utc_ms": block_time_ms(blocks["period_end"]), "state": "completed_and_checked" if not ledger.pending(p["key"], "events") else "partial", "evidence": "eth_getLogs ranges returned without truncation error; provider indexing completeness not independently established"})
        coverage = {"schema": "coverage_v1", "basis": "evm_rpc_logs", "intervals": cov_intervals, "ledger": ledger.rows, "interval_check": interval_check, "budget": budget.as_dict(), "unsupported_events": unsupported_events}
        from ..datasets.builder import build_pack, make_period
        from ..datasets.execution_params import historical_research_params

        params = historical_research_params(block_interval_ms=BLOCK_INTERVAL_MS, availability_delay_ms=int(cfg.get("availability_delay_ms", 4000)))
        pack = build_pack(
            out_dir,
            origin=DataOrigin.HISTORICAL_RECONSTRUCTION,
            chain=cfg["chain"],
            chain_id=dep["chain_id"],
            scope_label="base_uniswap_v2_native_pairs_research_v1",
            title_private=f"Base v2 research slice {cfg['period_start_utc']}..{cfg['period_end_utc']}",
            period=make_period(block_time_ms(blocks["period_start"]), block_time_ms(blocks["period_end"]), block_time_ms(blocks["prehistory_start"])),
            universe=Universe(
                factories=[dep["factory"].lower()],
                pool_models=[PoolModel.UNISWAP_V2_PLAIN],
                quote_asset=f"{dep['chain_id']}:{wn}",
                selection_rule_version=rule,
                indexed_block_ranges=[[blocks["discovery_start"], blocks["period_end"] - 1]],
                excluded_or_unsupported_counts={"no_wrapped_native_leg": len(excluded), "beyond_max_pairs": len(sampled_out), "missing_state": len(missing)},
                candidate_count=len(candidates),
                selected_count=len(selected),
                unsupported_count=len(excluded),
                missing_count=len(missing),
                description=f"PairCreated cohort from the declared discovery window; wrapped-native pairs only; sampling rule {rule} frozen before period flows were read.",
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
            availability_model={"kind": "constant_delay_from_block_time", "delay_ms": int(cfg.get("availability_delay_ms", 4000)), "acquisition_utc_ms": now_ms(), "note": "acquired later than the events; original provider availability not established"},
            rights=Rights(storage_basis="public_chain_data_via_configured_rpc; endpoint terms not reviewed here", local_processing_basis="research", redistribution="not_cleared", simulator_serving="local_only", notes=cfg["authorization_note"]),
            qualification=UseStatus.RESEARCH,
            inventory={"unsupported": excluded, "excluded_by_sampling": sampled_out, "missing": missing, "candidate_count": len(candidates), "selected_count": len(selected)},
            provenance_notes=["HISTORICAL RECONSTRUCTION from eth_getLogs. Token sellability/restrictions unknown (assumed standard transfer). Not a full week unless the period says so."],
            decision_log=log,
        )
        result.update({"status": "pack_built", "pack_id": pack.pack_id, "pack_dir": str(out_dir), "qualification": pack.validation["resulting_qualification"], "tape_events": len(tape), "pools": len(pools_out), "missing": missing, "unsupported_events": len(unsupported_events), "budget": budget.as_dict(), "decision_log": log})
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
