"""Tests for deterministic fallback behavior when the LLM is unavailable."""
import json
from pathlib import Path

import pytest
import pytest_asyncio

from knowler_engine.compile.pipeline import compile_concept_page, compile_source_summary
from knowler_engine.llm.provider import LLMProvider
from knowler_engine.normalize.pipeline import normalize_source
from knowler_engine.project.manager import ProjectManager
from knowler_engine.storage.db import open_global_db

SAMPLE_PAPER_TEXT = """---
source_id: src_test_bert
title: "1810.04805v2"
author: ""
page_count: 16
source_date: "2019-05-24"
extracted_at: "2026-04-06T07:30:48.057601+00:00"
---
## Page 1

BERT: Pre-training of Deep Bidirectional Transformers for
Language Understanding
Jacob Devlin Ming-Wei Chang Kenton Lee Kristina Toutanova
Google AI Language
{jacobdevlin,mingweichang,kentonl,kristout}@google.com
Abstract
We introduce a new language representation model called BERT, which stands for Bidirectional Encoder Representations from Transformers. Unlike recent language representation models, BERT is designed to pre-train deep bidirectional representations from unlabeled text by jointly conditioning on both left and right context in all layers. The pre-trained BERT model can be fine-tuned with just one additional output layer to create state-of-the-art models for a wide range of tasks such as question answering and language inference.
1 Introduction
Language model pre-training has been shown to be effective for improving many natural language processing tasks.
"""


@pytest_asyncio.fixture
async def project(tmp_path: Path):
    global_db = await open_global_db(tmp_path / "support")
    manager = ProjectManager(global_db=global_db)
    proj = await manager.create_project(
        name="Fallback Project",
        slug=None,
        root_path=str(tmp_path / "fallback-project"),
    )
    yield proj
    await manager.close_all()
    await global_db.close()


async def _insert_pdf_source(project, title: str = "1810.04805v2") -> str:
    raw_pdf = project.vault.raw / "papers" / "1810.04805v2.pdf"
    raw_pdf.parent.mkdir(parents=True, exist_ok=True)
    raw_pdf.write_bytes(b"%PDF-1.4 fake pdf")

    text_path = project.vault.raw / "papers" / "src_test_bert_text.md"
    text_path.write_text(SAMPLE_PAPER_TEXT, encoding="utf-8")

    source_id = "src_test_bert"
    await project.db.execute(
        """
        INSERT INTO sources(
          id, project_id, source_type, origin_type, title, raw_path, mime_type,
          trust_level, status, ingest_state, metadata_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            source_id,
            project.id,
            "pdf",
            "file_upload",
            title,
            str(raw_pdf),
            "application/pdf",
            "high",
            "pending",
            "approved",
            json.dumps({"extracted_text_path": str(text_path)}),
        ),
    )
    await project.db.commit()
    return source_id


def _unconfigured_llm() -> LLMProvider:
    provider = LLMProvider(provider="anthropic", api_key=None)
    provider._api_key = None
    return provider


@pytest.mark.asyncio
async def test_normalize_source_uses_deterministic_summary_when_llm_missing(project):
    source_id = await _insert_pdf_source(project)

    normalized = await normalize_source(
        source_id=source_id,
        project_id=project.id,
        db=project.db,
        vault=project.vault,
        llm=_unconfigured_llm(),
    )

    assert normalized["summary"].startswith("We introduce a new language representation model called BERT")
    assert any(entity["name"] == "BERT" for entity in normalized["entities"])
    assert len(normalized["claims"]) >= 1

    source_row = await project.db.fetchone("SELECT title, metadata_json FROM sources WHERE id=?", (source_id,))
    assert source_row["title"] == "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding"
    metadata = json.loads(source_row["metadata_json"])
    assert metadata["summary"].startswith("We introduce a new language representation model called BERT")


@pytest.mark.asyncio
async def test_compile_source_summary_fallback_writes_useful_page(project):
    source_id = await _insert_pdf_source(project)

    await normalize_source(
        source_id=source_id,
        project_id=project.id,
        db=project.db,
        vault=project.vault,
        llm=_unconfigured_llm(),
    )

    page_id = await compile_source_summary(
        source_id=source_id,
        project_id=project.id,
        db=project.db,
        vault=project.vault,
        llm=_unconfigured_llm(),
    )

    assert page_id is not None

    page_path = project.vault.wiki / "sources" / "bert-pre-training-of-deep-bidirectional-transformers-for-language-understanding.md"
    content = page_path.read_text(encoding="utf-8")

    assert 'source_url: ""' in content
    assert "We introduce a new language representation model called BERT" in content
    assert "- [[BERT]]" in content
    assert "No claims extracted." not in content


@pytest.mark.asyncio
async def test_compile_concept_page_fallback_writes_concept_page(project):
    source_id = await _insert_pdf_source(project)

    await normalize_source(
        source_id=source_id,
        project_id=project.id,
        db=project.db,
        vault=project.vault,
        llm=_unconfigured_llm(),
    )

    entity_row = await project.db.fetchone(
        "SELECT id FROM entities WHERE project_id=? AND display_name=?",
        (project.id, "BERT"),
    )
    assert entity_row is not None

    page_id = await compile_concept_page(
        entity_id=entity_row["id"],
        project_id=project.id,
        db=project.db,
        vault=project.vault,
        llm=_unconfigured_llm(),
    )

    assert page_id is not None

    content = (project.vault.wiki / "concepts" / "bert.md").read_text(encoding="utf-8")
    assert "## Overview" in content
    assert "## Supporting Sources" in content
    assert "[[BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding]]" in content
