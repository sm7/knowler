"""Tests for the visualization knowledge map dataset."""
from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from knowler_engine.graph import build_knowledge_map, export_knowledge_graph
from knowler_engine.storage.vault import Vault
from knowler_engine.storage.db import open_project_db

PROJECT_ID = "proj-graph"


@pytest_asyncio.fixture
async def graph_db(tmp_path: Path):
    db = await open_project_db(tmp_path / "graph-project")
    await db.execute(
        "INSERT INTO projects (id, name, slug, root_path) VALUES (?, ?, ?, ?)",
        (PROJECT_ID, "Graph Project", "graph-project", str(tmp_path / "graph-project")),
    )
    await db.commit()
    yield db
    await db.close()


@pytest.mark.asyncio
async def test_build_knowledge_map_returns_empty_when_no_entities(graph_db):
    graph = await build_knowledge_map(graph_db, PROJECT_ID)
    assert graph["nodes"] == []
    assert graph["edges"] == []
    assert graph["stats"]["group_count"] == 0
    assert graph["stats"]["explicit_edge_count"] == 0
    assert graph["stats"]["inferred_edge_count"] == 0


@pytest.mark.asyncio
async def test_build_knowledge_map_includes_explicit_and_inferred_edges(graph_db, tmp_path: Path):
    wiki_dir = tmp_path / "graph-project" / "wiki" / "concepts"
    wiki_dir.mkdir(parents=True, exist_ok=True)
    page_path = wiki_dir / "neural-networks.md"
    page_path.write_text(
        """---
id: "pg_neural"
project_id: "proj-graph"
page_type: "concept"
title: "Neural Networks"
---

Neural networks are layered models for representation learning.
""",
        encoding="utf-8",
    )

    await graph_db.executemany(
        """
        INSERT INTO sources(id, project_id, source_type, origin_type, title, raw_path, trust_level, status, ingest_state)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("src_a", PROJECT_ID, "pdf", "file_upload", "Paper A", str(tmp_path / "a.pdf"), "high", "compiled", "approved"),
            ("src_b", PROJECT_ID, "pdf", "file_upload", "Paper B", str(tmp_path / "b.pdf"), "high", "compiled", "approved"),
        ],
    )
    await graph_db.executemany(
        """
        INSERT INTO entities(id, project_id, entity_type, canonical_name, display_name, description, confidence, created_from_source_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("ent_neural", PROJECT_ID, "concept", "neural networks", "Neural Networks", "Layered function approximators.", 0.9, "src_a"),
            ("ent_transformers", PROJECT_ID, "concept", "transformers", "Transformers", "Attention-based architectures.", 0.88, "src_a"),
            ("ent_attention", PROJECT_ID, "concept", "attention", "Attention", "A mechanism for focusing on relevant tokens.", 0.83, "src_b"),
        ],
    )
    await graph_db.execute(
        """
        INSERT INTO pages(id, project_id, page_type, title, slug, file_path, status)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("pg_neural", PROJECT_ID, "concept", "Neural Networks", "neural-networks", str(page_path), "active"),
    )
    await graph_db.executemany(
        """
        INSERT INTO relations(id, project_id, from_kind, from_id, relation_type, to_kind, to_id, confidence, evidence_source_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("rel_about", PROJECT_ID, "page", "pg_neural", "about", "entity", "ent_neural", 1.0, None),
            ("rel_explicit", PROJECT_ID, "entity", "ent_neural", "related_to", "entity", "ent_transformers", 0.92, "src_a"),
            ("rel_source_transformers", PROJECT_ID, "source", "src_b", "mentions", "entity", "ent_transformers", 0.8, None),
            ("rel_source_attention", PROJECT_ID, "source", "src_b", "mentions", "entity", "ent_attention", 0.8, None),
        ],
    )
    await graph_db.commit()

    graph = await build_knowledge_map(graph_db, PROJECT_ID)

    assert graph["stats"]["node_count"] == 3
    assert graph["stats"]["edge_count"] >= 2
    assert graph["stats"]["group_count"] >= 1
    assert graph["stats"]["group_count"] < graph["stats"]["node_count"]

    neural = next(node for node in graph["nodes"] if node["id"] == "ent_neural")
    assert neural["page_id"] == "pg_neural"
    assert "representation learning" in neural["summary"].lower()
    assert neural["lead_ideas"]
    assert neural["group_label"]
    assert neural["group_rationale"]
    assert neural["supporting_sources"] == ["Paper A"]
    assert any(idea["label"] == "Transformers" for idea in neural["lead_ideas"])

    explicit_edge = next(
        edge for edge in graph["edges"]
        if {edge["source"], edge["target"]} == {"ent_neural", "ent_transformers"}
    )
    assert explicit_edge["kind"] == "explicit"
    assert explicit_edge["provenance_kind"] in {"explicit_relation", "hybrid"}
    assert "Paper A" in explicit_edge["source_titles"]
    assert "Explicit relation" in explicit_edge["provenance_summary"]

    inferred_edge = next(
        edge for edge in graph["edges"]
        if {edge["source"], edge["target"]} == {"ent_attention", "ent_transformers"}
    )
    assert inferred_edge["kind"] == "inferred"
    assert inferred_edge["provenance_kind"] == "shared_support"
    assert "Paper B" in inferred_edge["source_titles"]
    assert "Inferred from" in inferred_edge["provenance_summary"]

    primary_group = graph["groups"][0]
    assert primary_group["label"]
    assert primary_group["rationale"]
    assert isinstance(primary_group["themes"], list)
    assert isinstance(primary_group["representative_nodes"], list)


