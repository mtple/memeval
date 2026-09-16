"""Optional GeckoTerminal/CoinGecko onchain enrichment collector (read-only, opt-in, budgeted).

Trades and OHLCV pages are stored as raw receipts and normalized with the same rules as the
report-excerpt importer: a page is a sample (coverage partial), an open bucket is open, absent
buckets are gaps. Nothing here becomes an executable tape.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from ..datasets.importer import classify_receipt, normalize_candles, normalize_trade_page
from .base import Budget, HttpCollector, ReceiptStore

PUBLIC_BASE = "https://api.geckoterminal.com/api/v2"


def _app_error(parsed: Any) -> str | None:
    if isinstance(parsed, dict):
        if parsed.get("errors"):
            return str(parsed["errors"])[:200]
        st = parsed.get("status")
        if isinstance(st, dict) and st.get("error_code"):
            return f"{st.get('error_code')}: {st.get('error_message')}"
    return None


class GeckoCollector:
    def __init__(self, work_dir: Path, *, max_requests: int, base_url: str = PUBLIC_BASE, api_key: str | None = None, transport: httpx.BaseTransport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.budget = Budget(max_requests=max_requests)
        headers = {"accept": "application/json"}
        if api_key:
            headers["x-cg-pro-api-key"] = api_key
        self.http = HttpCollector(provider="gecko", budget=self.budget, receipts=ReceiptStore(work_dir / "receipts"), errors_path=work_dir / "errors.jsonl", transport=transport, headers=headers)

    def pool_trades(self, network: str, pool: str) -> dict[str, Any]:
        parsed, rid = self.http.request("GET", f"{self.base_url}/networks/{network}/pools/{pool}/trades", application_error_check=_app_error)
        rows = [d.get("attributes", {}) for d in parsed.get("data", [])]
        norm = normalize_trade_page(rows, requested_limit=None, received_utc_ms=self.http.receipts.count and None)
        return {"receipt_id": rid, "rows": len(rows), "normalized": norm, "receipt": classify_receipt(200, parsed)}

    def pool_ohlcv(self, network: str, pool: str, timeframe: str = "minute", aggregate: int = 1, limit: int = 100) -> dict[str, Any]:
        parsed, rid = self.http.request("GET", f"{self.base_url}/networks/{network}/pools/{pool}/ohlcv/{timeframe}", params={"aggregate": aggregate, "limit": limit}, application_error_check=_app_error)
        rows = parsed.get("data", {}).get("attributes", {}).get("ohlcv_list", [])
        import time

        norm = normalize_candles(rows, interval_s=60 * aggregate if timeframe == "minute" else 3600 * aggregate, received_utc_ms=int(time.time() * 1000))
        return {"receipt_id": rid, "rows": len(rows), "normalized": norm}
