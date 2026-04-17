"""
Shared helpers for project-level system pages.

These pages are deterministic and are rebuilt from the database:
  - index.md: content-oriented catalog of wiki pages
  - log.md: chronological record of ingests, queries, builds, and maintenance
"""
from __future__ import annotations

import pathlib
import re
from datetime import datetime
from typing import Any

import structlog

from knowler_engine.storage.atomic import write_atomic

log = structlog.get_logger(__name__)


def _strip_frontmatter(markdown: str) -> str:
    if not markdown.startswith("---"):
        return markdown
    parts = markdown.split("---", 2)
    if len(parts) < 3:
        return markdown
    return parts[2].lstrip()


def summarize_markdown(markdown: str, fallback: str = "", max_chars: int = 180) -> str:
    """Extract a one-line summary from markdown content."""
    body = _strip_frontmatter(markdown)
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        if line.startswith("- "):
            continue
        if line in {"---", "***"}:
            continue
        line = re.sub(r"\[\[([^\]]+)\]\]", r"\1", line)
        line = re.sub(r"`([^`]+)`", r"\1", line)
        line = re.sub(r"\s+", " ", line).strip()
        if not line:
            continue
        if len(line) > max_chars:
            return line[: max_chars - 1].rstrip() + "…"
        return line
    return fallback


def _format_log_date(value: str | None) -> str:
    if not value:
        return "unknown"
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value[:16]


def _safe_read_text(path_str: str | None) -> str:
    if not path_str:
        return ""
    path = pathlib.Path(path_str)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _page_entry(title: str, summary: str, updated_at: str | None = None) -> str:
    suffix = f" ({updated_at[:10]})" if updated_at else ""
    if summary:
        return f"- [[{title}]] — {summary}{suffix}"
    return f"- [[{title}]]{suffix}"


async def build_project_indexes(project_id: str, db: Any, vault: Any) -> None:
    """Build the project index pages from active wiki pages."""
    rows = await db.fetchall(
        """
        SELECT id, page_type, title, slug, file_path, updated_at
        FROM pages
        WHERE project_id=? AND status='active'
        ORDER BY page_type, title
        """,
        (project_id,),
    )

    grouped: dict[str, list[str]] = {
        "concept": [],
        "source_summary": [],
        "question": [],
        "index": [],
        "other": [],
    }

    for row in rows:
        markdown = _safe_read_text(row["file_path"])
        summary = summarize_markdown(markdown)
        entry = _page_entry(row["title"], summary, row["updated_at"])
        page_type = row["page_type"]
        if page_type in grouped:
            grouped[page_type].append(entry)
        else:
            grouped["other"].append(entry)

    index_content = f"""---
page_type: "index"
title: "Project Index"
project_id: "{project_id}"
---

# Project Index

This file is the content-oriented catalog for the project wiki.

## Concepts ({len(grouped["concept"])})

{chr(10).join(grouped["concept"]) or "_No concept pages compiled yet._"}

## Sources ({len(grouped["source_summary"])})

{chr(10).join(grouped["source_summary"]) or "_No source summaries compiled yet._"}

## Questions ({len(grouped["question"])})

{chr(10).join(grouped["question"]) or "_No filed question pages yet._"}

## Indexes ({len(grouped["index"])})

{chr(10).join(grouped["index"]) or "_No secondary indexes yet._"}

## Other Pages ({len(grouped["other"])})

{chr(10).join(grouped["other"]) or "_No additional page types yet._"}
"""

    wiki_index_path = vault.wiki / "indexes" / "index.md"
    wiki_index_path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(wiki_index_path, index_content, tmp_dir=vault.tmp_dir)

    root_index_path = vault.root / "index.md"
    write_atomic(root_index_path, index_content, tmp_dir=vault.tmp_dir)
    log.info("project_index_built", project_id=project_id, pages=len(rows))


async def rebuild_project_log(project_id: str, db: Any, vault: Any) -> None:
    """Rebuild the chronological project log from persisted events."""
    entries: list[tuple[str, str]] = []

    source_rows = await db.fetchall(
        """
        SELECT id, title, source_type, status, created_at
        FROM sources
        WHERE project_id=? AND deleted_at IS NULL
        ORDER BY created_at ASC
        """,
        (project_id,),
    )
    for row in source_rows:
        title = row["title"] or row["id"]
        heading = f"## [{_format_log_date(row['created_at'])}] ingest | {title}"
        body = "\n".join(
            [
                f"- Source ID: `{row['id']}`",
                f"- Type: {row['source_type']}",
                f"- Status: {row['status']}",
            ]
        )
        entries.append((row["created_at"], f"{heading}\n{body}"))

    query_rows = await db.fetchall(
        """
        SELECT qr.id, qr.prompt_text, qr.task_type, qr.status, qr.created_at,
               qr.result_artifact_id, a.title AS artifact_title
        FROM query_runs qr
        LEFT JOIN artifacts a ON a.id = qr.result_artifact_id
        WHERE qr.project_id=?
        ORDER BY qr.created_at ASC
        """,
        (project_id,),
    )
    for row in query_rows:
        prompt = (row["prompt_text"] or "").strip() or row["id"]
        heading = f"## [{_format_log_date(row['created_at'])}] query | {prompt[:80]}"
        body_lines = [
            f"- Query Run ID: `{row['id']}`",
            f"- Task type: {row['task_type']}",
            f"- Status: {row['status']}",
        ]
        if row["result_artifact_id"]:
            artifact_title = row["artifact_title"] or row["result_artifact_id"]
            body_lines.append(f"- Output: `{row['result_artifact_id']}` ({artifact_title})")
        entries.append((row["created_at"], f"{heading}\n" + "\n".join(body_lines)))

    job_rows = await db.fetchall(
        """
        SELECT id, job_type, status, created_at
        FROM jobs
        WHERE project_id=? AND job_type IN ('compile_project', 'run_maintenance')
        ORDER BY created_at ASC
        """,
        (project_id,),
    )
    for row in job_rows:
        kind = "build" if row["job_type"] == "compile_project" else "lint"
        heading = f"## [{_format_log_date(row['created_at'])}] {kind} | {row['job_type']}"
        body = "\n".join(
            [
                f"- Job ID: `{row['id']}`",
                f"- Status: {row['status']}",
            ]
        )
        entries.append((row["created_at"], f"{heading}\n{body}"))

    entries.sort(key=lambda item: item[0] or "")
    body = "\n\n".join(entry for _, entry in entries)
    log_content = f"""# Project Log

This file is the chronological record of ingests, queries, builds, and maintenance runs.

{body or "_No project activity recorded yet._"}
"""

    log_path = vault.root / "log.md"
    write_atomic(log_path, log_content, tmp_dir=vault.tmp_dir)
    log.info("project_log_rebuilt", project_id=project_id, entries=len(entries))
