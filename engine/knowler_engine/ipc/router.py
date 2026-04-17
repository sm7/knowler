"""
IPC method router.

Routes incoming Request objects to registered handler functions.
Handlers are async callables: (params: dict, ctx: AppContext) -> dict
Errors should raise EngineError or its subclasses.
"""
from __future__ import annotations

import traceback
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import structlog

from knowler_engine.ipc.types import (
    ErrorCode,
    Event,
    Request,
    make_error_response,
    make_event,
    make_success_response,
)

if TYPE_CHECKING:
    from knowler_engine.ipc.transport import Transport

log = structlog.get_logger(__name__)

HandlerFn = Callable[..., Awaitable[dict[str, Any]]]


class EngineError(Exception):
    """Raised by handlers to produce a structured error response."""

    def __init__(
        self,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class Router:
    def __init__(self, transport: "Transport") -> None:
        self._transport = transport
        self._handlers: dict[str, HandlerFn] = {}
        self._context: Any = None

    def set_context(self, ctx: Any) -> None:
        self._context = ctx

    def register(self, method: str, handler: HandlerFn) -> None:
        self._handlers[method] = handler

    def method(self, name: str) -> Callable[[HandlerFn], HandlerFn]:
        """Decorator: @router.method('project.create')"""
        def decorator(fn: HandlerFn) -> HandlerFn:
            self.register(name, fn)
            return fn
        return decorator

    async def dispatch(self, request: Request) -> None:
        handler = self._handlers.get(request.method)
        if handler is None:
            resp = make_error_response(
                request.id,
                ErrorCode.NOT_IMPLEMENTED,
                f"Unknown method: {request.method}",
            )
            await self._transport.send(resp)
            return

        try:
            result = await handler(request.params, self._context)
            resp = make_success_response(request.id, result)
            await self._transport.send(resp)
        except EngineError as exc:
            log.warning("handler_engine_error", method=request.method, code=exc.code, msg=exc.message)
            resp = make_error_response(request.id, exc.code, exc.message, exc.details)
            await self._transport.send(resp)
        except Exception as exc:
            log.error(
                "handler_internal_error",
                method=request.method,
                error=str(exc),
                traceback=traceback.format_exc(),
            )
            resp = make_error_response(
                request.id,
                ErrorCode.INTERNAL_ERROR,
                f"Internal engine error: {type(exc).__name__}",
            )
            await self._transport.send(resp)

    async def emit(self, event_name: str, payload: dict[str, Any]) -> None:
        """Emit an unsolicited event to the frontend."""
        evt = make_event(event_name, payload)
        await self._transport.send(evt)
