"""
Ingest pipeline.

Orchestrates source ingestion job handlers.
Each handler is an async function that takes a JobContext.
"""
from __future__ import annotations

import json
from typing import Any

import structlog

from knowler_engine.ingest.adapters.bookmark import (
    build_source_rows,
    load_browser_bookmarks,
    parse_bookmark_html,
)
from knowler_engine.ingest.adapters.pdf import ingest_pdf
from knowler_engine.ingest.adapters.url import ingest_url
from knowler_engine.jobs.types import JobContext
from knowler_engine.system_pages import rebuild_project_log

log = structlog.get_logger(__name__)


async def _insert_source(db: Any, row: dict[str, Any]) -> None:
    """Insert a source row, ignoring duplicates by canonical_url checksum."""
    # Check for URL duplicate
    if row.get("canonical_url"):
        existing = await db.fetchone(
            "SELECT id FROM sources WHERE project_id=? AND canonical_url=? AND deleted_at IS NULL",
            (row["project_id"], row["canonical_url"]),
        )
        if existing:
            log.info("source_url_duplicate_skipped", url=row.get("canonical_url", "")[:80])
            return

    # Check for checksum duplicate
    if row.get("checksum_sha256"):
        existing = await db.fetchone(
            "SELECT id FROM sources WHERE project_id=? AND checksum_sha256=? AND deleted_at IS NULL",
            (row["project_id"], row["checksum_sha256"]),
        )
        if existing:
            log.info("source_checksum_duplicate_skipped", checksum=row.get("checksum_sha256", "")[:12])
            return

    await db.execute(
        """
        INSERT INTO sources(
          id, project_id, source_type, origin_type, title, canonical_url,
          display_url, domain, raw_path, mime_type, checksum_sha256, byte_size,
          trust_level, status, ingest_state, language_code, source_date, metadata_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            row["id"], row["project_id"], row["source_type"], row["origin_type"],
            row.get("title"), row.get("canonical_url"), row.get("display_url"),
            row.get("domain"), row["raw_path"], row.get("mime_type"),
            row.get("checksum_sha256"), row.get("byte_size"),
            row.get("trust_level", "unknown"), row.get("status", "pending"),
            row.get("ingest_state", "new"), row.get("language_code"),
            row.get("source_date"), row.get("metadata_json", "{}"),
        ),
    )
    await db.commit()


async def handle_ingest_bookmarks(ctx: JobContext) -> dict[str, Any]:
    """
    Job handler for bookmark import.

    Payload:
      bookmark_file_path: str
      browser_id: str
      browser_profile: str
      default_folder_rules: dict[str, str]  e.g. {"ML Papers": "trusted"}
    """
    payload = ctx.payload
    file_path = payload.get("bookmark_file_path")
    browser_id = payload.get("browser_id")
    browser_profile = payload.get("browser_profile")
    if not file_path and not browser_id:
        raise ValueError("bookmark_file_path or browser_id is required")

    project = await ctx.app_ctx.project_manager.get_project(ctx.project_id)
    config = project.config
    trusted_domains = config.get("trusted_domains", [])
    folder_rules = payload.get("default_folder_rules", {})
    trusted_folders = [k for k, v in folder_rules.items() if v == "trusted"]

    browser_source = "bookmark_html"
    browser_source_label = "bookmark file"
    await ctx.log_event("info", "step_started", "Parsing bookmarks")

    if file_path:
        bookmarks = parse_bookmark_html(file_path)
    else:
        source, bookmarks = load_browser_bookmarks(
            browser_id=str(browser_id),
            profile=str(browser_profile) if browser_profile else None,
        )
        browser_source = str(source["browser_id"])
        browser_profile = source.get("profile")
        browser_source_label = source.get("detail") or source.get("browser_name") or browser_source

    await ctx.log_event("info", "log", f"Source: {browser_source_label}")
    await ctx.log_event("info", "log", f"Found {len(bookmarks)} bookmarks")

    rows = build_source_rows(
        bookmarks,
        project_id=ctx.project_id,
        trusted_domains=trusted_domains,
        trusted_folders=trusted_folders,
        raw_dir=project.vault.raw,
        browser_source=browser_source,
        browser_profile=str(browser_profile) if browser_profile else None,
    )

    inserted = 0
    for i, row in enumerate(rows):
        if ctx.cancelled:
            break
        await _insert_source(project.db, row)
        inserted += 1

    await ctx.log_event(
        "info", "step_finished",
        f"Imported {inserted} bookmarks ({len(bookmarks) - inserted} deduplicated)",
    )
    await rebuild_project_log(ctx.project_id, project.db, project.vault)

    return {
        "total_parsed": len(bookmarks),
        "inserted": inserted,
        "skipped": len(bookmarks) - inserted,
    }


async def handle_ingest_urls(ctx: JobContext) -> dict[str, Any]:
    """
    Job handler for URL ingestion.

    Payload:
      urls: list[str]
    """
    urls = ctx.payload.get("urls", [])
    if not urls:
        raise ValueError("urls list is required")

    project = await ctx.app_ctx.project_manager.get_project(ctx.project_id)
    config = project.config
    trusted_domains = config.get("trusted_domains", [])
    raw_dir = project.vault.raw / "articles"

    results = {"succeeded": [], "failed": []}
    for url in urls:
        if ctx.cancelled:
            break
        try:
            await ctx.log_event("info", "step_started", f"Fetching {url[:80]}")
            row = await ingest_url(url, ctx.project_id, raw_dir, trusted_domains)
            await _insert_source(project.db, row)
            results["succeeded"].append({"url": url, "source_id": row["id"]})
            await ctx.log_event("info", "log", f"Ingested: {row.get('title', url)[:80]}")
        except Exception as exc:
            log.warning("url_ingest_failed", url=url[:100], error=str(exc))
            results["failed"].append({"url": url, "error": str(exc)})
            await ctx.log_event("warn", "log", f"Failed to fetch {url[:80]}: {exc}")

    await rebuild_project_log(ctx.project_id, project.db, project.vault)
    return results


async def handle_ingest_files(ctx: JobContext) -> dict[str, Any]:
    """
    Job handler for local file ingestion (PDFs, text, markdown).

    Payload:
      file_paths: list[str]
      auto_approve: bool — skip needs_review and queue normalize+compile automatically
    """
    import pathlib
    import shutil
    from ulid import ULID

    file_paths = ctx.payload.get("file_paths", [])
    if not file_paths:
        raise ValueError("file_paths list is required")

    auto_approve: bool = ctx.payload.get("auto_approve", False)
    project = await ctx.app_ctx.project_manager.get_project(ctx.project_id)
    raw_papers_dir = project.vault.raw / "papers"
    raw_papers_dir.mkdir(parents=True, exist_ok=True)

    results: dict[str, Any] = {"succeeded": [], "failed": []}
    normalize_ids: list[str] = []

    for fp in file_paths:
        if ctx.cancelled:
            break
        try:
            src_path = pathlib.Path(fp)
            await ctx.log_event("info", "step_started", f"Processing {src_path.name}")

            if src_path.suffix.lower() == ".pdf":
                row = await ingest_pdf(fp, ctx.project_id, raw_papers_dir)
            else:
                dest = raw_papers_dir / src_path.name
                if not dest.exists():
                    shutil.copy2(src_path, dest)
                row = {
                    "id": f"src_{ULID()}",
                    "project_id": ctx.project_id,
                    "source_type": "markdown_note",
                    "origin_type": "file",
                    "title": src_path.stem,
                    "raw_path": str(dest),
                    "mime_type": "text/plain",
                    "trust_level": "high",
                    "status": "pending",
                    "ingest_state": "approved",
                }

            if auto_approve:
                row["ingest_state"] = "approved"
                row["trust_level"] = "high"

            await _insert_source(project.db, row)
            results["succeeded"].append({"file": fp, "source_id": row["id"]})
            await ctx.log_event("info", "log", f"Ingested: {row.get('title', src_path.name)[:80]}")

            if row.get("ingest_state") == "approved":
                normalize_ids.append(row["id"])

        except Exception as exc:
            log.warning("file_ingest_failed", path=fp, error=str(exc))
            results["failed"].append({"file": fp, "error": str(exc)})
            await ctx.log_event("warn", "log", f"Failed: {pathlib.Path(fp).name}: {exc}")

    # Queue normalize for each approved source, then compile
    for source_id in normalize_ids:
        await ctx.app_ctx.job_runner.enqueue(
            ctx.project_id, "normalize_source", {"source_id": source_id}
        )
    if normalize_ids:
        await ctx.app_ctx.job_runner.enqueue(
            ctx.project_id, "compile_project",
            {"scope": "incremental", "reason": "post_ingest"},
        )
        await ctx.log_event(
            "info", "step_finished",
            f"Queued normalize ({len(normalize_ids)} sources) + compile",
        )

    results["normalize_queued"] = len(normalize_ids)
    await rebuild_project_log(ctx.project_id, project.db, project.vault)
    return results
