"""Concentrated-liquidity collector (v4 PoolManager and v3 factory) against a deterministic fake Base RPC.

The fake chain drives a ``ClPoolState`` per pool, so every emitted Swap event carries the amounts,
sqrt price, liquidity and tick the v3 swap loop produces: the pack it yields reconciles exactly.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest
import yaml

from market_replay.collectors.evm_rpc import (
    TOPIC_V3_BURN,
    TOPIC_V3_INITIALIZE,
    TOPIC_V3_MINT,
    TOPIC_V3_POOL_CREATED,
    TOPIC_V3_SWAP,
    TOPIC_V4_INITIALIZE,
    TOPIC_V4_MODIFY_LIQUIDITY,
    TOPIC_V4_SWAP,
)
from market_replay.collectors.historical import CollectionBlocked, run_collection
from market_replay.collectors.uniswap_cl import active_liquidity, fold_tick_map
from market_replay.datasets.pack import Pack
from market_replay.venues.clmm.pool import ClPoolState

POOL_MANAGER = "0x498581ff718922c3f8e6a244956af099b2652b2b"
V3_FACTORY = "0x33128a8fc17869897dce68ed026d694621f6fdfd"
WETH = "0x4200000000000000000000000000000000000006"
TOKEN = "0x" + "ab" * 20
TOKEN2 = "0x" + "bc" * 20
TOKEN3 = "0x" + "de" * 20
HOOKS = "0xc1a4" + "0" * 32 + "a8cc"  # Clanker-style hook address; any address works for the fake
SENDER = "0x" + "77" * 20
POOL_ID = "0x" + "01" * 32  # v4 pool ids are bytes32
POOL_ID2 = "0x" + "02" * 32
POOL_ID3 = "0x" + "03" * 32
POOL_V3 = "0x" + "cd" * 20
POOL_V3_2 = "0x" + "ce" * 20
POOL_V3_3 = "0x" + "cf" * 20
ANCHOR_BLOCK = 30_000_000
ANCHOR_TS = 1_756_997_600  # seconds == 2025-09-04T14:53:20Z
Q96 = 1 << 96
FEE, SPACING = 10000, 200
L1, L2 = 10**21, 5 * 10**20
IN_AMOUNT = 10**18


def w(v: int) -> str:
    return (v % (1 << 256)).to_bytes(32, "big").hex()


def topic_addr(a: str) -> str:
    return "0x" + "0" * 24 + a[2:].lower()


def topic_int(v: int) -> str:
    return "0x" + w(v)


class FakePoolManager:
    """One WETH/TOKEN pool with two in-range mints and a deterministic swap sequence, one TOKEN/TOKEN2 pool
    that fails the WETH filter, and one WETH/TOKEN3 pool initialized inside the period. ``protocol``
    selects whether the same world is emitted as v4 PoolManager logs or v3 factory + pool logs. Each
    pool's events come from a ``ClPoolState`` so the tape is self-consistent under the v3 math."""

    def __init__(self, *, protocol: str = "uniswap_v4", fail_first_logs: bool = False, rate_limit_once: bool = False) -> None:
        self.protocol = protocol
        self.v4 = protocol == "uniswap_v4"
        self.fail_first_logs = fail_first_logs
        self.rate_limit_once = rate_limit_once
        self.calls = 0
        self.logs: list[dict] = []
        self.swaps: list[tuple[int, int, int]] = []  # (block, sqrt_price_x96, tick) for slot0 answers
        created = ANCHOR_BLOCK - 5000
        self.created = created
        tx0 = "0x" + "11" * 32
        # pool 1 (WETH/TOKEN) created before the window, pool 2 (TOKEN/TOKEN2, no WETH leg) right after
        world = self.init(POOL_ID, POOL_V3, WETH, TOKEN, created, tx0)
        self.init(POOL_ID2, POOL_V3_2, TOKEN, TOKEN2, created + 1, "0x" + "12" * 32)
        self.modify(world, POOL_ID, POOL_V3, created + 2, "0x" + "13" * 32, -20000, 20000, L1)
        self.modify(world, POOL_ID, POOL_V3, created + 3, "0x" + "14" * 32, -2000, 2000, L2)
        # swaps every 10 blocks from A-3000 to A+1800 (prehistory starts at A-1800, period is [A, A+1800))
        for i, b in enumerate(range(ANCHOR_BLOCK - 3000, ANCHOR_BLOCK + 1800, 10)):
            tx = "0x" + f"{i:064x}"
            self.swap(world, POOL_ID, POOL_V3, b, tx, zero_for_one=i % 3 == 0)
            if i == 200:  # block A-1000, inside the prehistory
                self.modify(world, POOL_ID, POOL_V3, b, tx + "1", -2000, 2000, -L2 // 10)
        # pool 3 initialized inside the period with one mint and one swap
        b3 = ANCHOR_BLOCK + 100
        world3 = self.init(POOL_ID3, POOL_V3_3, WETH, TOKEN3, b3, "0x" + "15" * 32)
        self.modify(world3, POOL_ID3, POOL_V3_3, b3 + 1, "0x" + "16" * 32, -1000, 1000, L1)
        self.swap(world3, POOL_ID3, POOL_V3_3, b3 + 2, "0x" + "17" * 32, zero_for_one=True)
        self.latest = ANCHOR_BLOCK + 5000

    def init(self, pid: str, pool: str, c0: str, c1: str, block: int, tx: str) -> ClPoolState:
        if self.v4:
            self.logs.append({"address": POOL_MANAGER, "blockNumber": hex(block), "logIndex": hex(0), "transactionHash": tx, "topics": [TOPIC_V4_INITIALIZE, pid, topic_addr(c0), topic_addr(c1)], "data": "0x" + w(FEE) + w(SPACING) + w(int(HOOKS, 16)) + w(Q96) + w(0)})
        else:
            self.logs.append({"address": V3_FACTORY, "blockNumber": hex(block), "logIndex": hex(0), "transactionHash": tx, "topics": [TOPIC_V3_POOL_CREATED, topic_addr(c0), topic_addr(c1), topic_int(FEE)], "data": "0x" + w(SPACING) + w(int(pool, 16))})
            self.logs.append({"address": pool, "blockNumber": hex(block), "logIndex": hex(1), "transactionHash": tx, "topics": [TOPIC_V3_INITIALIZE], "data": "0x" + w(Q96) + w(0)})
        return ClPoolState.initialize(pid, c0, c1, FEE, SPACING, Q96)

    def modify(self, world: ClPoolState, pid: str, pool: str, block: int, tx: str, lo: int, hi: int, delta: int) -> None:
        amount0, amount1 = world.apply_modify_liquidity(lo, hi, delta)
        if self.v4:
            self.logs.append({"address": POOL_MANAGER, "blockNumber": hex(block), "logIndex": hex(5), "transactionHash": tx, "topics": [TOPIC_V4_MODIFY_LIQUIDITY, pid, topic_addr(SENDER)], "data": "0x" + w(lo) + w(hi) + w(delta) + w(0)})
        elif delta > 0:
            self.logs.append({"address": pool, "blockNumber": hex(block), "logIndex": hex(5), "transactionHash": tx, "topics": [TOPIC_V3_MINT, topic_addr(SENDER), topic_int(lo), topic_int(hi)], "data": "0x" + w(int(SENDER, 16)) + w(delta) + w(amount0) + w(amount1)})
        else:
            self.logs.append({"address": pool, "blockNumber": hex(block), "logIndex": hex(5), "transactionHash": tx, "topics": [TOPIC_V3_BURN, topic_addr(SENDER), topic_int(lo), topic_int(hi)], "data": "0x" + w(-delta) + w(-amount0) + w(-amount1)})

    def swap(self, world: ClPoolState, pid: str, pool: str, block: int, tx: str, *, zero_for_one: bool) -> None:
        # exact input of IN_AMOUNT on the driving state; pool deltas: the input leg is paid into the pool
        a0, a1, sqrt, liq, tick = world.swap(zero_for_one, IN_AMOUNT)
        if pid == POOL_ID:
            self.swaps.append((block, sqrt, tick))
        if self.v4:
            # v4 emits user deltas: the opposite sign of the pool deltas
            self.logs.append({"address": POOL_MANAGER, "blockNumber": hex(block), "logIndex": hex(2), "transactionHash": tx, "topics": [TOPIC_V4_SWAP, pid, topic_addr(SENDER)], "data": "0x" + w(-a0) + w(-a1) + w(sqrt) + w(liq) + w(tick) + w(FEE)})
        else:
            self.logs.append({"address": pool, "blockNumber": hex(block), "logIndex": hex(2), "transactionHash": tx, "topics": [TOPIC_V3_SWAP, topic_addr(SENDER), topic_addr(SENDER)], "data": "0x" + w(a0) + w(a1) + w(sqrt) + w(liq) + w(tick)})

    def ts(self, block: int) -> int:
        return ANCHOR_TS + (block - ANCHOR_BLOCK) * 2

    def last_swap_at_or_before(self, block: int) -> tuple[int, int]:
        before = [(s, t) for b, s, t in self.swaps if b <= block]
        return before[-1] if before else (Q96, 0)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        body = json.loads(request.content)
        method, params = body["method"], body["params"]
        if self.rate_limit_once:
            self.rate_limit_once = False
            return httpx.Response(429, json={"error": "rate limited"})

        def ok(result):
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

        if method == "eth_chainId":
            return ok(hex(8453))
        if method == "eth_getTransactionReceipt":  # a Base receipt: L2 execution plus the L1 data fee
            return ok({"transactionHash": params[0], "gasUsed": hex(173_040), "effectiveGasPrice": hex(100_000_000), "l1Fee": hex(10_000_000_000_000)})
        if method == "eth_blockNumber":
            return ok(hex(self.latest))
        if method == "eth_getBlockByNumber":
            n = int(params[0], 16)
            return ok({"number": hex(n), "timestamp": hex(self.ts(n))})
        if method == "eth_call":
            data = params[0]["data"]
            to = params[0]["to"].lower()
            if to == POOL_MANAGER and data.startswith("0x8da5cb5b"):  # owner()
                return ok("0x" + w(int("0x" + "99" * 20, 16)))
            if to == V3_FACTORY and data.startswith("0x22afcccb"):  # feeAmountTickSpacing(10000)
                return ok("0x" + w(SPACING))
            if data.startswith("0x313ce567"):  # decimals()
                return ok("0x" + w(18))
            if to == POOL_V3 and data.startswith("0x3850c7bd"):  # slot0()
                sqrt, tick = self.last_swap_at_or_before(int(params[1], 16))
                return ok("0x" + w(sqrt) + w(tick) + w(0) * 5)
            return ok("0x")
        if method == "eth_getLogs":
            if self.fail_first_logs:
                self.fail_first_logs = False
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32005, "message": "query returned more than 10000 results"}})
            f = params[0]
            fb, tb = int(f["fromBlock"], 16), int(f["toBlock"], 16)
            addr = f.get("address")
            addrs = {a.lower() for a in (addr if isinstance(addr, list) else [addr])} if addr else None
            specs = f.get("topics") or []

            def match(lg: dict) -> bool:
                for i, spec in enumerate(specs):
                    if spec is None:
                        continue
                    have = lg["topics"][i].lower() if i < len(lg["topics"]) else None
                    want = {s.lower() for s in spec} if isinstance(spec, list) else {spec.lower()}
                    if have not in want:
                        return False
                return True

            out = [l for l in self.logs if fb <= int(l["blockNumber"], 16) <= tb and (addrs is None or l["address"].lower() in addrs) and match(l)]
            return ok(out)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32601, "message": "method not found"}})


