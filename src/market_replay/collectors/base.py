"""Shared collector infrastructure: budgets, checkpoints, raw receipts, coverage ledger, backoff."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from ..domain.status import CoverageState

SECRET_QUERY_KEYS = {"apikey", "api_key", "key", "token", "x_cg_pro_api_key"}
SECRET_HEADER_KEYS = {"authorization", "x-api-key", "x-cg-pro-api-key", "x-cg-demo-api-key"}


class BudgetExhausted(RuntimeError):
    pass


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False, status: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status


def now_ms() -> int:
    return int(time.time() * 1000)


def utc_iso(ms: int | None = None) -> str:
    return datetime.fromtimestamp((ms or now_ms()) / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(slots=True)
class Budget:
    max_requests: int
    max_response_bytes: int = 200 * 1024 * 1024
    requests: int = 0
    response_bytes: int = 0
    retries: int = 0
    failures: int = 0

    def charge(self, nbytes: int = 0) -> None:
        if self.requests >= self.max_requests:
            raise BudgetExhausted(f"request budget of {self.max_requests} exhausted")
        self.requests += 1
        self.response_bytes += nbytes
        if self.response_bytes > self.max_response_bytes:
            raise BudgetExhausted("response byte budget exhausted")

    def as_dict(self) -> dict[str, int]:
        return {"max_requests": self.max_requests, "requests": self.requests, "response_bytes": self.response_bytes, "retries": self.retries, "failures": self.failures}


class Checkpoints:
    """JSON file of named cursors; written atomically after every advance so runs are resumable."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = {}
        if path.exists():
            self.data = json.loads(path.read_text())

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value
        self.flush()

    def flush(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True))
        tmp.replace(self.path)


def redact_request(url: str, params: dict[str, Any] | None, headers: dict[str, str] | None, body: Any) -> dict[str, Any]:
    clean_params = {k: ("[redacted]" if k.lower() in SECRET_QUERY_KEYS else v) for k, v in (params or {}).items()}
    clean_headers = {k: ("[redacted]" if k.lower() in SECRET_HEADER_KEYS else v) for k, v in (headers or {}).items()}
    u = httpx.URL(url)
    # Strip credentials embedded in URL paths/userinfo (e.g. https://user:key@rpc.example/KEY)
    safe_url = str(u.copy_with(username=None, password=None, query=None))
    if len(u.path.strip("/").split("/")) >= 1 and any(len(seg) >= 24 for seg in u.path.split("/")):
        safe_url = str(u.copy_with(path="/[redacted-path]", username=None, password=None, query=None))
    return {"url": safe_url, "params": clean_params, "headers": clean_headers, "body": body}


