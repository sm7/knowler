"""Tests for IPC envelope types and helpers."""
import json
import pytest

from knowler_engine.ipc.types import (
    Request,
    SuccessResponse,
    ErrorResponse,
    Event,
    ErrorCode,
    make_success_response,
    make_error_response,
    make_event,
)


def test_request_round_trip():
    req = Request(id="req-1", method="engine.handshake", params={"version": "1"})
    data = req.model_dump()
    assert data["id"] == "req-1"
    assert data["method"] == "engine.handshake"
    assert data["params"]["version"] == "1"


def test_request_from_dict():
    raw = {"id": "r1", "method": "project.create", "params": {"name": "Test"}}
    req = Request(**raw)
    assert req.method == "project.create"


def test_request_defaults_empty_params():
    req = Request(id="r1", method="engine.handshake")
    assert req.params == {}


def test_make_success_response():
    resp = make_success_response("req-1", {"project_id": "abc"})
    assert resp.id == "req-1"
    assert resp.result["project_id"] == "abc"
    assert resp.ok is True


def test_make_error_response_known_code():
    resp = make_error_response("req-1", ErrorCode.NOT_FOUND, "project not found")
    assert resp.id == "req-1"
    assert resp.error is not None
    assert resp.error.code == ErrorCode.NOT_FOUND
    assert resp.error.message == "project not found"
    assert resp.ok is False


def test_make_error_response_with_details():
    resp = make_error_response("req-2", ErrorCode.CONFLICT, "hash mismatch", {"path": "/foo"})
    assert resp.error.details["path"] == "/foo"


def test_make_error_response_serializable():
    resp = make_error_response("req-2", ErrorCode.CONFLICT, "hash mismatch", {"path": "/foo"})
    data = resp.model_dump()
    json.dumps(data)  # Must be JSON-serializable
    assert data["error"]["code"] == ErrorCode.CONFLICT


def test_make_event():
    evt = make_event("job.progress", {"job_id": "j1", "progress": 0.5})
    assert evt.event == "job.progress"
    assert evt.payload["progress"] == 0.5


def test_event_serializable():
    evt = make_event("engine.ready", {"version": "0.1"})
    data = evt.model_dump()
    json.dumps(data)
    assert "event" in data
    assert "payload" in data


def test_event_type_field():
    evt = make_event("test.event", {})
    assert evt.type == "event"


def test_success_response_type_field():
    resp = make_success_response("r1", {})
    assert resp.type == "response"


def test_error_response_type_field():
    resp = make_error_response("r1", ErrorCode.INTERNAL_ERROR, "oops")
    assert resp.type == "response"


def test_all_error_codes_exist():
    """Verify all error codes from architecture §41.12 are defined."""
    required = [
        "VALIDATION_ERROR", "NOT_FOUND", "CONFLICT", "IO_ERROR", "PARSER_ERROR",
        "NORMALIZATION_ERROR", "LLM_OUTPUT_INVALID", "MODEL_PROVIDER_ERROR",
        "RATE_LIMITED", "JOB_CANCELLED", "INTERNAL_ERROR", "NOT_IMPLEMENTED",
    ]
    for code in required:
        assert hasattr(ErrorCode, code), f"ErrorCode.{code} missing"
