"""Job type definitions."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


JOB_TYPES = {
    "ingest_bookmarks",
    "ingest_url",
    "ingest_file",
    "normalize_source",
    "compile_project",
    "rebuild_indexes",
    "query",
    "run_maintenance",
}

JOB_STATUS_QUEUED = "queued"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_SUCCEEDED = "succeeded"
JOB_STATUS_FAILED = "failed"
JOB_STATUS_CANCELLED = "cancelled"


@dataclass
class JobContext:
    """Passed to job handlers with everything they need."""
    job_id: str
    project_id: str
    job_type: str
    payload: dict[str, Any]
    app_ctx: Any  # AppContext — avoid circular import
    events: list[dict[str, Any]] = field(default_factory=list)
    cancelled: bool = False

    async def set_progress(
        self,
        progress: float,
        message: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Persist and emit a progress update for the current job."""
        progress = max(0.0, min(1.0, float(progress)))
        row_payload = dict(payload or {})
        row_payload["progress"] = progress

        project = self.app_ctx.project_manager._open_projects.get(self.project_id)
        if project:
            await project.db.execute(
                """
                UPDATE jobs
                SET status=?, progress=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                WHERE id=?
                """,
                (JOB_STATUS_RUNNING, progress, self.job_id),
            )
            await project.db.commit()

        if hasattr(self.app_ctx, "router"):
            await self.app_ctx.router.emit(
                "job.progress",
                {
                    "job_id": self.job_id,
                    "project_id": self.project_id,
                    "level": "info",
                    "event_type": "progress",
                    "message": message,
                    "progress": progress,
                    "status": JOB_STATUS_RUNNING,
                    "payload": row_payload,
                },
            )

    async def log_event(
        self,
        level: str,
        event_type: str,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Append a job event and emit it to the frontend."""
        seq_no = len(self.events) + 1
        from ulid import ULID
        event_id = f"je_{ULID()}"
        row_payload = payload or {}
        self.events.append(
            {
                "id": event_id,
                "job_id": self.job_id,
                "seq_no": seq_no,
                "level": level,
                "event_type": event_type,
                "message": message,
                "payload_json": row_payload,
            }
        )

        # Persist to DB
        project = self.app_ctx.project_manager._open_projects.get(self.project_id)
        if project:
            import json
            await project.db.execute(
                """
                INSERT INTO job_events(id, job_id, seq_no, level, event_type, message, payload_json)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    event_id,
                    self.job_id,
                    seq_no,
                    level,
                    event_type,
                    message,
                    json.dumps(row_payload),
                ),
            )
            await project.db.commit()

        # Emit to frontend via IPC router
        if hasattr(self.app_ctx, "router"):
            await self.app_ctx.router.emit(
                "job.progress",
                {
                    "job_id": self.job_id,
                    "project_id": self.project_id,
                    "level": level,
                    "event_type": event_type,
                    "message": message,
                    "payload": row_payload,
                },
            )
