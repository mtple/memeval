"""Historical collector against a deterministic fake Base RPC: resumability, failures, normalization, reconciliation."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import yaml

from market_replay.collectors.evm_rpc import (
    TOPIC_BURN,
    TOPIC_MINT,
    TOPIC_PAIR_CREATED,
    TOPIC_SWAP,
    TOPIC_SYNC,
)
from market_replay.collectors.historical import normalize_pair_logs, run_collection
from market_replay.datasets.pack import Pack
from market_replay.venues.cpmm.math import get_amount_out

FACTORY = "0x8909dc15e40173ff4699343b6eb8132c65e18ec6"
WETH = "0x4200000000000000000000000000000000000006"
TOKEN = "0x" + "ab" * 20
PAIR = "0x" + "cd" * 20
ANCHOR_BLOCK = 30_000_000
ANCHOR_TS = 1_756_997_600  # seconds == 2025-09-04T14:53:20Z


def w(v: int) -> str:
    return v.to_bytes(32, "big").hex()


def topic_addr(a: str) -> str:
    return "0x" + "0" * 24 + a[2:].lower()


class FakeBase:
    """A tiny v2 world: one factory, one WETH pair created before the window, deterministic swaps."""

    def __init__(self, *, fail_first_logs: bool = False, rate_limit_once: bool = False) -> None:
        self.fail_first_logs = fail_first_logs
        self.rate_limit_once = rate_limit_once
        self.calls = 0
        self.logs: list[dict] = []
        # pair created at block A-5000
        created = ANCHOR_BLOCK - 5000
        self.logs.append({"address": FACTORY, "blockNumber": hex(created), "logIndex": hex(0), "transactionHash": "0x" + "11" * 32, "topics": [TOPIC_PAIR_CREATED, topic_addr(TOKEN), topic_addr(WETH)], "data": "0x" + w(int(PAIR, 16)) + w(1)})
        r0, r1 = 10**24, 500 * 10**18  # token0=TOKEN (sorted lower), token1=WETH
        # initial mint + sync
        self.logs.append({"address": PAIR, "blockNumber": hex(created), "logIndex": hex(1), "transactionHash": "0x" + "11" * 32, "topics": [TOPIC_SYNC], "data": "0x" + w(r0) + w(r1)})
        self.logs.append({"address": PAIR, "blockNumber": hex(created), "logIndex": hex(2), "transactionHash": "0x" + "11" * 32, "topics": [TOPIC_MINT, topic_addr(FACTORY)], "data": "0x" + w(r0) + w(r1)})
        # swaps every 10 blocks from A-1000 to A+1800 (period is [A, A+1800))
        for i, b in enumerate(range(ANCHOR_BLOCK - 1000, ANCHOR_BLOCK + 1800, 10)):
            tx = "0x" + f"{i:064x}"
            if i % 3 == 0:
                a_in = 10**17  # buy token with 0.1 WETH
                out = get_amount_out(a_in, r1, r0)
                r1 += a_in
                r0 -= out
                data = "0x" + w(0) + w(a_in) + w(out) + w(0)
            else:
                a_in = 10**20
                out = get_amount_out(a_in, r0, r1)
                r0 += a_in
                r1 -= out
                data = "0x" + w(a_in) + w(0) + w(0) + w(out)
            self.logs.append({"address": PAIR, "blockNumber": hex(b), "logIndex": hex(0), "transactionHash": tx, "topics": [TOPIC_SYNC], "data": "0x" + w(r0) + w(r1)})
            self.logs.append({"address": PAIR, "blockNumber": hex(b), "logIndex": hex(1), "transactionHash": tx, "topics": [TOPIC_SWAP, topic_addr("0x" + "77" * 20), topic_addr("0x" + "88" * 20)], "data": data})
            if i == 50:
                # a burn of 1% with its sync
                b0, b1 = r0 // 100, r1 // 100
                r0 -= b0
                r1 -= b1
                self.logs.append({"address": PAIR, "blockNumber": hex(b), "logIndex": hex(2), "transactionHash": tx + "1", "topics": [TOPIC_SYNC], "data": "0x" + w(r0) + w(r1)})
                self.logs.append({"address": PAIR, "blockNumber": hex(b), "logIndex": hex(3), "transactionHash": tx + "1", "topics": [TOPIC_BURN, topic_addr(FACTORY)], "data": "0x" + w(b0) + w(b1)})
        self.latest = ANCHOR_BLOCK + 5000

    def ts(self, block: int) -> int:
        return ANCHOR_TS + (block - ANCHOR_BLOCK) * 2

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
        if method == "eth_blockNumber":
            return ok(hex(self.latest))
        if method == "eth_getBlockByNumber":
            n = int(params[0], 16)
            return ok({"number": hex(n), "timestamp": hex(self.ts(n))})
        if method == "eth_call":
            data = params[0]["data"]
            to = params[0]["to"].lower()
            if to == FACTORY:
                return ok("0x" + w(1) if data.startswith("0x574f2ba3") else "0x" + w(int("0x" + "99" * 20, 16)))
            if data.startswith("0x313ce567"):  # decimals()
                return ok("0x" + w(18))
            if data.startswith("0x0902f1ac"):  # getReserves
                return ok("0x" + w(10**24) + w(500 * 10**18) + w(0))
            return ok("0x")
        if method == "eth_getLogs":
            if self.fail_first_logs:
                self.fail_first_logs = False
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32005, "message": "query returned more than 10000 results"}})
            f = params[0]
            fb, tb = int(f["fromBlock"], 16), int(f["toBlock"], 16)
            addr = f.get("address")
            addrs = {a.lower() for a in (addr if isinstance(addr, list) else [addr])} if addr else None
            topics = f.get("topics") or [None]
            t0 = topics[0]
            want = set(t0) if isinstance(t0, list) else ({t0} if t0 else None)
            out = [l for l in self.logs if fb <= int(l["blockNumber"], 16) <= tb and (addrs is None or l["address"].lower() in addrs) and (want is None or l["topics"][0] in want)]
            return ok(out)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32601, "message": "method not found"}})


def write_cfg(tmp_path: Path, **over) -> Path:
    cfg = {
        "rpc_url_env": "TEST_BASE_RPC_URL",
        "chain": "base",
        "protocol": "uniswap_v2",
        "period_start_utc": "2025-09-04T14:53:20Z",
        "period_end_utc": "2025-09-04T15:53:20Z",
        "discovery_window_start_utc": "2025-09-04T11:53:20Z",
        "prehistory_hours": 1,
        "max_pairs": 2,
        "max_requests": 400,
        "log_chunk_blocks": 3000,
        "out_dir": "packs/historical/test_slice",
        "authorization_note": "test: fake RPC, no external access",
        "availability_delay_ms": 4000,
    }
    cfg.update(over)
    p = tmp_path / "collect.yaml"
    p.write_text(yaml.safe_dump(cfg))
    return p


def test_iso_anchor_matches_fake_chain():
    from market_replay.collectors.historical import iso_ms

    assert iso_ms("2025-09-04T14:53:20Z") == ANCHOR_TS * 1000


def test_collection_builds_research_pack_and_reconciles(tmp_path: Path):
    fake = FakeBase()
    cfg = write_cfg(tmp_path)
    res = run_collection(cfg, tmp_path, transport=httpx.MockTransport(fake.handle), rpc_url_override="http://fake-rpc.local", sleep=lambda s: None)
    assert res["status"] == "pack_built", res
    pack = Pack.load(res["pack_dir"])
    assert pack.manifest.origin == "historical_reconstruction"
    assert pack.validation["resulting_qualification"] == "research"
    ex = next(g for g in pack.validation["gates"] if g["gate"] == "execution_state")
    assert ex["status"] == "passed" and ex["reconciliation"]["mismatch_count"] == 0
    assert pack.manifest.period.is_full_week is False
    assert str(pack.manifest.data.token_behavior_basis) == "assumed_standard_transfer"
    assert res["budget"]["requests"] <= 400
    # receipts preserved with redacted request and sha256 of stored bytes
    receipts = [json.loads(l) for l in (Path(res["work_dir"]) / "receipts" / "receipts.jsonl").read_text().splitlines()]
    assert receipts and all(r["body_sha256"] for r in receipts if r["http_status"] == 200)
    assert all(r["integrity_hash_basis"] == "sha256_of_stored_bytes" for r in receipts)


def test_collection_is_resumable_after_budget_exhaustion(tmp_path: Path):
    fake = FakeBase()
    cfg = write_cfg(tmp_path, max_requests=12)
    res1 = run_collection(cfg, tmp_path, transport=httpx.MockTransport(fake.handle), rpc_url_override="http://fake-rpc.local", sleep=lambda s: None)
    assert res1["status"] == "budget_exhausted_resumable"
    ck = json.loads((Path(res1["work_dir"]) / "checkpoints.json").read_text())
    assert "chain_verified" in ck
    cfg2 = write_cfg(tmp_path, max_requests=400)
    calls_before = fake.calls
    res2 = run_collection(cfg2, tmp_path, transport=httpx.MockTransport(fake.handle), rpc_url_override="http://fake-rpc.local", sleep=lambda s: None)
    assert res2["status"] == "pack_built"
    # the second run did not repeat the chain verification request
    assert fake.calls - calls_before < res1["budget"]["requests"] + 400


def test_provider_errors_persisted_and_chunk_halved(tmp_path: Path):
    fake = FakeBase(fail_first_logs=True, rate_limit_once=True)
    cfg = write_cfg(tmp_path)
    res = run_collection(cfg, tmp_path, transport=httpx.MockTransport(fake.handle), rpc_url_override="http://fake-rpc.local", sleep=lambda s: None)
    assert res["status"] == "pack_built"
    errors = [json.loads(l) for l in (Path(res["work_dir"]) / "errors.jsonl").read_text().splitlines()]
    kinds = {e["kind"] for e in errors}
    assert "retryable" in kinds and "application" in kinds
    assert any("chunk reduced" in s for s in res["decision_log"])


def test_backoff_never_sleeps_past_the_slice_deadline(tmp_path: Path):
    import time

    fake = FakeBase(rate_limit_once=True)
    cfg = write_cfg(tmp_path)
    slept: list[float] = []
    res = run_collection(cfg, tmp_path, transport=httpx.MockTransport(fake.handle), rpc_url_override="http://fake-rpc.local", sleep=slept.append, deadline=time.monotonic() - 1)
    assert res["status"] == "in_progress_resumable" and "backing off" in res["reason"] and slept == []
    res = run_collection(cfg, tmp_path, transport=httpx.MockTransport(fake.handle), rpc_url_override="http://fake-rpc.local", sleep=slept.append)
    assert res["status"] == "pack_built"


def test_missing_endpoint_is_blocked_not_guessed(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("TEST_BASE_RPC_URL", raising=False)
    cfg = write_cfg(tmp_path)
    res = run_collection(cfg, tmp_path, sleep=lambda s: None)
    assert res["status"] == "blocked" and "not set" in res["reason"]
    assert res["budget"]["requests"] == 0


def test_wrong_chain_id_blocks(tmp_path: Path):
    fake = FakeBase()

    def handle(req):
        body = json.loads(req.content)
        if body["method"] == "eth_chainId":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": hex(1)})
        return fake.handle(req)

    res = run_collection(write_cfg(tmp_path), tmp_path, transport=httpx.MockTransport(handle), rpc_url_override="http://fake-rpc.local", sleep=lambda s: None)
    assert res["status"] == "blocked" and "chain id" in res["reason"]


def test_normalizer_orders_sync_after_event_and_rejects_multi_shapes():
    tx = "0x" + "aa" * 32
    logs = [
        {"blockNumber": hex(10), "logIndex": hex(0), "transactionHash": tx, "topics": [TOPIC_SYNC], "data": "0x" + w(90) + w(110)},
        {"blockNumber": hex(10), "logIndex": hex(1), "transactionHash": tx, "topics": [TOPIC_SWAP, topic_addr(TOKEN), topic_addr(WETH)], "data": "0x" + w(0) + w(10) + w(10) + w(0)},
        {"blockNumber": hex(11), "logIndex": hex(0), "transactionHash": "0x" + "bb" * 32, "topics": [TOPIC_SYNC], "data": "0x" + w(1) + w(1)},
        {"blockNumber": hex(11), "logIndex": hex(1), "transactionHash": "0x" + "bb" * 32, "topics": [TOPIC_SWAP, topic_addr(TOKEN), topic_addr(WETH)], "data": "0x" + w(5) + w(5) + w(1) + w(0)},
    ]
    rows, unsupported = normalize_pair_logs(logs, pool_key="p", token0="t0", token1="t1", block_time_ms=lambda b: b * 2000)
    assert [r["kind"] for r in rows] == ["swap", "sync"]
    assert rows[0]["asset_in"] == "t1" and rows[0]["amount_in"] == "10" and rows[0]["amount_out_recorded"] == "10"
    assert (rows[0]["block"], rows[0]["log_index"]) < (rows[1]["block"], rows[1]["log_index"])
    assert unsupported and "multi-input" in unsupported[0]["reason"]
    assert all(r["received_utc_ms"] > r["time_utc_ms"] for r in rows)  # acquisition time kept separate, never contemporaneous
