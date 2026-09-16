"""Narrow inference gateway for model-based participants.

Provider credentials stay in the gateway process; the agent only sees a local
endpoint. Provider-side browsing/retrieval tools are never enabled. The gateway
records model identifier, settings and returned usage. The ``mock`` provider makes
tests and demos runnable without paid inference. Default spend authorization is
zero: a configured credential does not itself authorize billable calls.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx


class GatewayError(RuntimeError):
    pass


@dataclass(slots=True)
class UsageRecord:
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: str


@dataclass
class ModelGateway:
    provider: str = "mock"
    model: str = "mock-deterministic"
    spend_limit_usd: Decimal = Decimal("0")
    max_tokens: int = 512
    usage: list[UsageRecord] = field(default_factory=list)
    spent_usd: Decimal = Decimal("0")
    _client: httpx.Client | None = None

    @classmethod
    def from_env(cls) -> ModelGateway:
        provider = os.environ.get("MARKET_REPLAY_INFERENCE_PROVIDER", "mock")
        model = os.environ.get("MARKET_REPLAY_INFERENCE_MODEL", "mock-deterministic")
        limit = Decimal(os.environ.get("MARKET_REPLAY_INFERENCE_SPEND_LIMIT_USD", "0"))
        return cls(provider=provider, model=model, spend_limit_usd=limit)

    def complete(self, *, system: str, messages: list[dict[str, str]], tools_schema: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        if self.provider == "mock":
            return self._mock(system, messages, tools_schema or [])
        if self.provider == "anthropic":
            return self._anthropic(system, messages, tools_schema or [])
        raise GatewayError(f"unsupported provider {self.provider}")

    # ------------------------------------------------------------------ providers
    def _mock(self, system: str, messages: list[dict[str, str]], tools_schema: list[dict[str, Any]]) -> dict[str, Any]:
        """Deterministic tool-choice policy for tests: read state, then wait. Never trades."""
        last = messages[-1]["content"] if messages else ""
        step = last.count("\n")
        if "portfolio" not in last:
            choice = {"tool": "portfolio.get", "arguments": {}}
        else:
            choice = {"tool": "clock.advance", "arguments": {"next_event": True, "max_ms": 3_600_000}}
        text = json.dumps(choice)
        self.usage.append(UsageRecord(self.provider, self.model, len(last) // 4, len(text) // 4, "0"))
        return {"text": text, "model": self.model, "usage": {"input_tokens": len(last) // 4, "output_tokens": len(text) // 4}, "step": step}

    def _anthropic(self, system: str, messages: list[dict[str, str]], tools_schema: list[dict[str, Any]]) -> dict[str, Any]:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise GatewayError("ANTHROPIC_API_KEY not configured in the gateway environment")
        if self.spend_limit_usd <= 0:
            raise GatewayError("inference spend limit is zero; a configured credential does not authorize billable calls")
        if self.spent_usd >= self.spend_limit_usd:
            raise GatewayError("inference spend limit reached")
        if self._client is None:
            self._client = httpx.Client(timeout=60)
        body = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system + "\nRespond with a single JSON object {\"tool\": ..., \"arguments\": {...}}.",
            "messages": messages,
        }
        r = self._client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json=body,
        )
        if r.status_code != 200:
            raise GatewayError(f"provider error {r.status_code}: {r.text[:200]}")
        data = r.json()
        text = "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")
        usage = data.get("usage", {})
        in_t = int(usage.get("input_tokens", 0))
        out_t = int(usage.get("output_tokens", 0))
        # Conservative placeholder pricing; real pricing must be configured per model.
        cost = Decimal(in_t) * Decimal("0.000003") + Decimal(out_t) * Decimal("0.000015")
        self.spent_usd += cost
        self.usage.append(UsageRecord("anthropic", data.get("model", self.model), in_t, out_t, str(cost)))
        return {"text": text, "model": data.get("model", self.model), "usage": usage}

    def summary(self) -> dict[str, Any]:
        return {
            "recorded": True,
            "provider": self.provider,
            "model": self.model,
            "calls": len(self.usage),
            "input_tokens": sum(u.input_tokens for u in self.usage),
            "output_tokens": sum(u.output_tokens for u in self.usage),
            "estimated_cost_usd": str(self.spent_usd),
            "spend_limit_usd": str(self.spend_limit_usd),
            "provider_tools_enabled": False,
        }
