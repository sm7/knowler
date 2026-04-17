"""
Knowler local web server.

Provides:
  - FastAPI HTTP server at http://localhost:PORT
  - POST /api/rpc        — JSON-RPC style: {method, params} → {ok, result/error}
  - GET  /ws             — WebSocket for engine events (job.progress etc.)
  - GET  /*              — serves the bundled React frontend

Usage:
  knowler                 # default port 7842
  knowler --port 9000
  knowler --no-browser    # don't auto-open browser
"""
import asyncio
import json
import os
import pathlib
import platform
import webbrowser
import logging
from typing import Any

import structlog
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from knowler_engine.app_settings import resolve_api_key, resolve_llm_provider
from knowler_engine.ipc.types import ErrorCode, Request
from knowler_engine.ipc.router import Router, EngineError
from knowler_engine.ipc.transport import PROTOCOL_VERSION, ENGINE_VERSION
from knowler_engine.storage.db import open_global_db
from knowler_engine.project.manager import ProjectManager
from knowler_engine.jobs.runner import JobRunner
from knowler_engine.llm.provider import LLMProvider

log = structlog.get_logger(__name__)

# ------------------------------------------------------------------
# WebSocket connection manager
# ------------------------------------------------------------------

class _ConnectionManager:
    def __init__(self) -> None:
        self._connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._connections = [c for c in self._connections if c is not ws]

    async def broadcast(self, data: dict) -> None:
        dead = []
        for ws in self._connections:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = _ConnectionManager()


# ------------------------------------------------------------------
# WebSocket transport (replaces stdio Transport)
# ------------------------------------------------------------------

class WebSocketTransport:
    """Sends engine responses/events to all connected WebSocket clients."""

    async def send(self, message: Any) -> None:
        data = json.loads(message.model_dump_json())
        await manager.broadcast(data)

    async def send_raw(self, data: dict) -> None:
        await manager.broadcast(data)


# ------------------------------------------------------------------
# App context (mirrors __main__.py AppContext)
# ------------------------------------------------------------------

class RPCRequest(BaseModel):
    id: str = ""
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


class AppContext:
    def __init__(self, global_db, project_manager, job_runner, llm, router):
        self.global_db = global_db
        self.project_manager = project_manager
        self.job_runner = job_runner
        self.llm = llm
        self.router = router


# ------------------------------------------------------------------
# FastAPI app factory
# ------------------------------------------------------------------

