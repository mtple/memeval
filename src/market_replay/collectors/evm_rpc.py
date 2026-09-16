"""EVM JSON-RPC client (read-only) with chain-id and contract-interface verification."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .base import HttpCollector, ProviderError
from .keccak import event_topic, selector

TOPIC_PAIR_CREATED = event_topic("PairCreated(address,address,address,uint256)")
TOPIC_SWAP = event_topic("Swap(address,uint256,uint256,uint256,uint256,address)")
TOPIC_MINT = event_topic("Mint(address,uint256,uint256)")
TOPIC_BURN = event_topic("Burn(address,uint256,uint256,address)")
TOPIC_SYNC = event_topic("Sync(uint112,uint112)")

SEL_ALL_PAIRS_LENGTH = selector("allPairsLength()")
SEL_FEE_TO_SETTER = selector("feeToSetter()")
SEL_TOKEN0 = selector("token0()")
SEL_TOKEN1 = selector("token1()")
SEL_GET_RESERVES = selector("getReserves()")
SEL_DECIMALS = selector("decimals()")
SEL_SYMBOL = selector("symbol()")


def _rpc_app_error(parsed: Any) -> str | None:
    if isinstance(parsed, dict) and parsed.get("error"):
        e = parsed["error"]
        return f"{e.get('code')}: {e.get('message')}" if isinstance(e, dict) else str(e)
    return None


def hex_to_int(h: str) -> int:
    return int(h, 16)


def word(data_hex: str, i: int) -> int:
    d = data_hex[2:] if data_hex.startswith("0x") else data_hex
    return int(d[i * 64 : (i + 1) * 64] or "0", 16)


def topic_address(topic: str) -> str:
    return "0x" + topic[-40:].lower()


@dataclass
class RpcClient:
    url: str
    http: HttpCollector
    _id: int = 0

    def call(self, method: str, params: list[Any]) -> Any:
        self._id += 1
        body = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        parsed, _rid = self.http.request("POST", self.url, json_body=body, application_error_check=_rpc_app_error)
        return parsed.get("result")

    def chain_id(self) -> int:
        return hex_to_int(self.call("eth_chainId", []))

    def block_number(self) -> int:
        return hex_to_int(self.call("eth_blockNumber", []))

    def block(self, number: int) -> dict[str, Any] | None:
        return self.call("eth_getBlockByNumber", [hex(number), False])

    def block_timestamp_ms(self, number: int) -> int | None:
        b = self.block(number)
        if not b:
            return None
        return hex_to_int(b["timestamp"]) * 1000

    def eth_call(self, to: str, data: str, block: str | int = "latest") -> str:
        blk = hex(block) if isinstance(block, int) else block
        return self.call("eth_call", [{"to": to, "data": data}, blk])

    def get_logs(self, *, address: str | list[str] | None, topics: list[Any], from_block: int, to_block: int) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"fromBlock": hex(from_block), "toBlock": hex(to_block), "topics": topics}
        if address is not None:
            params["address"] = address
        res = self.call("eth_getLogs", [params])
        if not isinstance(res, list):
            raise ProviderError("eth_getLogs returned a non-list result")
        return res

    # ------------------------------------------------------------------ verification helpers
    def verify_factory(self, factory: str) -> dict[str, Any]:
        """Check the contract answers the v2 factory interface. Never guesses an address."""
        out: dict[str, Any] = {"factory": factory}
        try:
            n = hex_to_int(self.eth_call(factory, SEL_ALL_PAIRS_LENGTH))
            out["all_pairs_length"] = n
            setter = self.eth_call(factory, SEL_FEE_TO_SETTER)
            out["fee_to_setter"] = "0x" + setter[-40:]
            out["interface_ok"] = n >= 0 and len(setter) >= 42
        except (ProviderError, ValueError, TypeError) as e:
            out["interface_ok"] = False
            out["error"] = str(e)
        return out

    def find_block_at_or_after(self, target_ms: int, lo: int, hi: int) -> tuple[int, int]:
        """Binary search block boundary by timestamp; returns (block, timestamp_ms). Budgeted by the caller."""
        lo_ts = self.block_timestamp_ms(lo)
        hi_ts = self.block_timestamp_ms(hi)
        if lo_ts is None or hi_ts is None:
            raise ProviderError("block boundary lookup failed")
        if lo_ts >= target_ms:
            return lo, lo_ts
        if hi_ts < target_ms:
            raise ProviderError("target time is after the highest block")
        while hi - lo > 1:
            mid = (lo + hi) // 2
            ts = self.block_timestamp_ms(mid)
            if ts is None:
                raise ProviderError("block lookup failed")
            if ts >= target_ms:
                hi, hi_ts = mid, ts
            else:
                lo, lo_ts = mid, ts
        return hi, hi_ts
