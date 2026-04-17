"""Tests for the async job runner."""
import asyncio
import json
import pytest
import pytest_asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from knowler_engine.storage.db import open_project_db
from knowler_engine.jobs.runner import JobRunner
from knowler_engine.jobs.types import JobContext, JOB_STATUS_SUCCEEDED, JOB_STATUS_FAILED


def _make_app_ctx(project_db, project_id: str, project_root: Path):
    """Build a minimal AppContext mock that JobRunner and JobContext expect."""
    project = MagicMock()
    project.id = project_id
    project.db = project_db

    project_manager = MagicMock()
    project_manager.get_project = AsyncMock(return_value=project)
    project_manager._open_projects = {project_id: project}

    router = MagicMock()
    router.emit = AsyncMock()

    ctx = MagicMock()
    ctx.project_manager = project_manager
    ctx.router = router
    return ctx


@pytest_asyncio.fixture
async def proj_root(tmp_path: Path):
    return tmp_path / "test_project"


@pytest_asyncio.fixture
async def db(proj_root: Path):
    # open_project_db takes project root, not db path
    _db = await open_project_db(proj_root)
    # Insert project row required by FK
    await _db.execute(
        "INSERT INTO projects (id, name, slug, root_path) VALUES (?, ?, ?, ?)",
        ("proj-1", "Test", "test", str(proj_root))
    )
    await _db.commit()
    yield _db
    await _db.close()


@pytest.mark.asyncio
async def test_enqueue_creates_job_record(db, proj_root: Path):
    app_ctx = _make_app_ctx(db, "proj-1", proj_root)
    runner = JobRunner(app_ctx=app_ctx)

    job_id = await runner.enqueue("proj-1", "compile_project", {"scope": "incremental"})
    assert job_id is not None
    assert job_id.startswith("job_")

    row = await db.fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,))
    assert row is not None
    assert row["status"] == "queued"
    assert row["job_type"] == "compile_project"


@pytest.mark.asyncio
async def test_enqueue_emits_job_created_event(db, proj_root: Path):
    app_ctx = _make_app_ctx(db, "proj-1", proj_root)
    runner = JobRunner(app_ctx=app_ctx)

    await runner.enqueue("proj-1", "compile_project", {})
    app_ctx.router.emit.assert_called_once()
    event_name = app_ctx.router.emit.call_args[0][0]
    assert event_name == "job.created"


@pytest.mark.asyncio
async def test_job_executes_registered_handler(db, proj_root: Path):
    app_ctx = _make_app_ctx(db, "proj-1", proj_root)
    runner = JobRunner(app_ctx=app_ctx)
    executed = []

    async def my_handler(ctx: JobContext):
        executed.append(ctx.job_id)
        return {}

    runner.register("my_job_type", my_handler)
    job_id = await runner.enqueue("proj-1", "my_job_type", {})

    task = asyncio.create_task(runner.run_forever())
    await asyncio.sleep(0.3)
    runner.stop()
    try:
        await asyncio.wait_for(task, timeout=1.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass

    assert job_id in executed


@pytest.mark.asyncio
async def test_job_marked_succeeded_after_completion(db, proj_root: Path):
    app_ctx = _make_app_ctx(db, "proj-1", proj_root)
    runner = JobRunner(app_ctx=app_ctx)

    async def fast_handler(ctx: JobContext):
        return {}

    runner.register("fast_job", fast_handler)
    job_id = await runner.enqueue("proj-1", "fast_job", {})

    task = asyncio.create_task(runner.run_forever())
    await asyncio.sleep(0.3)
    runner.stop()
    try:
        await asyncio.wait_for(task, timeout=1.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass

    row = await db.fetchone("SELECT status FROM jobs WHERE id = ?", (job_id,))
    assert row["status"] == JOB_STATUS_SUCCEEDED


@pytest.mark.asyncio
async def test_job_marked_failed_on_handler_exception(db, proj_root: Path):
    app_ctx = _make_app_ctx(db, "proj-1", proj_root)
    runner = JobRunner(app_ctx=app_ctx)

    async def crashing_handler(ctx: JobContext):
        raise RuntimeError("handler failed")

    runner.register("crash_job", crashing_handler)
    job_id = await runner.enqueue("proj-1", "crash_job", {})

    task = asyncio.create_task(runner.run_forever())
    await asyncio.sleep(0.3)
    runner.stop()
    try:
        await asyncio.wait_for(task, timeout=1.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass

    row = await db.fetchone("SELECT status FROM jobs WHERE id = ?", (job_id,))
    assert row["status"] == JOB_STATUS_FAILED


@pytest.mark.asyncio
async def test_job_context_log_event_persists(db, proj_root: Path):
    app_ctx = _make_app_ctx(db, "proj-1", proj_root)
    runner = JobRunner(app_ctx=app_ctx)
    job_id_holder = []

    async def logging_handler(ctx: JobContext):
        job_id_holder.append(ctx.job_id)
        await ctx.log_event("info", "step_complete", "step 1 done")
        return {}

    runner.register("logging_job", logging_handler)
    job_id = await runner.enqueue("proj-1", "logging_job", {})

    task = asyncio.create_task(runner.run_forever())
    await asyncio.sleep(0.3)
    runner.stop()
    try:
        await asyncio.wait_for(task, timeout=1.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass

    events = await db.fetchall("SELECT * FROM job_events WHERE job_id = ?", (job_id,))
    assert len(events) >= 1
    assert any(e["message"] == "step 1 done" for e in events)


@pytest.mark.asyncio
async def test_job_context_set_progress_updates_job_row(db, proj_root: Path):
    app_ctx = _make_app_ctx(db, "proj-1", proj_root)
    runner = JobRunner(app_ctx=app_ctx)

    async def progress_handler(ctx: JobContext):
        await ctx.set_progress(0.55, "Halfway there")
        return {}

    runner.register("progress_job", progress_handler)
    job_id = await runner.enqueue("proj-1", "progress_job", {})

    task = asyncio.create_task(runner.run_forever())
    await asyncio.sleep(0.3)
    runner.stop()
    try:
        await asyncio.wait_for(task, timeout=1.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass

    row = await db.fetchone("SELECT status, progress FROM jobs WHERE id = ?", (job_id,))
    assert row["status"] == JOB_STATUS_SUCCEEDED
    assert row["progress"] == pytest.approx(1.0)
    assert app_ctx.router.emit.await_count >= 3
    emitted = [call.args[0] for call in app_ctx.router.emit.await_args_list]
    assert "job.progress" in emitted
    assert "job.completed" in emitted


@pytest.mark.asyncio
async def test_job_context_carries_payload(db, proj_root: Path):
    app_ctx = _make_app_ctx(db, "proj-1", proj_root)
    runner = JobRunner(app_ctx=app_ctx)
    captured = []

    async def inspecting_handler(ctx: JobContext):
        captured.append(ctx.payload)
        return {}

    runner.register("inspect_job", inspecting_handler)
    await runner.enqueue("proj-1", "inspect_job", {"key": "val"})

    task = asyncio.create_task(runner.run_forever())
    await asyncio.sleep(0.3)
    runner.stop()
    try:
        await asyncio.wait_for(task, timeout=1.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass

    assert len(captured) == 1
    assert captured[0]["key"] == "val"
