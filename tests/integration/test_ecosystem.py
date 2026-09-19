"""The market lines against fake sources: the Base index over every ETH pool, ETH and BTC in dollars,
and the crypto market from a fake CoinGecko."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import yaml

from market_replay.collectors.crypto import CRYPTO_BASIS, TOP10, collect_crypto_market, write_crypto_market
from market_replay.collectors.ecosystem import (
    CBBTC,
    ECOSYSTEM_BASIS,
    MULTICALL3,
    USDC,
    WETH,
    capped_weights,
    collect_ecosystem,
    decode_aggregate3,
    encode_aggregate3,
    write_ecosystem,
)
from market_replay.collectors.evm_rpc import SEL_TOKEN0, SEL_TOKEN1, TOPIC_SWAP, TOPIC_SYNC, TOPIC_V3_SWAP
from market_replay.datasets.baseline import (
    BASELINE_FILE,
    baseline_sentence,
    market_lines,
    read_market_baseline,
    write_market_baseline,
)
from tests.unit.test_engine_clmm import make_cl_pack

Q96 = 1 << 96
START_MS = 1_789_171_201_000
BLOCK0 = 51_190_000
T0 = START_MS - 200 * 2000  # block BLOCK0 is 200 blocks before the day starts; two seconds a block
DAY0, DAY1 = BLOCK0 + 200, BLOCK0 + 200 + 43_200 - 1
TOKEN_A = "0x" + "a" * 40  # v3 pool against WETH, +10% vs ETH, deep
TOKEN_B = "0x" + "b" * 40  # v2 pair against WETH, -20% vs ETH, shallow
TOKEN_C = "0x" + "c" * 40  # traded only at the start: not counted
TOKEN_D = "0x" + "d" * 40  # v3 pool against USDC, no ETH leg: not counted


def _sqrt(price_1_per_0: float) -> int:
    return int((price_1_per_0**0.5) * Q96)


def _w(n: int) -> str:
    return hex(n)[2:].rjust(64, "0")


class FakeBase:
    def __init__(self) -> None:
        self.calls = 0
        # v3 pools: address -> (token0, token1, sqrt at start, sqrt at end, liquidity)
        self.v3 = {
            "0x" + "1".rjust(40, "0"): (WETH, TOKEN_A, _sqrt(1000.0), _sqrt(1100.0), 10**21),  # WETH is token0: token1 per token0 rises => token worth less? no: price is tokens per WETH; handled by the collector
            "0x" + "2".rjust(40, "0"): (WETH, USDC, _sqrt(2500e-12), _sqrt(2600e-12), 10**24),
            "0x" + "3".rjust(40, "0"): (WETH, CBBTC, _sqrt(0.03e-10), _sqrt(0.03e-10 * 0.95), 10**20),
            "0x" + "4".rjust(40, "0"): (WETH, TOKEN_C, _sqrt(5.0), _sqrt(5.0), 10**21),
            "0x" + "5".rjust(40, "0"): (USDC, TOKEN_D, _sqrt(5.0), _sqrt(6.0), 10**21),
        }
        # v2 pairs: address -> (token0, token1, reserves at start, reserves at end)
        self.v2 = {"0x" + "6".rjust(40, "0"): (TOKEN_B, WETH, (10**24, 5 * 10**18), (10**24, 4 * 10**18))}

    def logs(self, topics, lo: int, hi: int) -> list[dict]:
        out = []
        want = topics[0] if isinstance(topics[0], list) else [topics[0]]
        for blk, edge in ((DAY0 + 10, 0), (DAY1 - 10, 1)):
            if not (lo <= blk <= hi):
                continue
            for addr, (_t0, t1, s0, s1, liq) in self.v3.items():
                if TOPIC_V3_SWAP in want and not (edge == 1 and t1 == TOKEN_C):
                    out.append({"address": addr, "blockNumber": hex(blk), "logIndex": "0x1", "topics": [TOPIC_V3_SWAP], "data": "0x" + "0" * 128 + _w(s0 if edge == 0 else s1) + _w(liq) + "0" * 64})
            for addr, (_t0, _t1, ra, rb) in self.v2.items():
                if TOPIC_SWAP in want:
                    out.append({"address": addr, "blockNumber": hex(blk), "logIndex": "0x2", "topics": [TOPIC_SWAP], "data": "0x" + "0" * 256})
                if TOPIC_SYNC in want:
                    r = ra if edge == 0 else rb
                    out.append({"address": addr, "blockNumber": hex(blk), "logIndex": "0x3", "topics": [TOPIC_SYNC], "data": "0x" + _w(r[0]) + _w(r[1])})
        return out

    def multicall(self, data: str) -> str:
        raw = bytes.fromhex(data[10:])
        n = int.from_bytes(raw[32:64], "big")
        results: list[bytes | None] = []
        for i in range(n):
            off = 64 + int.from_bytes(raw[64 + 32 * i : 96 + 32 * i], "big")
            target = "0x" + raw[off + 12 : off + 32].hex()
            length = int.from_bytes(raw[off + 96 : off + 128], "big")
            sel = "0x" + raw[off + 128 : off + 128 + length].hex()
            pool = self.v3.get(target) or self.v2.get(target)
            if pool is None:
                results.append(None)
                continue
            tok = pool[0] if sel == SEL_TOKEN0 else pool[1]
            results.append(bytes.fromhex(_w(int(tok, 16))))
        # encode (bool, bytes)[]
        body = b""
        heads = []
        for r in results:
            heads.append(len(body))
            ok = (1 if r is not None else 0).to_bytes(32, "big")
            payload = r or b""
            body += ok + (64).to_bytes(32, "big") + len(payload).to_bytes(32, "big") + payload.ljust(32, b"\x00") if payload else ok + (64).to_bytes(32, "big") + (0).to_bytes(32, "big")
        arr = (32).to_bytes(32, "big") + len(results).to_bytes(32, "big") + b"".join((32 * len(results) + h).to_bytes(32, "big") for h in heads) + body
        return "0x" + arr.hex()

    def handle(self, req: httpx.Request) -> httpx.Response:
        self.calls += 1
        body = json.loads(req.content)
        m, params = body["method"], body["params"]
        if m == "eth_chainId":
            result: object = hex(8453)
        elif m == "eth_blockNumber":
            result = hex(BLOCK0 + 50_000)
        elif m == "eth_getBlockByNumber":
            n = int(params[0], 16)
            result = {"number": hex(n), "timestamp": hex((T0 + (n - BLOCK0) * 2000) // 1000)}
        elif m == "eth_call":
            assert params[1] == "latest" and params[0]["to"].lower() == MULTICALL3.lower()
            result = self.multicall(params[0]["data"])
        elif m == "eth_getLogs":
            q = params[0]
            assert "address" not in q
            result = self.logs(q["topics"], int(q["fromBlock"], 16), int(q["toBlock"], 16))
        else:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32601, "message": "no"}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})


class FakeGecko:
    def __init__(self, missing: str | None = None) -> None:
        self.missing = missing
        self.keys: set[str] = set()

    def handle(self, req: httpx.Request) -> httpx.Response:
        self.keys.add(req.headers.get("x-cg-demo-api-key", ""))
        cid = req.url.path.split("/")[-3]
        if cid == self.missing:
            return httpx.Response(200, json={"prices": [], "market_caps": [], "total_volumes": []})
        lo, hi = int(req.url.params["from"]) * 1000, int(req.url.params["to"]) * 1000
        base = {"bitcoin": 1.5e12, "ethereum": 3e11}.get(cid, 1e11)
        pts = [[t, base * (1.0 if t < lo + 12 * 3_600_000 else 1.02)] for t in range(lo, hi + 1, 3_600_000)]
        return httpx.Response(200, json={"prices": [], "market_caps": pts, "total_volumes": []})


def _day_pack(tmp_path: Path) -> Path:
    d = tmp_path / "base_day_2026-09-12"
    make_cl_pack(d)
    m = yaml.safe_load((d / "manifest.yaml").read_text())
    m["chain_id"] = 8453
    m["period"]["start_utc_ms"] = START_MS
    m["period"]["end_utc_ms"] = START_MS + 86_400_000
    m.setdefault("universe", {})["indexed_block_ranges"] = [[DAY0, DAY1 + 1]]
    (d / "manifest.yaml").write_text(yaml.safe_dump(m, sort_keys=False))
    return d


def test_capped_weights_spread_the_excess_and_never_exceed_the_cap():
    from fractions import Fraction

    w = capped_weights([70, 10, 10, 5, 5] + [1] * 20)  # one token is 58% of the depth
    assert sum(w) == 1 and max(w) == Fraction(1, 10)
    assert w[0] == w[1] == w[2] == w[3] == w[4] == Fraction(1, 10)  # the spread excess pushed the next four to the cap as well
    assert all(x == w[5] < Fraction(1, 10) for x in w[5:])  # the small ones share what is left, in proportion
    assert capped_weights([]) == [] and capped_weights([0, 0]) == []
    assert capped_weights([3, 1]) == [Fraction(1, 2), Fraction(1, 2)]  # the cap cannot be met with two tokens
    assert sum(capped_weights([100] + [1] * 99)) == 1


def test_multicall_encoding_round_trips():
    calls = [("0x" + "1".rjust(40, "0"), SEL_TOKEN0), ("0x" + "2".rjust(40, "0"), SEL_TOKEN1)]
    data = encode_aggregate3(calls)
    assert data.startswith("0x82ad56cb") and len(data) == 2 + 8 + 64 * (2 + 2 + 5 * 2)
    fake = FakeBase()
    out = decode_aggregate3(fake.multicall(data))
    assert out[0] is not None and "0x" + out[0][12:32].hex() == WETH and "0x" + out[1][12:32].hex() == USDC


def test_base_index_counts_every_native_token_with_an_eth_pool(tmp_path: Path):
    d = _day_pack(tmp_path)
    fake = FakeBase()
    eco = collect_ecosystem(d, rpc_url_override="http://fake-rpc.local", transport=httpx.MockTransport(fake.handle), sleep=lambda s: None)
    assert eco["basis"] == ECOSYSTEM_BASIS and eco["start_block"] == DAY0 and eco["end_block"] == DAY1
    t = eco["base_tokens"]
    # A (v3, WETH token0: 1100 tokens per WETH at the end means the token fell 1/1.1) and B (v2, -20%); C quiet at the end; D has no ETH leg
    assert t["tokens"] == 2 and t["pools"] == 2
    assert abs(float(t["equal_weight_return_vs_eth"]) - ((1 / 1.1 - 1) + (-0.2)) / 2) < 1e-6
    # with two tokens the tenth cap cannot be met, so both carry half: the depth-weighted line equals the equal-weight one
    assert t["depth_weighted_return_vs_eth"] == t["equal_weight_return_vs_eth"] and t["largest_weight"] == "0.500000"
    assert t["excluded_not_native"] == ["USDC", "cbBTC"]
    assert abs(float(eco["eth_usd_return"]) - 0.04) < 1e-6  # 2500 -> 2600
    assert abs(float(eco["btc_usd_return"]) - (1.04 / 0.95 - 1)) < 1e-6  # fewer cbBTC per WETH at the end: BTC rose against ETH
    assert eco["budget"]["requests"] == fake.calls < 60


def test_crypto_market_sums_the_ten_largest_coins(tmp_path: Path):
    d = _day_pack(tmp_path)
    gecko = FakeGecko(missing="cardano")
    paused: list[float] = []
    c = collect_crypto_market(d, transport=httpx.MockTransport(gecko.handle), sleep=paused.append, api_key="demo-key")
    assert c["basis"] == CRYPTO_BASIS and len(c["coins"]) == 9 and c["coins_expected"] == list(TOP10)
    assert abs(float(c["return"]) - 0.02) < 1e-6 and any("cardano" in n for n in c["notes"])
    assert c["budget"]["requests"] == 10 and gecko.keys == {"demo-key"} and len(paused) == 9  # paced between calls


def test_lines_merge_into_the_baseline_and_survive_regeneration(tmp_path: Path):
    d = _day_pack(tmp_path)
    write_market_baseline(d)
    write_ecosystem(d, rpc_url_override="http://fake-rpc.local", transport=httpx.MockTransport(FakeBase().handle), sleep=lambda s: None)
    write_crypto_market(d, transport=httpx.MockTransport(FakeGecko().handle), sleep=lambda s: None)
    b = read_market_baseline(d)
    assert b and b["ecosystem"]["basis"] == ECOSYSTEM_BASIS and b["crypto_market"]["basis"] == CRYPTO_BASIS
    lines = market_lines(b)
    assert lines["eth_usd"] == "0.040000" and lines["crypto_market_usd"] == "0.020000" and lines["base_ecosystem_tokens"] == 2
    assert abs(float(lines["base_ecosystem_usd"]) - ((1 + float(lines["base_ecosystem_vs_eth"])) * 1.04 - 1)) < 1e-6
    sentence = baseline_sentence(b)
    assert sentence.startswith("Market that day: the Base ecosystem") and "ETH +4.0%" in sentence and "the crypto market +2.0%" in sentence
    write_market_baseline(d)  # the tape-derived part is rebuilt, the network-derived parts are kept
    again = read_market_baseline(d)
    assert again["ecosystem"] == b["ecosystem"] and again["crypto_market"] == b["crypto_market"]
    assert json.loads((d / BASELINE_FILE).read_text())["crypto_market"]["return"] == "0.020000"
    assert baseline_sentence({"launches": {"pools_priced": 3}}) is None and market_lines(None) is None
