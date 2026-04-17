"""Tests for FTS search and relation expansion."""
import pytest
import pytest_asyncio
from pathlib import Path

from knowler_engine.storage.db import open_project_db
from knowler_engine.search.fts import search_project, exact_search, relation_expand


async def _setup_project(db, project_id: str, tmp_path: Path):
    """Insert a project row required by FK constraints."""
    await db.execute(
        "INSERT INTO projects (id, name, slug, root_path) VALUES (?, ?, ?, ?)",
        (project_id, "Test", "test", str(tmp_path))
    )
    await db.commit()


async def _insert_content_unit(db, cu_id, project_id, body, title=None, unit_type="page_body", parent_kind="page", parent_id="pg-1"):
    cursor = await db.execute(
        "INSERT INTO content_units (id, project_id, unit_type, parent_kind, parent_id, body, title) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (cu_id, project_id, unit_type, parent_kind, parent_id, body, title)
    )
    await db.commit()
    # Get rowid for FTS sync
    row = await db.fetchone("SELECT rowid FROM content_units WHERE id=?", (cu_id,))
    return row[0]


@pytest_asyncio.fixture
async def db_with_content(tmp_path: Path):
    db = await open_project_db(tmp_path / "search_proj")
    project_id = "proj-1"
    await _setup_project(db, project_id, tmp_path)

    # Insert content units and sync to FTS
    units = [
        ("cu-1", "Machine learning is a subset of artificial intelligence", "Machine Learning"),
        ("cu-2", "Deep learning uses neural networks with many layers", "Deep Learning"),
        ("cu-3", "Transformers are attention-based machine learning models", None),
    ]
    for cu_id, body, title in units:
        rowid = await _insert_content_unit(db, cu_id, project_id, body, title)
        await db.execute(
            "INSERT INTO content_units_fts(rowid, title, body, project_id) VALUES (?, ?, ?, ?)",
            (rowid, title or "", body, project_id)
        )
    await db.commit()
    yield db
    await db.close()


@pytest.mark.asyncio
async def test_search_finds_matching_content(db_with_content):
    results = await search_project(db_with_content, "proj-1", "machine learning", limit=10)
    assert len(results) > 0
    bodies = [r["body_excerpt"] for r in results]
    assert any("machine learning" in b.lower() for b in bodies)


@pytest.mark.asyncio
async def test_search_empty_query_returns_empty(db_with_content):
    results = await search_project(db_with_content, "proj-1", "", limit=10)
    assert results == []


@pytest.mark.asyncio
async def test_search_no_match_returns_empty(db_with_content):
    results = await search_project(db_with_content, "proj-1", "quantum chromodynamics", limit=10)
    assert results == []


@pytest.mark.asyncio
async def test_search_scoped_to_project(db_with_content):
    results = await search_project(db_with_content, "proj-999", "machine learning", limit=10)
    assert results == []


@pytest.mark.asyncio
async def test_search_result_structure(db_with_content):
    results = await search_project(db_with_content, "proj-1", "neural networks", limit=10)
    assert len(results) >= 1
    result = results[0]
    assert "content_unit_id" in result
    assert "body_excerpt" in result
    assert "score" in result
    assert "unit_type" in result


@pytest.mark.asyncio
async def test_exact_search_by_title(db_with_content):
    # exact_search queries pages and entities tables, so insert a page row
    await db_with_content.execute(
        "INSERT INTO pages (id, project_id, page_type, slug, file_path, title, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("pg-ml", "proj-1", "concept", "machine-learning",
         "wiki/concepts/machine-learning.md", "Machine Learning", "active")
    )
    await db_with_content.commit()
    results = await exact_search(db_with_content, "proj-1", "Machine Learning")
    assert len(results) >= 1
    assert any("Machine Learning" in r.get("title", "") for r in results)


@pytest.mark.asyncio
async def test_exact_search_no_match(db_with_content):
    results = await exact_search(db_with_content, "proj-1", "ZZZ_Nonexistent_ZZZ")
    assert results == []


@pytest.mark.asyncio
async def test_relation_expand_returns_list(db_with_content):
    results = await relation_expand(db_with_content, "proj-1", [], hops=1)
    assert isinstance(results, list)


@pytest.mark.asyncio
async def test_relation_expand_empty_input(db_with_content):
    results = await relation_expand(db_with_content, "proj-1", [], hops=1)
    assert results == []
