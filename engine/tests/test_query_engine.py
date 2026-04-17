"""Regression tests for query fallback behavior and wiki filing."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from knowler_engine.jobs.types import JobContext
from knowler_engine.llm.provider import LLMProvider
from knowler_engine.project.manager import ProjectManager
from knowler_engine.query.engine import handle_query
from knowler_engine.storage.db import open_global_db


@pytest_asyncio.fixture
async def query_project(tmp_path: Path):
    global_db = await open_global_db(tmp_path / "support")
    manager = ProjectManager(global_db=global_db)
    project = await manager.create_project(
        name="Query Tests",
        slug=None,
        root_path=str(tmp_path / "query-tests"),
    )
    app_ctx = SimpleNamespace(
        project_manager=manager,
        llm=LLMProvider(provider="openai", api_key=None),
        router=SimpleNamespace(emit=AsyncMock()),
    )
    try:
        yield project, app_ctx
    finally:
        await manager.close_all()
        await global_db.close()


async def _insert_query_run(project, query_run_id: str, prompt: str) -> None:
    await project.db.execute(
        """
        INSERT INTO query_runs(id, project_id, prompt_text, task_type, output_format, model_tier, status)
        VALUES (?,?,?,?,?,?,?)
        """,
        (query_run_id, project.id, prompt, "answer", "markdown_report", "balanced", "queued"),
    )
    await project.db.commit()


async def _insert_job(project, job_id: str, payload: dict[str, object]) -> None:
    await project.db.execute(
        """
        INSERT INTO jobs(id, project_id, job_type, status, priority, requested_by, payload_json)
        VALUES (?,?,?,?,?,?,?)
        """,
        (job_id, project.id, "query", "running", 100, "test", json.dumps(payload)),
    )
    await project.db.commit()


@pytest.mark.asyncio
async def test_query_without_evidence_writes_visible_fallback_artifact(query_project):
    project, app_ctx = query_project
    query_run_id = "qr_no_evidence"
    prompt = "What does this project say about retrieval augmented generation?"
    await _insert_query_run(project, query_run_id, prompt)
    await _insert_job(
        project,
        "job_no_evidence",
        {
            "query_run_id": query_run_id,
            "prompt": prompt,
            "task_type": "answer",
            "output_format": "markdown_report",
            "model_tier": "balanced",
            "file_back": True,
        },
    )

    ctx = JobContext(
        job_id="job_no_evidence",
        project_id=project.id,
        job_type="query",
        payload={
            "query_run_id": query_run_id,
            "prompt": prompt,
            "task_type": "answer",
            "output_format": "markdown_report",
            "model_tier": "balanced",
            "file_back": True,
        },
        app_ctx=app_ctx,
    )

    result = await handle_query(ctx)

    assert result["artifact_id"].startswith("art_")
    assert result["question_page_id"] is None

    run = await project.db.fetchone(
        "SELECT status, result_artifact_id FROM query_runs WHERE id=?",
        (query_run_id,),
    )
    assert run["status"] == "succeeded"
    assert run["result_artifact_id"] == result["artifact_id"]

    artifact = await project.db.fetchone(
        "SELECT file_path FROM artifacts WHERE id=?",
        (result["artifact_id"],),
    )
    assert artifact is not None
    content = Path(artifact["file_path"]).read_text(encoding="utf-8")
    assert "does not yet have enough compiled evidence" in content
    assert "## Next Steps" in content

    log_text = (Path(project.root_path) / "log.md").read_text(encoding="utf-8")
    assert "query | What does this project say about retrieval augmented generation?" in log_text


@pytest.mark.asyncio
async def test_query_with_page_evidence_files_question_page_and_updates_indexes(query_project):
    project, app_ctx = query_project

    source_path = project.vault.raw / "papers" / "neural-networks.md"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text("Neural networks are layered function approximators.", encoding="utf-8")

    await project.db.execute(
        """
        INSERT INTO sources(
          id, project_id, source_type, origin_type, title, raw_path, trust_level, status,
          ingest_state, metadata_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "src_neural",
            project.id,
            "markdown_note",
            "file_upload",
            "Neural Networks Primer",
            str(source_path),
            "high",
            "compiled",
            "approved",
            json.dumps({"summary": "Neural networks use stacked layers to learn useful representations."}),
        ),
    )

    page_path = project.vault.wiki_path("concept", "neural-networks")
    page_path.parent.mkdir(parents=True, exist_ok=True)
    page_path.write_text(
        """---
id: "pg_neural"
project_id: "proj"
page_type: "concept"
title: "Neural Networks"
---

## Overview

Neural networks are layered models that learn representations from data.
""",
        encoding="utf-8",
    )

    await project.db.execute(
        """
        INSERT INTO pages(id, project_id, page_type, title, slug, file_path, frontmatter_json, body_sha256, status)
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            "pg_neural",
            project.id,
            "concept",
            "Neural Networks",
            "neural-networks",
            str(page_path),
            "{}",
            "sha",
            "active",
        ),
    )
    await project.db.execute(
        """
        INSERT INTO relations(id, project_id, from_kind, from_id, relation_type, to_kind, to_id)
        VALUES (?,?,?,?,?,?,?)
        """,
        ("rel_neural_source", project.id, "page", "pg_neural", "derived_from", "source", "src_neural"),
    )
    await project.db.commit()

    query_run_id = "qr_neural"
    prompt = "What are neural networks?"
    await _insert_query_run(project, query_run_id, prompt)
    await _insert_job(
        project,
        "job_neural",
        {
            "query_run_id": query_run_id,
            "prompt": prompt,
            "task_type": "answer",
            "output_format": "markdown_note",
            "model_tier": "balanced",
            "file_back": True,
        },
    )

    ctx = JobContext(
        job_id="job_neural",
        project_id=project.id,
        job_type="query",
        payload={
            "query_run_id": query_run_id,
            "prompt": prompt,
            "task_type": "answer",
            "output_format": "markdown_note",
            "model_tier": "balanced",
            "file_back": True,
        },
        app_ctx=app_ctx,
    )

    result = await handle_query(ctx)

    assert result["artifact_id"].startswith("art_")
    assert result["question_page_id"]

    question_page = await project.db.fetchone(
        "SELECT title, file_path, page_type FROM pages WHERE id=?",
        (result["question_page_id"],),
    )
    assert question_page is not None
    assert question_page["page_type"] == "question"
    question_text = Path(question_page["file_path"]).read_text(encoding="utf-8")
    assert "## Question" in question_text
    assert prompt in question_text
    assert "fallback answer" in question_text.lower()

    root_index = (Path(project.root_path) / "index.md").read_text(encoding="utf-8")
    assert "## Questions (1)" in root_index
    assert "[[What are neural networks]]" in root_index

    log_text = (Path(project.root_path) / "log.md").read_text(encoding="utf-8")
    assert "query | What are neural networks?" in log_text