def create_app(ctx: AppContext) -> FastAPI:
    app = FastAPI(title="Knowler Engine", version=ENGINE_VERSION)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ----------------------------------------------------------------
    # WebSocket endpoint
    # ----------------------------------------------------------------

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        await manager.connect(ws)
        # Send engine.ready immediately to newly connected client
        await ws.send_json({
            "type": "event",
            "event": "engine.ready",
            "payload": {
                "protocol_version": PROTOCOL_VERSION,
                "engine_version": ENGINE_VERSION,
            },
        })
        try:
            while True:
                # Keep connection alive; clients don't send via WS
                await ws.receive_text()
        except WebSocketDisconnect:
            manager.disconnect(ws)

    # ----------------------------------------------------------------
    # RPC endpoint
    # ----------------------------------------------------------------

    @app.post("/api/rpc")
    async def rpc(body: RPCRequest):
        import uuid
        req_id = body.id or str(uuid.uuid4())
        try:
            request = Request(
                type="request",
                id=req_id,
                method=body.method,
                params=body.params,
            )
            # Dispatch returns None; response is sent via transport.
            # For HTTP we need the result directly, so call the handler directly.
            handler = ctx.router._handlers.get(body.method)
            if handler is None:
                return JSONResponse({"ok": False, "error": {
                    "code": ErrorCode.NOT_IMPLEMENTED,
                    "message": f"Method '{body.method}' not found",
                }}, status_code=404)

            result = await handler(body.params, ctx)
            return {"ok": True, "result": result}

        except EngineError as exc:
            return JSONResponse(
                {"ok": False, "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                }},
                status_code=400,
            )
        except Exception as exc:
            log.error("rpc_handler_error", method=body.method, error=str(exc))
            return JSONResponse(
                {"ok": False, "error": {
                    "code": ErrorCode.INTERNAL_ERROR,
                    "message": str(exc),
                }},
                status_code=500,
            )

    # ----------------------------------------------------------------
    # Health check
    # ----------------------------------------------------------------

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "engine_version": ENGINE_VERSION}

    # ----------------------------------------------------------------
    # File upload — saves files server-side, returns paths for importFiles
    # ----------------------------------------------------------------

    @app.post("/api/upload")
    async def upload_files(
        files: list[UploadFile] = File(...),
        project_id: str = Form(""),
    ):
        """Accept browser file uploads, save to a temp dir, return file paths."""
        import tempfile, shutil

        upload_dir = pathlib.Path(tempfile.gettempdir()) / "knowler_uploads"
        if project_id:
            # Save into the project's raw dir if we can resolve it
            try:
                project = await ctx.project_manager.get_project(project_id)
                upload_dir = pathlib.Path(project.root_path) / "raw"
            except Exception:
                pass
        upload_dir.mkdir(parents=True, exist_ok=True)

        saved_paths = []
        for f in files:
            dest = upload_dir / (f.filename or "upload")
            # Avoid overwriting — add a counter suffix if needed
            counter = 1
            original = dest
            while dest.exists():
                dest = original.with_stem(f"{original.stem}_{counter}")
                counter += 1
            content = await f.read()
            dest.write_bytes(content)
            saved_paths.append(str(dest))

        return {"paths": saved_paths}

    # ----------------------------------------------------------------
    # Serve React frontend (must be last — catches all remaining routes)
    # ----------------------------------------------------------------

    _static_dir = pathlib.Path(__file__).parent / "static"
    if _static_dir.exists():
        app.mount("/", StaticFiles(directory=str(_static_dir), html=True), name="static")
    else:
        @app.get("/")
        async def no_frontend():
            return JSONResponse({
                "message": "Frontend not bundled. Run `npm run build` in the app/ directory.",
                "api": "/api/rpc",
                "websocket": "/ws",
            })

    return app


# ------------------------------------------------------------------
# Server entry point
# ------------------------------------------------------------------

def _default_workspace() -> pathlib.Path:
    home = pathlib.Path.home()
    if platform.system() == "Darwin":
        return home / "Library" / "Application Support" / "Knowler"
    return home / ".knowler"


async def serve(port: int, dev: bool, workspace: pathlib.Path, open_browser: bool) -> None:
    log.info("knowler_starting", port=port, workspace=str(workspace))

    workspace.mkdir(parents=True, exist_ok=True)
    global_db = await open_global_db(workspace)

    transport = WebSocketTransport()
    router = Router(transport)

    llm_provider = await resolve_llm_provider(global_db)
    api_key, _storage = await resolve_api_key(global_db, llm_provider)
    llm = LLMProvider(provider=llm_provider, api_key=api_key)
    pm = ProjectManager(global_db)

    ctx = AppContext(
        global_db=global_db,
        project_manager=pm,
        job_runner=None,  # type: ignore
        llm=llm,
        router=router,
    )

    job_runner = JobRunner(ctx)
    ctx.job_runner = job_runner

    # Import and register handlers (reuse __main__ registration logic)
    from knowler_engine.__main__ import _register_handlers, _register_job_handlers
    _register_job_handlers(job_runner)
    _register_handlers(router, ctx)

    app = create_app(ctx)

    url = f"http://localhost:{port}"
    if open_browser:
        # Delay slightly so the server is ready before the browser opens
        async def _open():
            await asyncio.sleep(1.5)
            webbrowser.open(url)
        asyncio.create_task(_open())

    job_task = asyncio.create_task(job_runner.run_forever())

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="debug" if dev else "warning",
    )
    server = uvicorn.Server(config)

    log.info("knowler_ready", url=url)
    print(f"\n  Knowler running at {url}\n  Press Ctrl+C to stop.\n")

    try:
        await server.serve()
    finally:
        job_runner.stop()
        job_task.cancel()
        await pm.close_all()
        await global_db.close()
        log.info("knowler_stopped")
