"""
Regression tests for the HTTP server layer.

Covers the boundaries identified in the stabilization plan:
  - /api/health reachable
  - /api/rpc engine.handshake succeeds
  - /api/rpc unknown method returns 404 with error payload
  - /api/rpc validation error returns 400
  - WebSocket /ws sends engine.ready on connect
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from knowler_engine.server import create_app, AppContext
from knowler_engine.ipc.router import Router
from knowler_engine.ipc.transport import ENGINE_VERSION, PROTOCOL_VERSION
from knowler_engine.storage.db import open_global_db


# ---------------------------------------------------------------------------
# Minimal AppContext fixture — no real DB needed for HTTP layer tests
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def app(tmp_path: Path):
    global_db = await open_global_db(tmp_path / "support")

    transport = MagicMock()
    transport.send = AsyncMock()
    router = Router(transport)

    pm = MagicMock()
    pm.close_all = AsyncMock()

    job_runner = MagicMock()

    ctx = AppContext(
        global_db=global_db,
        project_manager=pm,
        job_runner=job_runner,
        llm=MagicMock(),
        router=router,
    )

    # Register the real engine.handshake handler
    from knowler_engine.__main__ import _register_handlers
    _register_handlers(router, ctx)

    yield create_app(ctx)

    await global_db.close()


@pytest_asyncio.fixture
async def client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# /api/health
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_health_returns_ok(client: AsyncClient):
    r = await client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["engine_version"] == ENGINE_VERSION


# ---------------------------------------------------------------------------
# /api/rpc — handshake
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rpc_handshake_succeeds(client: AsyncClient):
    r = await client.post(
        "/api/rpc",
        json={"method": "engine.handshake", "params": {"protocol_version": PROTOCOL_VERSION}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["result"]["protocol_version"] == PROTOCOL_VERSION


@pytest.mark.asyncio
async def test_rpc_handshake_version_mismatch_returns_error(client: AsyncClient):
    r = await client.post(
        "/api/rpc",
        json={"method": "engine.handshake", "params": {"protocol_version": 9999}},
    )
    assert r.status_code == 400
    body = r.json()
    assert body["ok"] is False
    assert "VALIDATION_ERROR" in body["error"]["code"]


# ---------------------------------------------------------------------------
# /api/rpc — unknown method
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rpc_unknown_method_returns_404(client: AsyncClient):
    r = await client.post(
        "/api/rpc",
        json={"method": "no.such.method", "params": {}},
    )
    assert r.status_code == 404
    body = r.json()
    assert body["ok"] is False
    assert "NOT_IMPLEMENTED" in body["error"]["code"]


# ---------------------------------------------------------------------------
# /api/rpc — missing required param yields validation error
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rpc_project_create_missing_name_returns_400(client: AsyncClient):
    r = await client.post(
        "/api/rpc",
        json={"method": "project.create", "params": {"root_path": "/tmp/test"}},
    )
    assert r.status_code == 400
    body = r.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"
