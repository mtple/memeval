"""The Base ecosystem basket against a fake chain: symbol checks, deepest-pool choice, block search, returns."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import yaml

from market_replay.collectors.ecosystem import (
    BASKET,
    ECOSYSTEM_BASIS,
    USDC,
    V3_FACTORY,
    WETH,
    collect_ecosystem,
    write_ecosystem,
)
from market_replay.datasets.baseline import (
    BASELINE_FILE,
    baseline_sentence,
    read_market_baseline,
    write_market_baseline,
)
from tests.unit.test_engine_clmm import make_cl_pack

Q96 = 1 << 96
START_MS = 1_789_171_201_000
BLOCK0 = 51_190_000
T0 = START_MS - 200 * 2000  # block BLOCK0 is 200 blocks before the day starts; two seconds a block


def _sqrt(price_1_per_0: float) -> int:
    return int((price_1_per_0**0.5) * Q96)


class FakeBase:
    """Every basket token except one has a v3 pool against WETH; DEGEN's symbol is wrong on purpose."""

    def __init__(self) -> None:
        self.calls = 0
        self.pools: dict[tuple[str, int], str] = {}
        self.liq: dict[str, int] = {}
        self.price: dict[str, tuple[float, float]] = {}  # token1 per token0 at start, at end
        n = 0
        for sym, addr in BASKET:
            for fee, liq in ((3000, 1000), (10000, 5000)):
                n += 1
                pool = "0x" + hex(0xABC000 + n)[2:].rjust(40, "0")
                self.pools[(addr.lower(), fee)] = pool
                self.liq[pool] = liq if sym != "AERO" else 0
                # every token gains 10% against ETH; WETH is token0 for addresses above 0x42..
                weth0 = WETH < addr.lower()
                p = 0.001 if weth0 else 1000.0  # token1 per token0
                self.price[pool] = (p, p * (1.1 if not weth0 else 1 / 1.1) if sym != "TOSHI" else (p * (0.5 if not weth0 else 2.0)))
        pool = "0x" + "cc".rjust(40, "0")
        self.pools[(USDC, 500)] = pool
        self.liq[pool] = 10**9
        # WETH is token0 (0x42 < 0x83): USDC raw per WETH raw = 4000 * 1e6 / 1e18 at the start, 4400 at the end
        self.price[pool] = (4000e-12, 4400e-12)

    def handle(self, req: httpx.Request) -> httpx.Response:
        self.calls += 1
        body = json.loads(req.content)
        m, params = body["method"], body["params"]
        result: object
        if m == "eth_chainId":
            result = hex(8453)
        elif m == "eth_blockNumber":
            result = hex(BLOCK0 + 50_000)
        elif m == "eth_getBlockByNumber":
            n = int(params[0], 16)
            result = {"number": hex(n), "timestamp": hex((T0 + (n - BLOCK0) * 2000) // 1000)}
        elif m == "eth_call":
            to, data, blk = params[0]["to"].lower(), params[0]["data"], int(params[1], 16)
            sel = data[:10]
            if sel == "0x95d89b41":
                sym = next((s for s, a in BASKET if a.lower() == to), "USDC" if to == USDC else "?")
                if sym == "DEGEN":
                    sym = "NOTDEGEN"
                raw = sym.encode()
                result = "0x" + (32).to_bytes(32, "big").hex() + len(raw).to_bytes(32, "big").hex() + raw.ljust(32, b"\x00").hex()
            elif sel == "0x1698ee82" and to == V3_FACTORY.lower():
                a, b, fee = "0x" + data[34:74], "0x" + data[98:138], int(data[138:202], 16)
                token = a if b == WETH else b
                pool = self.pools.get((token, fee))
                result = "0x" + (pool[2:] if pool else "").rjust(64, "0")
            elif sel == "0x1a686502":
                result = "0x" + hex(self.liq.get(to, 0))[2:].rjust(64, "0")
            elif sel == "0x3850c7bd":
                start, end = self.price[to]
                p = start if blk < BLOCK0 + 200 + 43_000 else end
                result = "0x" + hex(_sqrt(p))[2:].rjust(64, "0") + "0" * 64 * 6
            else:
                result = "0x"
        else:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32601, "message": "no"}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})


def _day_pack(tmp_path: Path) -> Path:
    d = tmp_path / "base_day_2026-09-12"
    make_cl_pack(d)
    m = yaml.safe_load((d / "manifest.yaml").read_text())
    m["chain_id"] = 8453
    m["period"]["start_utc_ms"] = START_MS
    m["period"]["end_utc_ms"] = START_MS + 86_400_000
    m.setdefault("universe", {})["indexed_block_ranges"] = [[BLOCK0 + 200, BLOCK0 + 200 + 43_200]]
    (d / "manifest.yaml").write_text(yaml.safe_dump(m, sort_keys=False))
    return d


def test_basket_is_read_at_the_first_and_last_block(tmp_path: Path):
    d = _day_pack(tmp_path)
    fake = FakeBase()
    eco = collect_ecosystem(d, rpc_url_override="http://fake-rpc.local", transport=httpx.MockTransport(fake.handle), sleep=lambda s: None)
    assert eco["basis"] == ECOSYSTEM_BASIS
    assert eco["start_block"] == BLOCK0 + 200 and eco["end_block"] == BLOCK0 + 200 + 43_200 - 1
    got = {t["symbol"]: t for t in eco["tokens"]}
    assert set(got) == {"BRETT", "TOSHI", "VIRTUAL", "cbBTC"}  # DEGEN wrong symbol, AERO no liquidity
    assert all(t["pool_fee_pips"] == 10000 for t in got.values())  # the deeper pool wins
    assert abs(float(got["BRETT"]["return_vs_eth"]) - 0.10) < 1e-6 and abs(float(got["TOSHI"]["return_vs_eth"]) + 0.5) < 1e-6
    assert abs(float(eco["equal_weight_return_vs_eth"]) - (0.1 * 3 - 0.5) / 4) < 1e-6
    assert abs(float(eco["eth_usd_return"]) - 0.10) < 1e-6
    assert any("DEGEN" in n and "left out" in n for n in eco["notes"]) and any("AERO" in n for n in eco["notes"])
    assert eco["budget"]["requests"] < 200 and fake.calls == eco["budget"]["requests"]


def test_ecosystem_merges_into_the_baseline_and_survives_regeneration(tmp_path: Path):
    d = _day_pack(tmp_path)
    write_market_baseline(d)
    fake = FakeBase()
    write_ecosystem(d, rpc_url_override="http://fake-rpc.local", transport=httpx.MockTransport(fake.handle), sleep=lambda s: None)
    b = read_market_baseline(d)
    assert b and b["ecosystem"]["tokens"] and b["all_pools"]["pools"] == 2
    sentence = baseline_sentence(b)
    assert sentence.startswith("Market that day: the large Base tokens (BRETT, TOSHI, VIRTUAL, cbBTC)") and "ETH itself moved +10.0% in dollars" in sentence
    write_market_baseline(d)  # the tape-derived part is rebuilt, the RPC-derived part is kept
    assert read_market_baseline(d)["ecosystem"] == b["ecosystem"]
    assert json.loads((d / BASELINE_FILE).read_text())["ecosystem"]["basis"] == ECOSYSTEM_BASIS
