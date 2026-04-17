"""
Typed IPC envelope definitions.

Every message over the stdio NDJSON transport uses one of these envelopes.
All models use pydantic v2.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------


class Request(BaseModel):
    type: Literal["request"] = "request"
    id: str
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Response
# ---------------------------------------------------------------------------


class ErrorPayload(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class SuccessResponse(BaseModel):
    type: Literal["response"] = "response"
    id: str
    ok: Literal[True] = True
    result: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    type: Literal["response"] = "response"
    id: str
    ok: Literal[False] = False
    error: ErrorPayload


# ---------------------------------------------------------------------------
# Events (server → client, no request_id)
# ---------------------------------------------------------------------------


class Event(BaseModel):
    type: Literal["event"] = "event"
    event: str
    payload: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Error codes (keep in sync with architecture doc §41.12)
# ---------------------------------------------------------------------------


class ErrorCode:
    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    IO_ERROR = "IO_ERROR"
    PARSER_ERROR = "PARSER_ERROR"
    NORMALIZATION_ERROR = "NORMALIZATION_ERROR"
    LLM_OUTPUT_INVALID = "LLM_OUTPUT_INVALID"
    MODEL_PROVIDER_ERROR = "MODEL_PROVIDER_ERROR"
    RATE_LIMITED = "RATE_LIMITED"
    JOB_CANCELLED = "JOB_CANCELLED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"


def make_error_response(
    request_id: str,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> ErrorResponse:
    return ErrorResponse(
        id=request_id,
        error=ErrorPayload(code=code, message=message, details=details or {}),
    )


def make_success_response(request_id: str, result: dict[str, Any]) -> SuccessResponse:
    return SuccessResponse(id=request_id, result=result)


def make_event(event_name: str, payload: dict[str, Any]) -> Event:
    return Event(event=event_name, payload=payload)
