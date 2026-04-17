"""
Async job runner.

Runs jobs as asyncio tasks. An asyncio.Queue serves as the in-process queue.
No external queue (Redis etc.) needed for v1.

Job lifecycle:
  queued -> running -> succeeded | failed | cancelled

Every job is recorded in the jobs table and emits IPC events.
"""
from __future__ import annotations

import asyncio
import json
import traceback
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from ulid import ULID

from knowler_engine.jobs.types import (
    JOB_STATUS_CANCELLED,
    JOB_STATUS_FAILED,
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCEEDED,
    JobContext,
)

log = structlog.get_logger(__name__)

JobHandler = Callable[[JobContext], Awaitable[dict[str, Any]]]


class JobRunner:
    """In-process async job queue and executor."""

    def __init__(self, app_ctx: Any) -> None:
        self._app_ctx = app_ctx
        self._handlers: dict[str, JobHandler] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._active: dict[str, asyncio.Task] = {}  # job_id -> Task
        self._cancel_flags: set[str] = set()
        self._running = False

    def register(self, job_type: str, handler: JobHandler) -> None:
        self._handlers[job_type] = handler

    async def enqueue(
        self,
        project_id: str,
        job_type: str,
        payload: dict[str, Any],
        requested_by: str = "user",
        priority: int = 100,
    ) -> str:
        """Insert a job into the DB and queue it for execution."""
        job_id = f"job_{ULID()}"
        project = await self._app_ctx.project_manager.get_project(project_id)
        await project.db.execute(
            """
            INSERT INTO jobs(id, project_id, job_type, status, priority, requested_by, payload_json)
            VALUES (?,?,?,?,?,?,?)
            """,
            (job_id, project_id, job_type, JOB_STATUS_QUEUED, priority, requested_by, json.dumps(payload)),
        )
        await project.db.commit()

        await self._queue.put(job_id)
        log.info("job_queued", job_id=job_id, job_type=job_type, project_id=project_id)

        # Emit event
        if hasattr(self._app_ctx, "router"):
            await self._app_ctx.router.emit(
                "job.created",
                {"job_id": job_id, "project_id": project_id, "job_type": job_type},
            )

        return job_id

    async def cancel(self, job_id: str) -> None:
        """Request cancellation of a running job."""
        self._cancel_flags.add(job_id)
        if task := self._active.get(job_id):
            task.cancel()

    async def run_forever(self) -> None:
        """Consume the job queue until stopped. Jobs run serially so that
        normalize jobs always complete before a queued compile job starts."""
        self._running = True
        while self._running:
            try:
                job_id = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            task = asyncio.create_task(self._execute(job_id))
            self._active[job_id] = task
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            finally:
                self._active.pop(job_id, None)

    def stop(self) -> None:
        self._running = False
        for task in self._active.values():
            task.cancel()

    async def _execute(self, job_id: str) -> None:
        """
        Resolve the job from DB, find handler, run it.
        Updates job status before and after.
        """
        # Find the job — we need to scan open projects (or search all)
        project_id, job_type, payload_json = await self._find_job(job_id)
        if project_id is None:
            log.error("job_not_found", job_id=job_id)
            return

        payload = json.loads(payload_json or "{}")
        project = await self._app_ctx.project_manager.get_project(project_id)

        # Mark running
        await project.db.execute(
            "UPDATE jobs SET status=?, started_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'), updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
            (JOB_STATUS_RUNNING, job_id),
        )
        await project.db.commit()

        if hasattr(self._app_ctx, "router"):
            await self._app_ctx.router.emit(
                "job.progress",
                {"job_id": job_id, "project_id": project_id, "status": JOB_STATUS_RUNNING, "progress": 0.0},
            )

        handler = self._handlers.get(job_type)
        if handler is None:
            await self._finish_job(project, job_id, JOB_STATUS_FAILED, {}, f"No handler for job_type={job_type}")
            return

        ctx = JobContext(
            job_id=job_id,
            project_id=project_id,
            job_type=job_type,
            payload=payload,
            app_ctx=self._app_ctx,
        )

        try:
            result = await handler(ctx)
            if job_id in self._cancel_flags:
                self._cancel_flags.discard(job_id)
                await self._finish_job(project, job_id, JOB_STATUS_CANCELLED, result or {})
            else:
                await self._finish_job(project, job_id, JOB_STATUS_SUCCEEDED, result or {})
        except asyncio.CancelledError:
            self._cancel_flags.discard(job_id)
            await self._finish_job(project, job_id, JOB_STATUS_CANCELLED, {})
        except Exception as exc:
            log.error("job_failed", job_id=job_id, error=str(exc), tb=traceback.format_exc())
            await self._finish_job(project, job_id, JOB_STATUS_FAILED, {}, str(exc))

    async def _finish_job(
        self,
        project: Any,
        job_id: str,
        status: str,
        result: dict,
        error: str | None = None,
    ) -> None:
        result_json = json.dumps(result)
        await project.db.execute(
            """
            UPDATE jobs
            SET status=?, result_json=?, finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),
                updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'), progress=1.0
            WHERE id=?
            """,
            (status, result_json, job_id),
        )
        await project.db.commit()

        if hasattr(self._app_ctx, "router"):
            event_name = "job.completed" if status == JOB_STATUS_SUCCEEDED else "job.failed"
            await self._app_ctx.router.emit(
                event_name,
                {"job_id": job_id, "project_id": project.id, "status": status, "error": error},
            )
        log.info("job_finished", job_id=job_id, status=status)

    async def _find_job(self, job_id: str) -> tuple[str | None, str | None, str | None]:
        """Search all open projects for the job."""
        pm = self._app_ctx.project_manager
        for proj in pm._open_projects.values():
            row = await proj.db.fetchone(
                "SELECT project_id, job_type, payload_json FROM jobs WHERE id=?",
                (job_id,),
            )
            if row:
                return row["project_id"], row["job_type"], row["payload_json"]
        return None, None, None
