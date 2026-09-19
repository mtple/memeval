"""The crypto market as a whole on a recorded day: the combined market value of the ten largest
coins at the start and the end of the day, from CoinGecko's public API.

Total market capitalisation history is not free anywhere, so the line is the top ten by market
value (about 85% of the whole market) and is labelled that way. One request per coin per day,
paced under the free tier's limit, read-only and budgeted like every collector; never imported by
the engine. Anonymous calls from a shared address (a CI runner) are refused with 429, so the
free demo key is read from the environment variable ``COINGECKO_API_KEY`` and sent as a header.
The sandbox this code is tested in cannot reach the API, so the tests use a fake transport and
the recording workflow does the real reads.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import yaml

from ..datasets.baseline import BASELINE_FILE
from .base import Budget, HttpCollector, ProviderError, ReceiptStore

CRYPTO_BASIS = "coingecko_top10_market_cap_v1"
API = "https://api.coingecko.com/api/v3"
# the ten largest coins by market value when this was written; a fixed list so every day is measured the same way
KEY_ENV = "COINGECKO_API_KEY"
PACE_S = 2.5  # the demo tier allows 30 calls a minute; stay well under it
TOP10 = ("bitcoin", "ethereum", "tether", "ripple", "binancecoin", "solana", "usd-coin", "dogecoin", "tron", "cardano")
CAVEATS = [
    "The crypto market line is the combined market value of ten named large coins (about 85% of the whole market), not a total market capitalisation, which no free source provides historically.",
    "Values are CoinGecko's hourly market-cap points nearest the day's start and end, not exchange closes.",
]


def _nearest(points: list[list[float]], t_ms: int) -> float | None:
    if not points:
        return None
    ts, value = min(points, key=lambda p: abs(p[0] - t_ms))
    return float(value) if abs(ts - t_ms) <= 3 * 3_600_000 else None


def collect_crypto_market(pack_dir: Path | str, *, transport=None, sleep=None, max_requests: int = 40, base_url: str = API, api_key: str | None = None) -> dict[str, Any]:
    p = Path(pack_dir)
    period = yaml.safe_load((p / "manifest.yaml").read_text())["period"]
    start_ms, end_ms = int(period["start_utc_ms"]), int(period["end_utc_ms"])
    work = p.parent / (p.name + "_work")
    work.mkdir(parents=True, exist_ok=True)
    budget = Budget(max_requests=max_requests, max_response_bytes=32 * 1024 * 1024)
    key = api_key if api_key is not None else os.environ.get(KEY_ENV, "")
    http = HttpCollector(provider="coingecko", budget=budget, receipts=ReceiptStore(work / "receipts_crypto", store_bodies=False), errors_path=work / "errors_crypto.jsonl", transport=transport, headers={"x-cg-demo-api-key": key} if key else {})
    pause = sleep if sleep is not None else time.sleep
    if sleep is not None:
        http.sleep = sleep
    coins: list[dict[str, Any]] = []
    notes: list[str] = []
    if not key:
        notes.append(f"no {KEY_ENV} set: anonymous calls, which a shared address soon exhausts")
    for i, cid in enumerate(TOP10):
        if i:
            pause(PACE_S)
        params = {"vs_currency": "usd", "from": str(start_ms // 1000 - 3600), "to": str(end_ms // 1000 + 3600)}
        parsed, _rid = http.request("GET", f"{base_url}/coins/{cid}/market_chart/range", params=params, application_error_check=lambda d: d.get("error") or (d.get("status") or {}).get("error_message") if isinstance(d, dict) else "unexpected body")
        caps = parsed.get("market_caps") or []
        a, b = _nearest(caps, start_ms), _nearest(caps, end_ms)
        if a is None or b is None or a <= 0:
            notes.append(f"{cid}: no market-cap point within three hours of the day's edges; left out")
            continue
        coins.append({"id": cid, "market_cap_start_usd": f"{a:.0f}", "market_cap_end_usd": f"{b:.0f}", "return": f"{b / a - 1:.6f}"})
    total_a = sum(float(c["market_cap_start_usd"]) for c in coins)
    total_b = sum(float(c["market_cap_end_usd"]) for c in coins)
    if len(coins) < 5 or total_a <= 0:
        raise ProviderError(f"only {len(coins)} of {len(TOP10)} coins could be read; the market line needs at least five")
    return {
        "basis": CRYPTO_BASIS,
        "coins": coins,
        "coins_expected": list(TOP10),
        "market_cap_start_usd": f"{total_a:.0f}",
        "market_cap_end_usd": f"{total_b:.0f}",
        "return": f"{total_b / total_a - 1:.6f}",
        "notes": notes,
        "caveats": list(CAVEATS),
        "budget": budget.as_dict(),
    }


def write_crypto_market(pack_dir: Path | str, **kw: Any) -> dict[str, Any]:
    p = Path(pack_dir)
    section = collect_crypto_market(p, **kw)
    fp = p / BASELINE_FILE
    try:
        data = json.loads(fp.read_text())
    except (OSError, ValueError):
        data = {}
    data["crypto_market"] = section
    fp.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    return section
