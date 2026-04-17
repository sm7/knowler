"""Tests for the IPC router: dispatch, error handling, method registration."""
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock

from knowler_engine.ipc.router import Router, EngineError
from knowler_engine.ipc.types import Request, ErrorCode


@pytest.fixture
def transport():
    t = MagicMock()
    t.send = AsyncMock()
    return t


@pytest.fixture
def router(transport):
    return Router(transport=transport)


@pytest.mark.asyncio
async def test_router_dispatches_registered_method(router, transport):
    async def handler(params, ctx):
        return {"ok": True}

    router.register("test.method", handler)
    req = Request(id="r1", method="test.method", params={})
    await router.dispatch(req)

    transport.send.assert_called_once()
    sent = transport.send.call_args[0][0]
    assert sent.ok is True
    assert sent.result == {"ok": True}


@pytest.mark.asyncio
async def test_router_returns_not_implemented_for_unknown(router, transport):
    req = Request(id="r2", method="nonexistent.method", params={})
    await router.dispatch(req)

    transport.send.assert_called_once()
    sent = transport.send.call_args[0][0]
    assert sent.ok is False
    assert sent.error.code == ErrorCode.NOT_IMPLEMENTED


@pytest.mark.asyncio
async def test_router_catches_engine_error(router, transport):
    async def failing_handler(params, ctx):
        raise EngineError(ErrorCode.NOT_FOUND, "resource missing")

    router.register("test.fails", failing_handler)
    req = Request(id="r3", method="test.fails", params={})
    await router.dispatch(req)

    sent = transport.send.call_args[0][0]
    assert sent.ok is False
    assert sent.error.code == ErrorCode.NOT_FOUND
    assert sent.error.message == "resource missing"


@pytest.mark.asyncio
async def test_router_catches_unexpected_exception(router, transport):
    async def boom(params, ctx):
        raise ValueError("unexpected crash")

    router.register("test.boom", boom)
    req = Request(id="r4", method="test.boom", params={})
    await router.dispatch(req)

    sent = transport.send.call_args[0][0]
    assert sent.ok is False
    assert sent.error.code == ErrorCode.INTERNAL_ERROR


@pytest.mark.asyncio
async def test_router_method_decorator(router, transport):
    @router.method("decorated.method")
    async def handler(params, ctx):
        return {"decorated": True}

    req = Request(id="r5", method="decorated.method", params={})
    await router.dispatch(req)

    sent = transport.send.call_args[0][0]
    assert sent.ok is True
    assert sent.result == {"decorated": True}


@pytest.mark.asyncio
async def test_router_passes_params_to_handler(router, transport):
    received = []

    async def capture_handler(params, ctx):
        received.append(params)
        return {}

    router.register("test.capture", capture_handler)
    req = Request(id="r6", method="test.capture", params={"key": "value"})
    await router.dispatch(req)

    assert len(received) == 1
    assert received[0]["key"] == "value"


@pytest.mark.asyncio
async def test_router_passes_context_to_handler(router, transport):
    ctx_object = {"project_manager": "pm_instance"}
    router.set_context(ctx_object)
    received_ctx = []

    async def ctx_handler(params, ctx):
        received_ctx.append(ctx)
        return {}

    router.register("test.ctx", ctx_handler)
    req = Request(id="r7", method="test.ctx", params={})
    await router.dispatch(req)

    assert received_ctx[0] is ctx_object


@pytest.mark.asyncio
async def test_router_overwrite_registration(router, transport):
    async def handler1(params, ctx):
        return {"v": 1}

    async def handler2(params, ctx):
        return {"v": 2}

    router.register("test.overwrite", handler1)
    router.register("test.overwrite", handler2)
    req = Request(id="r8", method="test.overwrite", params={})
    await router.dispatch(req)

    sent = transport.send.call_args[0][0]
    assert sent.result == {"v": 2}


@pytest.mark.asyncio
async def test_router_emit_sends_event(router, transport):
    await router.emit("job.progress", {"job_id": "j1", "progress": 0.5})

    transport.send.assert_called_once()
    sent = transport.send.call_args[0][0]
    assert sent.type == "event"
    assert sent.event == "job.progress"
    assert sent.payload["progress"] == 0.5