def write_cfg(tmp_path: Path, **over) -> Path:
    cfg = {
        "rpc_url_env": "TEST_BASE_RPC_URL",
        "chain": "base",
        "protocol": "uniswap_v4",
        "period_start_utc": "2025-09-04T14:53:20Z",
        "period_end_utc": "2025-09-04T15:53:20Z",
        "discovery_window_start_utc": "2025-09-04T11:53:20Z",
        "prehistory_hours": 1,
        "max_pairs": 2,
        "max_requests": 400,
        "log_chunk_blocks": 3000,
        "out_dir": "packs/historical/test_cl_slice",
        "authorization_note": "test: fake RPC, no external access",
        "availability_delay_ms": 4000,
    }
    cfg.update(over)
    p = tmp_path / "collect.yaml"
    p.write_text(yaml.safe_dump(cfg))
    return p


def run(cfg: Path, tmp_path: Path, fake: FakePoolManager, **kw):
    return run_collection(cfg, tmp_path, transport=httpx.MockTransport(fake.handle), rpc_url_override="http://fake-rpc.local", sleep=lambda s: None, **kw)


def _check_pack(res: dict, fake: FakePoolManager, protocol: str) -> Pack:
    assert res["status"] == "pack_built", res
    pack = Pack.load(res["pack_dir"])
    assert pack.manifest.origin == "historical_reconstruction"
    assert pack.manifest.scope_label == "base_uniswap_cl_native_pairs_research_v1"
    assert [str(m) for m in pack.manifest.universe.pool_models] == [f"{protocol}_cl"]
    chain = 8453
    main_addr = POOL_ID if protocol == "uniswap_v4" else POOL_V3
    third_addr = POOL_ID3 if protocol == "uniswap_v4" else POOL_V3_3
    key = f"{chain}:{protocol}:{main_addr}"
    key3 = f"{chain}:{protocol}:{third_addr}"
    assert set(pack.pools) == {key, key3}
    p = pack.pools[key]
    assert str(p.model) == f"{protocol}_cl" and p.protocol == protocol
    assert p.supported_by_cpmm is False and p.supported_by_clmm is True
    assert p.fee_pips == FEE and p.tick_spacing == SPACING
    assert p.hooks == (HOOKS if protocol == "uniswap_v4" else None)
    assert p.asset0 == f"{chain}:{WETH}" and p.asset1 == f"{chain}:{TOKEN}"
    assert p.initial_reserve0 is None and p.initial_reserve1 is None
    # tick map folded from every liquidity event before the window; active liquidity summed at the initial tick
    assert p.initial_ticks == [["-20000", str(L1), str(L1)], ["-2000", str(L2), str(L2)], ["2000", str(-L2), str(L2)], ["20000", str(-L1), str(L1)]]
    assert p.initial_liquidity == str(L1 + L2)
    pre_start = ANCHOR_BLOCK - 1800
    sqrt, tick = fake.last_swap_at_or_before(pre_start - 1)
    assert p.initial_sqrt_price_x96 == str(sqrt) and p.initial_tick == tick and p.initial_state_block == pre_start - 1
    assert p.initial_state_basis is not None
    if protocol == "uniswap_v4":
        assert p.initial_state_basis.startswith(f"last_swap_before_prehistory_at_block_{pre_start - 10}")
    else:
        assert p.initial_state_basis.startswith(f"slot0_at_block_{pre_start - 1}")
    assert "tick_map_and_liquidity_folded" in p.initial_state_basis
    # the pool without a WETH leg is inventoried, never selected
    assert any(u["reason"] == "no wrapped-native leg" for u in pack.inventory["unsupported"])
    # tape: only the prehistory + period rows of pool 1, plus the in-window life of pool 3
    rows1 = [r for r in pack.tape if r["pool"] == key]
    assert rows1 and rows1[0]["kind"] == "cl_swap" and rows1[0]["block"] == pre_start
    assert all(r["block"] >= pre_start for r in pack.tape)
    kinds1 = [r["kind"] for r in rows1]
    assert "cl_init" not in kinds1 and kinds1.count("cl_modify") == 1
    burn = next(r for r in rows1 if r["kind"] == "cl_modify")
    assert burn["liquidity_delta"] == str(-L2 // 10) and burn["tick_lower"] == -2000 and burn["tick_upper"] == 2000
    if protocol == "uniswap_v3":
        assert int(burn["amount0"]) > 0 and int(burn["amount1"]) > 0
    else:
        assert burn.get("amount0") is None
    swaps = [r for r in rows1 if r["kind"] == "cl_swap"]
    # pool-delta sign convention in both protocols: the input leg is positive, the output leg negative
    z = [r for r in swaps if r["amount0"] == str(IN_AMOUNT)]
    o = [r for r in swaps if r["amount1"] == str(IN_AMOUNT)]
    assert z and o and len(z) + len(o) == len(swaps)
    assert all(int(r["amount1"]) < 0 for r in z) and all(int(r["amount0"]) < 0 for r in o)
    assert all(r["fee_pips"] == FEE and r["wallet"] == SENDER for r in swaps)
    assert all(int(r["liquidity_after"]) in (L1 + L2, L1 + L2 - L2 // 10) for r in swaps)
    assert all(isinstance(r["sqrt_price_x96_after"], str) and isinstance(r["tick_after"], int) for r in swaps)
    # the swap after the burn reports the reduced liquidity
    after_burn = [r for r in swaps if (r["block"], r["log_index"]) > (burn["block"], burn["log_index"])]
    assert after_burn and after_burn[0]["liquidity_after"] == str(L1 + L2 - L2 // 10)
    # pool 3 starts inside the window: cl_init, cl_modify, cl_swap in order, no initial state
    rows3 = [r for r in pack.tape if r["pool"] == key3]
    assert [r["kind"] for r in rows3] == ["cl_init", "cl_modify", "cl_swap"]
    assert rows3[0]["sqrt_price_x96"] == str(Q96) and rows3[0]["tick"] == 0
    p3 = pack.pools[key3]
    assert p3.initial_state_basis == "pool_initialized_inside_window" and p3.initial_sqrt_price_x96 is None and p3.initial_ticks == []
    # ordering, availability and acquisition times
    keys = [(r["block"], r["log_index"], r["seq"]) for r in pack.tape]
    assert keys == sorted(keys) and [r["seq"] for r in pack.tape] == list(range(1, len(pack.tape) + 1))
    assert all(r["available_utc_ms"] == r["time_utc_ms"] + 4000 and r["received_utc_ms"] > r["time_utc_ms"] for r in pack.tape)
    # validator: mechanics gate passes (no CL pool claims CPMM support); every cl_swap is a checkpoint and the
    # tape came from the same v3 math, so the no-agent replay reconciles exactly and the pack stays research
    gates = {g["gate"]: g for g in pack.validation["gates"]}
    assert gates["mechanics"]["status"] == "passed"
    recon = gates["execution_state"]["reconciliation"]
    assert recon["checkpoints"] == len([r for r in pack.tape if r["kind"] == "cl_swap"]) > 0
    assert recon["mismatch_count"] == 0 and recon["fidelity_flag_count"] == 0
    assert gates["execution_state"]["status"] == "passed" and gates["execution_state"]["pools_missing_initial_state"] == []
    assert gates["temporal_ordering"]["status"] == "passed"
    assert pack.validation["resulting_qualification"] == "research" and res["qualification"] == "research"
    assert any("supported_by_clmm" in s for s in pack.manifest.decision_log)
    assert res["budget"]["requests"] <= 400
    return pack


def test_v4_collection_builds_pack_with_cl_fields(tmp_path: Path):
    fake = FakePoolManager()
    res = run(write_cfg(tmp_path), tmp_path, fake)
    pack = _check_pack(res, fake, "uniswap_v4")
    assert pack.manifest.universe.factories == [POOL_MANAGER]
    receipts = [json.loads(l) for l in (Path(res["work_dir"]) / "receipts" / "receipts.jsonl").read_text().splitlines()]
    assert receipts and all(r["integrity_hash_basis"] == "sha256_of_stored_bytes" for r in receipts)


def test_v3_collection_builds_pack_with_cl_fields(tmp_path: Path):
    fake = FakePoolManager(protocol="uniswap_v3")
    res = run(write_cfg(tmp_path, protocol="uniswap_v3"), tmp_path, fake)
    pack = _check_pack(res, fake, "uniswap_v3")
    assert pack.manifest.universe.factories == [V3_FACTORY]


def test_v4_hooks_allowlist_filters_pools(tmp_path: Path):
    fake = FakePoolManager()
    res = run(write_cfg(tmp_path, hooks_allowlist=["0x" + "00" * 20]), tmp_path, fake)
    assert res["status"] == "pack_built" and res["pools"] == 0
    pack = Pack.load(res["pack_dir"])
    assert sum("not in configured allowlist" in u["reason"] for u in pack.inventory["unsupported"]) == 2


def test_collection_is_resumable_after_budget_exhaustion(tmp_path: Path):
    fake = FakePoolManager()
    res1 = run(write_cfg(tmp_path, max_requests=12), tmp_path, fake)
    assert res1["status"] == "budget_exhausted_resumable"
    ck = json.loads((Path(res1["work_dir"]) / "checkpoints.json").read_text())
    assert "chain_verified" in ck
    calls_before = fake.calls
    res2 = run(write_cfg(tmp_path, max_requests=400), tmp_path, fake)
    _check_pack(res2, fake, "uniswap_v4")
    # the second run did not repeat the chain verification request
    assert fake.calls - calls_before < res1["budget"]["requests"] + 400


def test_collection_is_resumable_across_time_slices(tmp_path: Path):
    fake = FakePoolManager()
    cfg = write_cfg(tmp_path)
    # an already-expired slice still completes exactly one chunk before stopping
    res1 = run(cfg, tmp_path, fake, deadline=time.monotonic() - 1)
    assert res1["status"] == "in_progress_resumable"
    ck = json.loads((Path(res1["work_dir"]) / "checkpoints.json").read_text())
    assert ck["discovery:cursor"] == ANCHOR_BLOCK - 5400 + 3000
    calls_before = fake.calls
    res2 = run(cfg, tmp_path, fake, deadline=time.monotonic() - 1)
    assert res2["status"] == "in_progress_resumable" and fake.calls - calls_before == 1
    statuses = [res2["status"]]
    while statuses[-1] == "in_progress_resumable":
        statuses.append(run(cfg, tmp_path, fake, deadline=time.monotonic() - 1)["status"])
        assert len(statuses) < 100
    res = json.loads((Path(res1["work_dir"]) / "last_result.json").read_text())
    _check_pack(res, fake, "uniswap_v4")


def test_provider_errors_persisted_and_chunk_halved(tmp_path: Path):
    fake = FakePoolManager(fail_first_logs=True, rate_limit_once=True)
    res = run(write_cfg(tmp_path), tmp_path, fake)
    _check_pack(res, fake, "uniswap_v4")
    errors = [json.loads(l) for l in (Path(res["work_dir"]) / "errors.jsonl").read_text().splitlines()]
    kinds = {e["kind"] for e in errors}
    assert "retryable" in kinds and "application" in kinds
    assert any("chunk reduced" in s for s in res["decision_log"])


def test_wrong_chain_id_blocks(tmp_path: Path):
    fake = FakePoolManager()

    def handle(req):
        body = json.loads(req.content)
        if body["method"] == "eth_chainId":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": hex(1)})
        return fake.handle(req)

    res = run_collection(write_cfg(tmp_path), tmp_path, transport=httpx.MockTransport(handle), rpc_url_override="http://fake-rpc.local", sleep=lambda s: None)
    assert res["status"] == "blocked" and "chain id" in res["reason"]


def test_unknown_protocol_is_blocked(tmp_path: Path):
    with pytest.raises(CollectionBlocked, match="unsupported protocol"):
        run(write_cfg(tmp_path, protocol="uniswap_v5"), tmp_path, FakePoolManager())


def test_tick_map_fold_and_active_liquidity():
    rows = [
        {"tick_lower": -100, "tick_upper": 100, "liquidity_delta": "10"},
        {"tick_lower": -50, "tick_upper": 50, "liquidity_delta": "5"},
        {"tick_lower": -50, "tick_upper": 50, "liquidity_delta": "-5"},
    ]
    ticks = fold_tick_map(rows)
    assert ticks == [["-100", "10", "10"], ["100", "-10", "10"]]
    assert active_liquidity(ticks, 0) == 10 and active_liquidity(ticks, -101) == 0 and active_liquidity(ticks, 100) == 0


def test_activity_scan_batches_pool_ids_so_no_filter_exceeds_the_request_body_cap(tmp_path: Path, monkeypatch):
    import market_replay.collectors.uniswap_cl as cl

    monkeypatch.setattr(cl, "ACTIVITY_BATCH", 1)
    fake = FakePoolManager(protocol="uniswap_v4")
    seen: list[int] = []
    orig = fake.handle

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] == "eth_getLogs":
            topics = body["params"][0].get("topics") or []
            if len(topics) > 1 and isinstance(topics[1], list):
                seen.append(len(topics[1]))
        return orig(request)

    cfg = write_cfg(tmp_path, selection_rule="active_before_window_earliest_created_v1", activity_lookback_blocks=3000)
    res = run_collection(cfg, tmp_path, transport=httpx.MockTransport(handle), rpc_url_override="http://fake-rpc.local", sleep=lambda s: None)
    assert res["status"] == "pack_built", res
    assert seen and max(seen) == 1  # every activity filter carried a single pool id
    assert any("activity scan before the window" in line for line in res["decision_log"])


@pytest.mark.parametrize("protocol", ["uniswap_v4", "uniswap_v3"])
def test_window_launches_join_the_universe_at_their_kth_swap(tmp_path: Path, protocol: str):
    fake = FakePoolManager(protocol=protocol)
    # one established pool (active before the window) plus launches from inside the period that reached 1 swap
    cfg = write_cfg(tmp_path, protocol=protocol, selection_rule="active_before_window_plus_window_launches_v1", activity_lookback_blocks=3000, max_pairs=2, max_launches=1, launch_min_swaps=1)
    res = run_collection(cfg, tmp_path, transport=httpx.MockTransport(fake.handle), rpc_url_override="http://fake-rpc.local", sleep=lambda s: None)
    assert res["status"] == "pack_built", res
    pack = Pack.load(res["pack_dir"])
    third_addr = POOL_ID3 if protocol == "uniswap_v4" else POOL_V3_3
    first_addr = POOL_ID if protocol == "uniswap_v4" else POOL_V3
    keys = {p.address: p for p in pack.pools.values()}
    assert set(keys) == {first_addr, third_addr}
    launch = keys[third_addr]
    activation_block = ANCHOR_BLOCK + 100 + 2  # its first (k=1) swap
    assert launch.discovery_available_utc_ms == fake.ts(activation_block) * 1000 + 4000
    assert launch.created_time_utc_ms == fake.ts(ANCHOR_BLOCK + 100) * 1000
    assert keys[first_addr].discovery_available_utc_ms == fake.ts(fake.created) * 1000 + 4000
    assert any("window launches: 1 of" in line for line in res["decision_log"])
    assert pack.manifest.universe.selection_rule_version == "active_before_window_plus_window_launches_v1"
    assert pack.validation["resulting_qualification"] == "research"