@pytest.mark.asyncio
async def test_export_knowledge_graph_writes_json_and_report(graph_db, tmp_path: Path):
    vault = Vault(root=tmp_path / "graph-project")
    vault.initialize()

    await graph_db.execute(
        """
        INSERT INTO entities(id, project_id, entity_type, canonical_name, display_name, description, confidence)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("ent_single", PROJECT_ID, "concept", "single node", "Single Node", "An isolated test entity.", 0.7),
    )
    await graph_db.commit()

    exported = await export_knowledge_graph(graph_db, PROJECT_ID, vault)
    graph_path = Path(exported["graph_path"])
    report_path = Path(exported["report_path"])

    assert graph_path.exists()
    assert report_path.exists()
    assert '"project_id": "proj-graph"' in graph_path.read_text(encoding="utf-8")
    report = report_path.read_text(encoding="utf-8")
    assert "Knowledge Graph Report" in report
    assert "Hub Concepts" in report


@pytest.mark.asyncio
async def test_build_knowledge_map_uses_concept_page_references_for_missing_links(graph_db, tmp_path: Path):
    wiki_dir = tmp_path / "graph-project" / "wiki" / "concepts"
    wiki_dir.mkdir(parents=True, exist_ok=True)
    transformers_page = wiki_dir / "transformers.md"
    gpt3_page = wiki_dir / "gpt-3.md"
    transformers_page.write_text(
        """---
id: "pg_transformers"
project_id: "proj-graph"
page_type: "concept"
title: "Transformers"
---

Transformers are self-attention models.
""",
        encoding="utf-8",
    )
    gpt3_page.write_text(
        """---
id: "pg_gpt3"
project_id: "proj-graph"
page_type: "concept"
title: "GPT-3"
---

GPT-3 is a large language model built on the [[Transformers]] architecture.
""",
        encoding="utf-8",
    )

    await graph_db.executemany(
        """
        INSERT INTO sources(id, project_id, source_type, origin_type, title, raw_path, trust_level, status, ingest_state)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("src_transformers", PROJECT_ID, "pdf", "file_upload", "Attention Is All You Need", str(tmp_path / "transformers.pdf"), "high", "compiled", "approved"),
            ("src_gpt3", PROJECT_ID, "pdf", "file_upload", "Language Models are Few-Shot Learners", str(tmp_path / "gpt3.pdf"), "high", "compiled", "approved"),
        ],
    )
    await graph_db.executemany(
        """
        INSERT INTO entities(id, project_id, entity_type, canonical_name, display_name, description, confidence, created_from_source_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("ent_transformers_ref", PROJECT_ID, "concept", "transformers", "Transformers", "Attention-based sequence architecture.", 0.9, "src_transformers"),
            ("ent_gpt3_ref", PROJECT_ID, "model", "gpt-3", "GPT-3", "Large autoregressive language model.", 0.88, "src_gpt3"),
        ],
    )
    await graph_db.executemany(
        """
        INSERT INTO pages(id, project_id, page_type, title, slug, file_path, status)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("pg_transformers", PROJECT_ID, "concept", "Transformers", "transformers", str(transformers_page), "active"),
            ("pg_gpt3", PROJECT_ID, "concept", "GPT-3", "gpt-3", str(gpt3_page), "active"),
        ],
    )
    await graph_db.executemany(
        """
        INSERT INTO relations(id, project_id, from_kind, from_id, relation_type, to_kind, to_id, confidence)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("rel_about_transformers_ref", PROJECT_ID, "page", "pg_transformers", "about", "entity", "ent_transformers_ref", 1.0),
            ("rel_about_gpt3_ref", PROJECT_ID, "page", "pg_gpt3", "about", "entity", "ent_gpt3_ref", 1.0),
        ],
    )
    await graph_db.commit()

    graph = await build_knowledge_map(graph_db, PROJECT_ID)
    edge = next(
        edge for edge in graph["edges"]
        if {edge["source"], edge["target"]} == {"ent_transformers_ref", "ent_gpt3_ref"}
    )
    assert edge["kind"] == "explicit"
    assert edge["relation_type"] in {"wiki_link", "page_reference"}
    assert "GPT-3" in edge["provenance_summary"]