class ReceiptStore:
    """Append-only raw receipts (JSONL index + body files). Content hashes verify stored bytes only."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.index = root / "receipts.jsonl"
        self.count = sum(1 for _ in self.index.open()) if self.index.exists() else 0

    def record(self, *, provider: str, request: dict[str, Any], http_status: int | None, body: bytes | None, application_error: str | None, requested_ms: int, received_ms: int) -> str:
        self.count += 1
        rid = f"rcpt_{provider}_{self.count:08d}"
        body_hash = None
        body_path = None
        if body is not None:
            body_hash = hashlib.sha256(body).hexdigest()
            body_path = self.root / "bodies" / f"{rid}.bin"
            body_path.parent.mkdir(exist_ok=True)
            body_path.write_bytes(body)
        rec = {
            "receipt_id": rid,
            "provider": provider,
            "request": request,
            "http_status": http_status,
            "application_error": application_error,
            "body_sha256": body_hash,
            "body_path": str(body_path.relative_to(self.root)) if body_path else None,
            "requested_utc_ms": requested_ms,
            "received_utc_ms": received_ms,
            "integrity_hash_basis": "sha256_of_stored_bytes",
        }
        with self.index.open("a") as f:
            f.write(json.dumps(rec, sort_keys=True) + "\n")
        return rid


class CoverageLedger:
    """States for every requested block/page/time interval."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.rows: list[dict[str, Any]] = []
        if path.exists():
            self.rows = json.loads(path.read_text()).get("intervals", [])

    def set(self, object_ref: str, field_name: str, start: int, end: int, state: CoverageState, evidence: str, unit: str = "block", gaps: list[dict[str, Any]] | None = None) -> None:
        for r in self.rows:
            if r["object_ref"] == object_ref and r["field"] == field_name and r["start"] == start and r["end"] == end and r["unit"] == unit:
                r.update({"state": str(state), "evidence": evidence, "gaps": gaps or [], "updated_utc": utc_iso()})
                self.flush()
                return
        self.rows.append({"object_ref": object_ref, "field": field_name, "start": start, "end": end, "unit": unit, "state": str(state), "evidence": evidence, "gaps": gaps or [], "updated_utc": utc_iso()})
        self.flush()

    def pending(self, object_ref: str, field_name: str) -> list[dict[str, Any]]:
        return [r for r in self.rows if r["object_ref"] == object_ref and r["field"] == field_name and r["state"] in ("pending", "failed", "partial")]

    def flush(self) -> None:
        self.path.write_text(json.dumps({"schema": "coverage_ledger_v1", "intervals": self.rows}, indent=2, sort_keys=True))

    def summary(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.rows:
            out[r["state"]] = out.get(r["state"], 0) + 1
        return out


@dataclass
class HttpCollector:
    provider: str
    budget: Budget
    receipts: ReceiptStore
    errors_path: Path
    transport: httpx.BaseTransport | None = None
    max_retries: int = 4
    backoff_base_s: float = 0.5
    sleep: Any = time.sleep
    headers: dict[str, str] = field(default_factory=dict)
    _client: httpx.Client | None = None

    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=30, transport=self.transport, headers=self.headers)
        return self._client

    def _persist_error(self, kind: str, detail: str, request: dict[str, Any]) -> None:
        self.budget.failures += 1
        with self.errors_path.open("a") as f:
            f.write(json.dumps({"utc": utc_iso(), "kind": kind, "detail": detail[:500], "request": request}, sort_keys=True) + "\n")

    def request(self, method: str, url: str, *, params: dict[str, Any] | None = None, json_body: Any = None, application_error_check=None) -> tuple[Any, str]:
        """Perform a budgeted request with backoff. Returns (parsed_json, receipt_id). Raises ProviderError/BudgetExhausted."""
        req_meta = redact_request(url, params, self.headers, json_body)
        attempt = 0
        while True:
            self.budget.charge()
            requested = now_ms()
            try:
                r = self.client().request(method, url, params=params, json=json_body)
            except httpx.HTTPError as e:
                received = now_ms()
                self._persist_error("transport", str(e), req_meta)
                self.receipts.record(provider=self.provider, request=req_meta, http_status=None, body=None, application_error=f"transport: {e}", requested_ms=requested, received_ms=received)
                if attempt < self.max_retries:
                    attempt += 1
                    self.budget.retries += 1
                    self.sleep(self.backoff_base_s * (2**attempt))
                    continue
                raise ProviderError(f"transport failure after retries: {e}", retryable=True) from e
            received = now_ms()
            body = r.content
            self.budget.response_bytes += len(body)
            app_err = None
            parsed: Any = None
            try:
                parsed = r.json()
            except ValueError:
                app_err = "non-JSON body"
            if parsed is not None and application_error_check is not None:
                app_err = application_error_check(parsed)
            rid = self.receipts.record(provider=self.provider, request=req_meta, http_status=r.status_code, body=body, application_error=app_err, requested_ms=requested, received_ms=received)
            if r.status_code in (429, 500, 502, 503, 504) or (app_err and "rate" in app_err.lower()):
                self._persist_error("retryable", f"status {r.status_code} {app_err or ''}", req_meta)
                if attempt < self.max_retries:
                    attempt += 1
                    self.budget.retries += 1
                    self.sleep(self.backoff_base_s * (2**attempt))
                    continue
                raise ProviderError(f"provider limit persisted: status {r.status_code} {app_err}", retryable=True, status=r.status_code)
            if r.status_code >= 400:
                self._persist_error("http", f"status {r.status_code}", req_meta)
                raise ProviderError(f"http {r.status_code}", status=r.status_code)
            if app_err:
                # HTTP success + application failure stays a failure.
                self._persist_error("application", app_err, req_meta)
                raise ProviderError(f"application error: {app_err}", status=r.status_code)
            return parsed, rid
