"""
Maintenance engine.

Orchestrates all maintenance checks and persists findings.
Architecture: §19
"""
from __future__ import annotations

import json
from typing import Any

import structlog

from knowler_engine.jobs.types import JobContext
from knowler_engine.maintenance.checks import (
    check_duplicate_entities,
    check_missing_comparisons,
    check_orphan_pages,
    check_stale_pages,
    check_weak_claims,
)
from knowler_engine.system_pages import rebuild_project_log

log = structlog.get_logger(__name__)


async def handle_run_maintenance(ctx: JobContext) -> dict[str, Any]:
    """
    Job handler: run all maintenance checks.

    Payload:
      checks: list[str]  — optional subset of checks to run
    """
    requested_checks = ctx.payload.get("checks", [
        "orphan_pages",
        "duplicate_entities",
        "weak_claims",
        "stale_pages",
        "missing_comparisons",
    ])

    project = await ctx.app_ctx.project_manager.get_project(ctx.project_id)
    db = project.db
    results: dict[str, int] = {}

    await ctx.log_event("info", "step_started", "Running maintenance checks")

    if "orphan_pages" in requested_checks:
        await ctx.log_event("info", "log", "Checking for orphan pages...")
        n = await check_orphan_pages(db, ctx.project_id)
        results["orphan_pages"] = n
        await ctx.log_event("info", "log", f"Found {n} orphan pages")

    if "duplicate_entities" in requested_checks:
        await ctx.log_event("info", "log", "Checking for duplicate entities...")
        n = await check_duplicate_entities(db, ctx.project_id)
        results["duplicate_entities"] = n
        await ctx.log_event("info", "log", f"Found {n} potential duplicates")

    if "weak_claims" in requested_checks:
        await ctx.log_event("info", "log", "Checking for weak claims...")
        n = await check_weak_claims(db, ctx.project_id)
        results["weak_claims"] = n
        await ctx.log_event("info", "log", f"Found {n} weak claims")

    if "stale_pages" in requested_checks:
        await ctx.log_event("info", "log", "Checking for stale pages...")
        n = await check_stale_pages(db, ctx.project_id)
        results["stale_pages"] = n
        await ctx.log_event("info", "log", f"Found {n} stale pages")

    if "missing_comparisons" in requested_checks:
        await ctx.log_event("info", "log", "Checking for missing comparison pages...")
        n = await check_missing_comparisons(db, ctx.project_id)
        results["missing_comparisons"] = n
        await ctx.log_event("info", "log", f"Found {n} missing comparisons")

    total = sum(results.values())
    await ctx.log_event(
        "info", "step_finished",
        f"Maintenance complete: {total} findings across {len(results)} checks",
    )

    # Emit event
    if hasattr(ctx.app_ctx, "router"):
        await ctx.app_ctx.router.emit(
            "maintenance.finding_created",
            {"project_id": ctx.project_id, "total_findings": total, "by_type": results},
        )

    await rebuild_project_log(ctx.project_id, db, project.vault)
    return results
