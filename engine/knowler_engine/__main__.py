"""
Knowler Engine entry point.

Starts the engine process, registers all IPC method handlers,
and runs the async stdio NDJSON loop.

Usage:
  python -m knowler_engine [--dev] [--workspace <path>]

In dev mode: logs are more verbose.
In normal use: the engine runs as a local Python process installed via pip.

IPC protocol:
  stdin  -> requests (NDJSON)
  stdout -> responses and events (NDJSON)
  stderr -> logs (human-readable)
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
import platform
import argparse
import logging
import subprocess

import structlog

from knowler_engine.app_settings import (
    api_key_fallback_key,
    delete_setting,
    get_app_settings,
    resolve_api_key,
    resolve_llm_provider,
    set_setting,
    update_app_settings,
)
from knowler_engine.ipc.transport import Transport, PROTOCOL_VERSION, ENGINE_VERSION
from knowler_engine.ipc.router import Router
from knowler_engine.ipc.types import ErrorCode, make_error_response
from knowler_engine.storage.db import open_global_db
from knowler_engine.project.manager import ProjectManager
from knowler_engine.jobs.runner import JobRunner
from knowler_engine.llm.provider import LLMProvider


def _configure_logging(dev: bool) -> None:
    level = logging.DEBUG if dev else logging.INFO
    logging.basicConfig(stream=sys.stderr, level=level)
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer() if dev else structlog.processors.JSONRenderer(),
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def _default_workspace() -> pathlib.Path:
    home = pathlib.Path.home()
    if platform.system() == "Darwin":
        return home / "Library" / "Application Support" / "Knowler"
    return home / ".knowler"


class AppContext:
    """Holds all shared engine state."""

    def __init__(
        self,
        global_db: any,
        project_manager: ProjectManager,
        job_runner: JobRunner,
        llm: LLMProvider,
        router: Router,
    ) -> None:
        self.global_db = global_db
        self.project_manager = project_manager
        self.job_runner = job_runner
        self.llm = llm
        self.router = router


def _escape_applescript_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


async def _pick_directory(prompt: str) -> str | None:
    if platform.system() != "Darwin":
        return None

    script = f'POSIX path of (choose folder with prompt "{_escape_applescript_string(prompt)}")'
    try:
        proc = await asyncio.to_thread(
            subprocess.run,
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").lower()
        if "user canceled" in stderr:
            return None
        raise

    selected = (proc.stdout or "").strip()
    return selected or None


async def _reveal_in_finder(path: str) -> None:
    target = pathlib.Path(path).expanduser().resolve()
    if not target.exists():
        raise FileNotFoundError(f"{target} does not exist")

    if platform.system() == "Darwin":
        cmd = ["open", "-R", str(target)] if target.is_file() else ["open", str(target)]
        await asyncio.to_thread(subprocess.run, cmd, check=True)
        return

    raise RuntimeError("Reveal in Finder is only supported on macOS")


def _register_handlers(router: Router, ctx: AppContext) -> None:
    """Register all IPC method handlers."""
    router.set_context(ctx)

    # -----------------------------------------------------------------------
    # Engine lifecycle
    # -----------------------------------------------------------------------

    @router.method("engine.handshake")
    async def handshake(params: dict, ctx: AppContext) -> dict:
        protocol_version = params.get("protocol_version", 1)
        if protocol_version != PROTOCOL_VERSION:
            from knowler_engine.ipc.router import EngineError
            raise EngineError(
                ErrorCode.VALIDATION_ERROR,
                f"Protocol version mismatch: engine supports v{PROTOCOL_VERSION}, app sent v{protocol_version}",
            )
        return {
            "protocol_version": PROTOCOL_VERSION,
            "engine_version": ENGINE_VERSION,
            "capabilities": ["projects", "sources", "compile", "query", "maintenance", "search", "graph"],
        }

    @router.method("engine.shutdown")
    async def shutdown(params: dict, ctx: AppContext) -> dict:
        ctx.job_runner.stop()
        asyncio.get_event_loop().call_later(0.5, lambda: asyncio.get_event_loop().stop())
        return {"status": "shutting_down"}

    # -----------------------------------------------------------------------
    # Project methods
    # -----------------------------------------------------------------------

    @router.method("project.create")
    async def project_create(params: dict, ctx: AppContext) -> dict:
        from knowler_engine.ipc.router import EngineError
        name = params.get("name")
        if not name:
            raise EngineError(ErrorCode.VALIDATION_ERROR, "name is required")
        root_path = params.get("root_path")
        if not root_path:
            raise EngineError(ErrorCode.VALIDATION_ERROR, "root_path is required")

        app_settings = await get_app_settings(ctx.global_db)
        config = dict(params.get("config") or {})
        config.setdefault("default_model_tier", app_settings["default_model_tier"])
        config.setdefault("privacy_mode", app_settings["privacy_mode"])
        if app_settings.get("obsidian_vault_path"):
            config.setdefault("obsidian_vault_path", app_settings["obsidian_vault_path"])

        project = await ctx.project_manager.create_project(
            name=name,
            slug=params.get("slug"),
            root_path=root_path,
            config=config,
        )
        return {
            "project_id": project.id,
            "name": project.name,
            "slug": project.slug,
            "root_path": project.root_path,
            "status": project.status,
        }

    @router.method("project.open")
    async def project_open(params: dict, ctx: AppContext) -> dict:
        from knowler_engine.ipc.router import EngineError
        root_path = params.get("root_path")
        if not root_path:
            raise EngineError(ErrorCode.VALIDATION_ERROR, "root_path is required")
        project = await ctx.project_manager.open_project(root_path)
        return {
            "project_id": project.id,
            "name": project.name,
            "slug": project.slug,
            "root_path": project.root_path,
            "status": project.status,
            "config": project.config,
        }

    @router.method("project.importAndBuild")
    async def project_import_and_build(params: dict, ctx: AppContext) -> dict:
        """Scan the project folder for supported files, import + approve them all,
        then kick off a compile job. Returns job ids for progress tracking."""
        from knowler_engine.ipc.router import EngineError
        pid = params.get("project_id")
        if not pid:
            raise EngineError(ErrorCode.VALIDATION_ERROR, "project_id is required")

        project = await ctx.project_manager.get_project(pid)
        root = pathlib.Path(project.root_path)

        SUPPORTED = {".pdf", ".txt", ".md", ".markdown"}
        SKIP_DIRS = {".knowler", "normalized", "wiki", "outputs", "maintenance", "cache", "raw"}
        SKIP_FILES = {"config.yaml", "index.md", "log.md", "graph.json", "GRAPH_REPORT.md"}

        found: list[str] = []
        for path in root.rglob("*"):
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if path.is_file() and path.name in SKIP_FILES:
                continue
            if path.suffix.lower() in SUPPORTED and path.is_file():
                found.append(str(path))

        if not found:
            raise EngineError(ErrorCode.NOT_FOUND, f"No supported files found in {root}")

        # Ingest handler will auto-approve, then queue normalize + compile
        ingest_job_id = await ctx.job_runner.enqueue(
            pid, "ingest_files",
            {"file_paths": found, "auto_approve": True},
        )
        return {
            "files_found": len(found),
            "ingest_job_id": ingest_job_id,
        }

    @router.method("project.list")
    async def project_list(params: dict, ctx: AppContext) -> dict:
        projects = await ctx.project_manager.list_projects()
        return {"projects": projects}

    @router.method("project.get")
    async def project_get(params: dict, ctx: AppContext) -> dict:
        from knowler_engine.ipc.router import EngineError
        pid = params.get("project_id")
        if not pid:
            raise EngineError(ErrorCode.VALIDATION_ERROR, "project_id is required")
        project = await ctx.project_manager.get_project(pid)
        return {
            "project_id": project.id,
            "name": project.name,
            "slug": project.slug,
            "root_path": project.root_path,
            "status": project.status,
            "config": project.config,
        }

    @router.method("project.delete")
    async def project_delete(params: dict, ctx: AppContext) -> dict:
        from knowler_engine.ipc.router import EngineError

        pid = params.get("project_id")
        if not pid:
            raise EngineError(ErrorCode.VALIDATION_ERROR, "project_id is required")

        deleted = await ctx.project_manager.delete_project(pid)
        return {
            "project_id": deleted["id"],
            "name": deleted["name"],
            "slug": deleted["slug"],
            "root_path": deleted["root_path"],
            "status": "deleted",
            "files_deleted": False,
        }

    # -----------------------------------------------------------------------
    # Source methods
    # -----------------------------------------------------------------------

    @router.method("sources.importBookmarks")
    async def import_bookmarks(params: dict, ctx: AppContext) -> dict:
        from knowler_engine.ipc.router import EngineError
        pid = params.get("project_id")
        if not pid:
            raise EngineError(ErrorCode.VALIDATION_ERROR, "project_id is required")
        job_id = await ctx.job_runner.enqueue(
            project_id=pid,
            job_type="ingest_bookmarks",
            payload={
                "bookmark_file_path": params.get("bookmark_file_path"),
                "default_folder_rules": params.get("default_folder_rules", {}),
            },
        )
        return {"job_id": job_id}

    @router.method("sources.listBrowserBookmarkSources")
    async def list_browser_bookmark_sources(params: dict, ctx: AppContext) -> dict:
        from knowler_engine.ingest.adapters.bookmark import list_browser_bookmark_sources

        return {"sources": list_browser_bookmark_sources()}

    @router.method("sources.importBrowserBookmarks")
    async def import_browser_bookmarks(params: dict, ctx: AppContext) -> dict:
        from knowler_engine.ipc.router import EngineError

        pid = params.get("project_id")
        browser_id = params.get("browser_id")
        if not pid:
            raise EngineError(ErrorCode.VALIDATION_ERROR, "project_id is required")
        if not browser_id:
            raise EngineError(ErrorCode.VALIDATION_ERROR, "browser_id is required")

        job_id = await ctx.job_runner.enqueue(
            project_id=pid,
            job_type="ingest_bookmarks",
            payload={
                "browser_id": browser_id,
                "browser_profile": params.get("browser_profile"),
                "default_folder_rules": params.get("default_folder_rules", {}),
            },
        )
        return {"job_id": job_id}

    @router.method("sources.importUrls")
    async def import_urls(params: dict, ctx: AppContext) -> dict:
        from knowler_engine.ipc.router import EngineError
        pid = params.get("project_id")
        if not pid:
            raise EngineError(ErrorCode.VALIDATION_ERROR, "project_id is required")
        urls = params.get("urls", [])
        if not urls:
            raise EngineError(ErrorCode.VALIDATION_ERROR, "urls list is required")
        job_id = await ctx.job_runner.enqueue(
            project_id=pid,
            job_type="ingest_urls",
            payload={"urls": urls},
        )
        return {"job_id": job_id}

    @router.method("sources.importFiles")
    async def import_files(params: dict, ctx: AppContext) -> dict:
        from knowler_engine.ipc.router import EngineError
        pid = params.get("project_id")
        if not pid:
            raise EngineError(ErrorCode.VALIDATION_ERROR, "project_id is required")
        file_paths = params.get("file_paths", [])
        if not file_paths:
            raise EngineError(ErrorCode.VALIDATION_ERROR, "file_paths is required")
        job_id = await ctx.job_runner.enqueue(
            project_id=pid,
            job_type="ingest_files",
            payload={"file_paths": file_paths},
        )
        return {"job_id": job_id}

    @router.method("sources.list")
    async def sources_list(params: dict, ctx: AppContext) -> dict:
        from knowler_engine.ipc.router import EngineError
        pid = params.get("project_id")
        if not pid:
            raise EngineError(ErrorCode.VALIDATION_ERROR, "project_id is required")
        project = await ctx.project_manager.get_project(pid)

        filters = []
        filter_params = [pid]
        if status := params.get("status"):
            filters.append("status=?")
            filter_params.append(status)
        if ingest_state := params.get("ingest_state"):
            filters.append("ingest_state=?")
            filter_params.append(ingest_state)
        where = ("AND " + " AND ".join(filters)) if filters else ""

        rows = await project.db.fetchall(
            f"""
            SELECT id, source_type, title, canonical_url, domain, trust_level,
                   status, ingest_state, created_at, updated_at,
                   json_extract(metadata_json, '$.summary') as summary
            FROM sources WHERE project_id=? AND deleted_at IS NULL {where}
            ORDER BY created_at DESC LIMIT 200
            """,
            tuple(filter_params),
        )
        return {"sources": [dict(r) for r in rows]}

    @router.method("sources.promote")
    async def sources_promote(params: dict, ctx: AppContext) -> dict:
        from knowler_engine.ipc.router import EngineError
        pid = params.get("project_id")
        sid = params.get("source_id")
        if not pid or not sid:
            raise EngineError(ErrorCode.VALIDATION_ERROR, "project_id and source_id are required")
        project = await ctx.project_manager.get_project(pid)
        await project.db.execute(
            "UPDATE sources SET ingest_state='approved', updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=? AND project_id=?",
            (sid, pid),
        )
        await project.db.commit()
        # Queue normalization
        job_id = await ctx.job_runner.enqueue(pid, "normalize_source", {"source_id": sid})
        return {"status": "approved", "normalize_job_id": job_id}

    @router.method("sources.reject")
    async def sources_reject(params: dict, ctx: AppContext) -> dict:
        from knowler_engine.ipc.router import EngineError
        pid = params.get("project_id")
        sid = params.get("source_id")
        if not pid or not sid:
            raise EngineError(ErrorCode.VALIDATION_ERROR, "project_id and source_id are required")
        project = await ctx.project_manager.get_project(pid)
        await project.db.execute(
            "UPDATE sources SET ingest_state='rejected', updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=? AND project_id=?",
            (sid, pid),
        )
        await project.db.commit()
        return {"status": "rejected"}

    # -----------------------------------------------------------------------
    # Compile methods
    # -----------------------------------------------------------------------

    @router.method("compile.runProject")
    async def compile_run(params: dict, ctx: AppContext) -> dict:
        from knowler_engine.ipc.router import EngineError
        pid = params.get("project_id")
        if not pid:
            raise EngineError(ErrorCode.VALIDATION_ERROR, "project_id is required")
        job_id = await ctx.job_runner.enqueue(
            pid, "compile_project",
            {"scope": params.get("scope", "incremental"), "reason": params.get("reason", "user_manual")},
        )
        return {"job_id": job_id}

    # -----------------------------------------------------------------------
    # Query methods
    # -----------------------------------------------------------------------

    @router.method("query.run")
    async def query_run(params: dict, ctx: AppContext) -> dict:
        from knowler_engine.ipc.router import EngineError
        import json
        pid = params.get("project_id")
        prompt = params.get("prompt", "")
        if not pid:
            raise EngineError(ErrorCode.VALIDATION_ERROR, "project_id is required")
        if not prompt.strip():
            raise EngineError(ErrorCode.VALIDATION_ERROR, "prompt is required")

        project = await ctx.project_manager.get_project(pid)
        from ulid import ULID
        qr_id = f"qr_{ULID()}"
        task_type = params.get("task_type", "auto")
        model_tier = params.get("model_tier", "balanced")
        output_format = params.get("output_format", "markdown_report")

        await project.db.execute(
            """
            INSERT INTO query_runs(id, project_id, prompt_text, task_type, output_format, model_tier, status)
            VALUES (?,?,?,?,?,?,?)
            """,
            (qr_id, pid, prompt, task_type, output_format, model_tier, "queued"),
        )
        await project.db.commit()

        job_id = await ctx.job_runner.enqueue(
            pid, "query",
            {
                "query_run_id": qr_id,
                "prompt": prompt,
                "task_type": task_type,
                "output_format": output_format,
                "model_tier": model_tier,
                "file_back": params.get("file_back", True),
            },
        )
        return {"job_id": job_id, "query_run_id": qr_id}

    @router.method("query.list")
    async def query_list(params: dict, ctx: AppContext) -> dict:
        from knowler_engine.ipc.router import EngineError
        pid = params.get("project_id")
        if not pid:
            raise EngineError(ErrorCode.VALIDATION_ERROR, "project_id is required")
        project = await ctx.project_manager.get_project(pid)
        rows = await project.db.fetchall(
            "SELECT id, prompt_text, task_type, status, result_artifact_id, created_at FROM query_runs WHERE project_id=? ORDER BY created_at DESC LIMIT 50",
            (pid,),
        )
        return {"query_runs": [dict(r) for r in rows]}

    @router.method("query.get")
    async def query_get(params: dict, ctx: AppContext) -> dict:
        from knowler_engine.ipc.router import EngineError
        pid = params.get("project_id")
        query_run_id = params.get("query_run_id")
        if not pid or not query_run_id:
            raise EngineError(ErrorCode.VALIDATION_ERROR, "project_id and query_run_id are required")
        project = await ctx.project_manager.get_project(pid)
        row = await project.db.fetchone(
            """
            SELECT id, prompt_text, task_type, output_format, model_tier, status,
                   result_artifact_id, created_at, started_at, finished_at
            FROM query_runs
            WHERE project_id=? AND id=?
            """,
            (pid, query_run_id),
        )
        if not row:
            raise EngineError(ErrorCode.NOT_FOUND, f"Query run {query_run_id} not found")
        return dict(row)

    # -----------------------------------------------------------------------
    # Search methods
    # -----------------------------------------------------------------------

    @router.method("search.run")
    async def search_run(params: dict, ctx: AppContext) -> dict:
        from knowler_engine.ipc.router import EngineError
        from knowler_engine.search.fts import search_project
        pid = params.get("project_id")
        query = params.get("query", "")
        if not pid:
            raise EngineError(ErrorCode.VALIDATION_ERROR, "project_id is required")
        if not query.strip():
            raise EngineError(ErrorCode.VALIDATION_ERROR, "query is required")
        project = await ctx.project_manager.get_project(pid)
        filters = params.get("filters", {})
        results = await search_project(
            project.db, pid, query,
            limit=params.get("limit", 20),
            unit_types=filters.get("unit_types"),
        )
        return {"results": results, "query": query}

    # -----------------------------------------------------------------------
    # Graph / visualization methods
    # -----------------------------------------------------------------------

    @router.method("graph.knowledgeMap")
    async def graph_knowledge_map(params: dict, ctx: AppContext) -> dict:
        from knowler_engine.ipc.router import EngineError
        from knowler_engine.graph import build_knowledge_map

        pid = params.get("project_id")
        if not pid:
            raise EngineError(ErrorCode.VALIDATION_ERROR, "project_id is required")
        project = await ctx.project_manager.get_project(pid)
        return await build_knowledge_map(project.db, pid)

    # -----------------------------------------------------------------------
    # Maintenance methods
    # -----------------------------------------------------------------------

    @router.method("maintenance.run")
    async def maintenance_run(params: dict, ctx: AppContext) -> dict:
        from knowler_engine.ipc.router import EngineError
        pid = params.get("project_id")
        if not pid:
            raise EngineError(ErrorCode.VALIDATION_ERROR, "project_id is required")
        job_id = await ctx.job_runner.enqueue(
            pid, "run_maintenance",
            {"checks": params.get("checks", [])},
        )
        return {"job_id": job_id}

    @router.method("maintenance.listFindings")
    async def maintenance_findings(params: dict, ctx: AppContext) -> dict:
        from knowler_engine.ipc.router import EngineError
        pid = params.get("project_id")
        if not pid:
            raise EngineError(ErrorCode.VALIDATION_ERROR, "project_id is required")
        project = await ctx.project_manager.get_project(pid)
        rows = await project.db.fetchall(
            """
            SELECT id, finding_type, severity, subject_kind, subject_id,
                   title, description, suggestion_json, status, created_at
            FROM maintenance_findings WHERE project_id=? AND status='open'
            ORDER BY severity DESC, created_at DESC LIMIT 100
            """,
            (pid,),
        )
        return {"findings": [dict(r) for r in rows]}

    @router.method("maintenance.ignoreFinding")
    async def ignore_finding(params: dict, ctx: AppContext) -> dict:
        from knowler_engine.ipc.router import EngineError
        pid = params.get("project_id")
        fid = params.get("finding_id")
        if not pid or not fid:
            raise EngineError(ErrorCode.VALIDATION_ERROR, "project_id and finding_id required")
        project = await ctx.project_manager.get_project(pid)
        await project.db.execute(
            "UPDATE maintenance_findings SET status='ignored', updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=? AND project_id=?",
            (fid, pid),
        )
        await project.db.commit()
        return {"status": "ignored"}

    # -----------------------------------------------------------------------
    # Pages (compiled wiki content)
    # -----------------------------------------------------------------------

    @router.method("pages.list")
    async def pages_list(params: dict, ctx: AppContext) -> dict:
        from knowler_engine.ipc.router import EngineError
        pid = params.get("project_id")
        if not pid:
            raise EngineError(ErrorCode.VALIDATION_ERROR, "project_id is required")
        page_type = params.get("page_type")  # optional filter: 'source_summary' | 'concept'
        project = await ctx.project_manager.get_project(pid)
        if page_type:
            rows = await project.db.fetchall(
                "SELECT id, page_type, title, slug, file_path, status, created_at FROM pages WHERE project_id=? AND status='active' AND page_type=? ORDER BY created_at DESC LIMIT 200",
                (pid, page_type),
            )
        else:
            rows = await project.db.fetchall(
                "SELECT id, page_type, title, slug, file_path, status, created_at FROM pages WHERE project_id=? AND status='active' ORDER BY page_type, title LIMIT 200",
                (pid,),
            )
        return {"pages": [dict(r) for r in rows]}

    @router.method("pages.get")
    async def pages_get(params: dict, ctx: AppContext) -> dict:
        from knowler_engine.ipc.router import EngineError
        pid = params.get("project_id")
        page_id = params.get("page_id")
        if not pid or not page_id:
            raise EngineError(ErrorCode.VALIDATION_ERROR, "project_id and page_id required")
        project = await ctx.project_manager.get_project(pid)
        row = await project.db.fetchone(
            "SELECT * FROM pages WHERE id=? AND project_id=?", (page_id, pid)
        )
        if not row:
            raise EngineError(ErrorCode.NOT_FOUND, f"Page {page_id} not found")
        page = dict(row)
        fp = pathlib.Path(page["file_path"])
        page["content"] = fp.read_text(encoding="utf-8") if fp.exists() else None
        return page

    # -----------------------------------------------------------------------
    # Artifact methods
    # -----------------------------------------------------------------------

    @router.method("artifacts.list")
    async def artifacts_list(params: dict, ctx: AppContext) -> dict:
        from knowler_engine.ipc.router import EngineError
        pid = params.get("project_id")
        if not pid:
            raise EngineError(ErrorCode.VALIDATION_ERROR, "project_id is required")
        project = await ctx.project_manager.get_project(pid)
        rows = await project.db.fetchall(
            "SELECT id, artifact_type, title, file_path, status, created_at FROM artifacts WHERE project_id=? AND status='active' ORDER BY created_at DESC LIMIT 100",
            (pid,),
        )
        return {"artifacts": [dict(r) for r in rows]}

    @router.method("artifacts.get")
    async def artifact_get(params: dict, ctx: AppContext) -> dict:
        from knowler_engine.ipc.router import EngineError
        pid = params.get("project_id")
        aid = params.get("artifact_id")
        if not pid or not aid:
            raise EngineError(ErrorCode.VALIDATION_ERROR, "project_id and artifact_id required")
        project = await ctx.project_manager.get_project(pid)
        row = await project.db.fetchone(
            "SELECT * FROM artifacts WHERE id=? AND project_id=?", (aid, pid)
        )
        if not row:
            raise EngineError(ErrorCode.NOT_FOUND, f"Artifact {aid} not found")
        artifact = dict(row)
        # Include file content
        fp = pathlib.Path(artifact["file_path"])
        artifact["content"] = fp.read_text(encoding="utf-8") if fp.exists() else None
        return artifact

    # -----------------------------------------------------------------------
    # Jobs
    # -----------------------------------------------------------------------

    @router.method("jobs.list")
    async def jobs_list(params: dict, ctx: AppContext) -> dict:
        from knowler_engine.ipc.router import EngineError
        pid = params.get("project_id")
        if not pid:
            raise EngineError(ErrorCode.VALIDATION_ERROR, "project_id is required")
        project = await ctx.project_manager.get_project(pid)
        rows = await project.db.fetchall(
            "SELECT id, job_type, status, progress, started_at, finished_at, created_at FROM jobs WHERE project_id=? ORDER BY created_at DESC LIMIT 50",
            (pid,),
        )
        return {"jobs": [dict(r) for r in rows]}

    @router.method("jobs.cancel")
    async def jobs_cancel(params: dict, ctx: AppContext) -> dict:
        from knowler_engine.ipc.router import EngineError
        job_id = params.get("job_id")
        if not job_id:
            raise EngineError(ErrorCode.VALIDATION_ERROR, "job_id is required")
        await ctx.job_runner.cancel(job_id)
        return {"status": "cancel_requested"}

    # -----------------------------------------------------------------------
    # System helpers
    # -----------------------------------------------------------------------

    @router.method("os.pickDirectory")
    async def os_pick_directory(params: dict, ctx: AppContext) -> dict:
        prompt = params.get("prompt") or "Select a project folder"
        return {"path": await _pick_directory(prompt)}

    @router.method("os.revealInFinder")
    async def os_reveal_in_finder(params: dict, ctx: AppContext) -> dict:
        from knowler_engine.ipc.router import EngineError

        path = params.get("path")
        if not path:
            raise EngineError(ErrorCode.VALIDATION_ERROR, "path is required")
        try:
            await _reveal_in_finder(path)
        except FileNotFoundError as exc:
            raise EngineError(ErrorCode.NOT_FOUND, str(exc)) from exc
        return {"ok": True}

    # -----------------------------------------------------------------------
    # Settings (app preferences + API key management)
    # -----------------------------------------------------------------------

    @router.method("settings.getApp")
    async def settings_get_app(params: dict, ctx: AppContext) -> dict:
        return await get_app_settings(ctx.global_db)

    @router.method("settings.setApp")
    async def settings_set_app(params: dict, ctx: AppContext) -> dict:
        values = params.get("values")
        if not isinstance(values, dict):
            values = {}
        settings = await update_app_settings(ctx.global_db, values)

        if "llm_provider" in values:
            key, _storage = await resolve_api_key(ctx.global_db, settings["llm_provider"])
            ctx.llm.set_provider(settings["llm_provider"], key)

        return settings

    @router.method("settings.setApiKey")
    async def settings_set_api_key(params: dict, ctx: AppContext) -> dict:
        """Store an API key engine-side (Keychain or env fallback) and activate it."""
        from knowler_engine.ipc.router import EngineError
        provider = params.get("provider", "anthropic")
        key = params.get("key", "").strip()
        if not key:
            raise EngineError(ErrorCode.VALIDATION_ERROR, "key is required")

        storage = "app_db"
        try:
            import keyring

            keyring.set_password("Knowler", provider, key)
            storage = "keychain"
        except Exception:
            await set_setting(ctx.global_db, api_key_fallback_key(provider), key)
        else:
            await delete_setting(ctx.global_db, api_key_fallback_key(provider))

        await update_app_settings(ctx.global_db, {"llm_provider": provider})

        # Activate immediately in the running LLM provider
        ctx.llm.set_provider(provider, key)

        return {"ok": True, "provider": provider, "storage": storage}

    @router.method("settings.getApiKeyStatus")
    async def settings_get_api_key_status(params: dict, ctx: AppContext) -> dict:
        """Return whether an API key is configured for a given provider."""
        provider = params.get("provider", "anthropic")
        key, storage = await resolve_api_key(ctx.global_db, provider)
        if not key and ctx.llm.provider == provider and ctx.llm.is_configured():
            return {"configured": True, "provider": provider, "storage": "session"}
        return {"configured": bool(key), "provider": provider, "storage": storage}


def _register_job_handlers(job_runner: JobRunner) -> None:
    """Register all job type handlers."""
    from knowler_engine.ingest.pipeline import (
        handle_ingest_bookmarks,
        handle_ingest_files,
        handle_ingest_urls,
    )
    from knowler_engine.normalize.pipeline import handle_normalize_source
    from knowler_engine.compile.pipeline import handle_compile_project
    from knowler_engine.query.engine import handle_query
    from knowler_engine.maintenance.engine import handle_run_maintenance

    job_runner.register("ingest_bookmarks", handle_ingest_bookmarks)
    job_runner.register("ingest_urls", handle_ingest_urls)
    job_runner.register("ingest_files", handle_ingest_files)
    job_runner.register("normalize_source", handle_normalize_source)
    job_runner.register("compile_project", handle_compile_project)
    job_runner.register("query", handle_query)
    job_runner.register("run_maintenance", handle_run_maintenance)


async def run(dev: bool, workspace: pathlib.Path) -> None:
    """Main async engine loop."""
    log = structlog.get_logger("knowler_engine")
    log.info("engine_starting", version=ENGINE_VERSION, dev=dev, workspace=str(workspace))

    # Open global DB
    workspace.mkdir(parents=True, exist_ok=True)
    global_db = await open_global_db(workspace)

    # Setup transport and router
    transport = Transport()
    router = Router(transport)

    # Setup LLM provider
    provider = await resolve_llm_provider(global_db)
    api_key, _storage = await resolve_api_key(global_db, provider)
    llm = LLMProvider(provider=provider, api_key=api_key)

    # Setup project manager and job runner
    pm = ProjectManager(global_db)

    # Create app context (router set after handler registration)
    ctx = AppContext(
        global_db=global_db,
        project_manager=pm,
        job_runner=None,  # type: ignore
        llm=llm,
        router=router,
    )

    job_runner = JobRunner(ctx)
    ctx.job_runner = job_runner
    _register_job_handlers(job_runner)
    _register_handlers(router, ctx)

    # Emit ready event
    await transport.send_raw({
        "type": "event",
        "event": "engine.ready",
        "payload": {
            "protocol_version": PROTOCOL_VERSION,
            "engine_version": ENGINE_VERSION,
        },
    })

    # Start job runner in background
    job_task = asyncio.create_task(job_runner.run_forever())

    # Main message loop
    try:
        async for request in transport.read_messages():
            asyncio.create_task(router.dispatch(request))
    except Exception as exc:
        log.error("engine_loop_error", error=str(exc))
    finally:
        job_runner.stop()
        job_task.cancel()
        await pm.close_all()
        await global_db.close()
        log.info("engine_stopped")


def main() -> None:
    parser = argparse.ArgumentParser(description="Knowler")
    subparsers = parser.add_subparsers(dest="command")

    # --- serve (default) ---
    serve_p = subparsers.add_parser("serve", help="Start local web server (default)")
    serve_p.add_argument("--port", type=int, default=7842, help="Port to listen on")
    serve_p.add_argument("--dev", action="store_true", help="Enable dev mode")
    serve_p.add_argument("--workspace", type=str, help="Workspace directory path")
    serve_p.add_argument("--no-browser", action="store_true", help="Don't open browser")

    # --- engine (stdio mode for scripting/integration) ---
    engine_p = subparsers.add_parser("engine", help="Run stdio NDJSON engine")
    engine_p.add_argument("--dev", action="store_true")
    engine_p.add_argument("--workspace", type=str)

    args = parser.parse_args()

    # Default command is serve
    if args.command is None or args.command == "serve":
        port = getattr(args, "port", 7842)
        dev = getattr(args, "dev", False)
        no_browser = getattr(args, "no_browser", False)
        workspace_str = getattr(args, "workspace", None)
        _configure_logging(dev)
        workspace = pathlib.Path(workspace_str) if workspace_str else _default_workspace()
        from knowler_engine.server import serve as _serve
        asyncio.run(_serve(port=port, dev=dev, workspace=workspace, open_browser=not no_browser))
    else:
        _configure_logging(args.dev)
        workspace = pathlib.Path(args.workspace) if args.workspace else _default_workspace()
        asyncio.run(run(dev=args.dev, workspace=workspace))


if __name__ == "__main__":
    main()
