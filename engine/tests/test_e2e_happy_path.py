"""
End-to-end happy path test: create project → vault structure → search → atomic write.
LLM-dependent steps are skipped (they require a live API key).
"""
import json
import pytest
import pytest_asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

from knowler_engine.storage.db import open_global_db
from knowler_engine.storage.atomic import write_atomic, snapshot
from knowler_engine.project.manager import ProjectManager
from knowler_engine.search.fts import search_project


@pytest_asyncio.fixture
async def project(tmp_path: Path):
    global_db = await open_global_db(tmp_path / "support")
    mgr = ProjectManager(global_db=global_db)
    proj = await mgr.create_project(
        name="E2E Test Project",
        slug=None,
        root_path=str(tmp_path / "e2e-test"),
    )
    yield proj
    await mgr.close_all()
    await global_db.close()


@pytest.mark.asyncio
async def test_project_creation_e2e(project):
    assert project.id is not None
    assert project.id.startswith("proj_")
    assert project.vault.wiki.exists()
    assert project.vault.raw.exists()
    assert project.vault.db_path.exists()


@pytest.mark.asyncio
async def test_vault_structure_complete(project):
    vault = project.vault
    expected_dirs = [vault.wiki, vault.raw, vault.normalized, vault.outputs, vault.dot_knowler]
    for d in expected_dirs:
        assert d.exists(), f"Missing directory: {d}"


@pytest.mark.asyncio
async def test_project_db_has_project_row(project):
    row = await project.db.fetchone(
        "SELECT id, name FROM projects WHERE id=?", (project.id,)
    )
    assert row is not None
    assert row["name"] == "E2E Test Project"


@pytest.mark.asyncio
async def test_config_yaml_written(project):
    config_file = Path(project.root_path) / "config.yaml"
    assert config_file.exists()
    content = config_file.read_text()
    assert "E2E Test Project" in content
    assert project.id in content


@pytest.mark.asyncio
async def test_search_after_content_inserted(project):
    """After inserting content units, FTS search should work."""
    await project.db.execute(
        "INSERT INTO content_units (id, project_id, unit_type, parent_kind, parent_id, body, title) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("cu-e2e", project.id, "page_body", "page", "pg-1",
         "Machine Learning is a fascinating field of AI research",
         "Machine Learning")
    )
    await project.db.commit()
    row = await project.db.fetchone("SELECT rowid FROM content_units WHERE id='cu-e2e'")
    await project.db.execute(
        "INSERT INTO content_units_fts(rowid, title, body, project_id) VALUES (?, ?, ?, ?)",
        (row[0], "Machine Learning", "Machine Learning is a fascinating field of AI research", project.id)
    )
    await project.db.commit()

    results = await search_project(project.db, project.id, "machine learning", limit=5)
    assert len(results) >= 1
    assert any("Machine Learning" in r["body_excerpt"] for r in results)


@pytest.mark.asyncio
async def test_atomic_write_and_read(project):
    """Write a wiki page atomically and verify it can be read back."""
    page_path = project.vault.wiki_path("concept", "test-concept")
    page_path.parent.mkdir(parents=True, exist_ok=True)
    content = "# Test Concept\n\nThis is a test."

    write_atomic(page_path, content, expected_revision=None, tmp_dir=project.vault.tmp_dir)

    assert page_path.exists()
    assert page_path.read_text() == content


@pytest.mark.asyncio
async def test_atomic_write_conflict_detected(project):
    """If a file changes between snapshot and write, ConflictError should be raised."""
    from knowler_engine.storage.atomic import ConflictError

    page_path = project.vault.wiki_path("concept", "conflict-test")
    page_path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(page_path, "v1", expected_revision=None)

    rev = snapshot(page_path)
    # Simulate concurrent modification
    page_path.write_text("externally modified")

    with pytest.raises(ConflictError):
        write_atomic(page_path, "v2", expected_revision=rev)


@pytest.mark.asyncio
async def test_multiple_projects_independent(tmp_path: Path):
    """Two projects should have independent DBs and vaults."""
    global_db = await open_global_db(tmp_path / "support")
    mgr = ProjectManager(global_db=global_db)

    proj_a = await mgr.create_project(name="Project A", slug=None, root_path=str(tmp_path / "proj-a"))
    proj_b = await mgr.create_project(name="Project B", slug=None, root_path=str(tmp_path / "proj-b"))

    assert proj_a.id != proj_b.id
    assert proj_a.vault.root != proj_b.vault.root
    assert proj_a.db is not proj_b.db

    projects = await mgr.list_projects()
    assert len(projects) == 2

    await mgr.close_all()
    await global_db.close()
