"""Agent-facing response envelope shared by HTTP, SDK and MCP."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .status import AvailabilityBasis, Completeness, ErrorCode


class Quality(BaseModel):
    completeness: Completeness = Completeness.UNKNOWN
    availability_basis: AvailabilityBasis = AvailabilityBasis.UNKNOWN
    observed_through_ms: int | None = None
    stale: bool = False
    warnings: list[str] = Field(default_factory=list)


class EnvelopeError(BaseModel):
    code: ErrorCode
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class Envelope(BaseModel):
    request_id: str
    session_id: str
    clock_ms: int
    status: str  # "ok" | "error"
    data: dict[str, Any] | None = None
    quality: Quality = Field(default_factory=Quality)
    error: EnvelopeError | None = None

    @classmethod
    def ok(
        cls,
        *,
        request_id: str,
        session_id: str,
        clock_ms: int,
        data: dict[str, Any],
        quality: Quality | None = None,
    ) -> Envelope:
        return cls(
            request_id=request_id,
            session_id=session_id,
            clock_ms=clock_ms,
            status="ok",
            data=data,
            quality=quality or Quality(),
        )

    @classmethod
    def fail(
        cls,
        *,
        request_id: str,
        session_id: str,
        clock_ms: int,
        code: ErrorCode,
        message: str,
        details: dict[str, Any] | None = None,
        quality: Quality | None = None,
    ) -> Envelope:
        return cls(
            request_id=request_id,
            session_id=session_id,
            clock_ms=clock_ms,
            status="error",
            data=None,
            quality=quality or Quality(),
            error=EnvelopeError(code=code, message=message, details=details or {}),
        )
