"""
Stdio NDJSON transport layer.

Reads newline-delimited JSON from stdin, writes to stdout.
stderr is reserved for human-readable logs.

Design rules:
- stdout is ONLY for protocol messages (one JSON object per line)
- stderr is ONLY for log output
- never mix logs into stdout
- each write is flushed immediately
"""
from __future__ import annotations

import asyncio
import json
import sys
from typing import AsyncIterator

import structlog

from knowler_engine.ipc.types import (
    ErrorResponse,
    Event,
    Request,
    SuccessResponse,
)

log = structlog.get_logger(__name__)

# Protocol version this engine speaks
PROTOCOL_VERSION = 1
ENGINE_VERSION = "0.1.0"


class Transport:
    """Async stdio NDJSON transport."""

    def __init__(self) -> None:
        self._write_lock = asyncio.Lock()

    async def read_messages(self) -> AsyncIterator[Request]:
        """Yield parsed Request objects from stdin."""
        loop = asyncio.get_event_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)

        while True:
            try:
                line_bytes = await reader.readline()
            except asyncio.IncompleteReadError:
                break

            if not line_bytes:
                # EOF — engine should shut down
                log.info("stdin_eof_engine_shutting_down")
                break

            line = line_bytes.decode("utf-8").strip()
            if not line:
                continue

            try:
                data = json.loads(line)
            except json.JSONDecodeError as exc:
                log.warning("invalid_json_on_stdin", error=str(exc), line=line[:200])
                continue

            if data.get("type") != "request":
                log.warning("unexpected_message_type", type=data.get("type"))
                continue

            try:
                yield Request.model_validate(data)
            except Exception as exc:
                log.warning("request_parse_failed", error=str(exc))
                continue

    async def send(self, message: SuccessResponse | ErrorResponse | Event) -> None:
        """Write a single NDJSON message to stdout."""
        payload = message.model_dump_json()
        async with self._write_lock:
            sys.stdout.write(payload + "\n")
            sys.stdout.flush()

    async def send_raw(self, data: dict) -> None:
        """Write arbitrary dict as NDJSON to stdout (for handshake before typed models)."""
        async with self._write_lock:
            sys.stdout.write(json.dumps(data) + "\n")
            sys.stdout.flush()
