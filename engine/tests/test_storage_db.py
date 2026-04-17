"""Tests for database migrations and CRUD helpers."""
import pytest
import pytest_asyncio
from pathlib import Path

from knowler_engine.storage.db import Database, open_project_db, open_global_db


@pytest.mark.asyncio
async def test_project_db_migrations_run(tmp_path: Path):
    # open_project_db takes project root; DB is at root/.knowler/project.db
    db = await open_project_db(tmp_path / "proj")
    tables = await db.fetchall(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    names = {row["name"] for row in tables}
    required = {
        "schema_migrations", "projects", "sources", "source_versions",
        "entities", "claims", "relations", "pages", "query_runs",
        "artifacts", "content_units", "jobs", "job_events", "maintenance_findings",
    }
    missing = required - names
    assert missing == set(), f"Missing tables: {missing}"
    await db.close()


@pytest.mark.asyncio
async def test_global_db_migrations_run(tmp_path: Path):
    # open_global_db takes support dir; DB is at dir/knowler.db
    db = await open_global_db(tmp_path / "support")
    tables = await db.fetchall(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    names = {row["name"] for row in tables}
    assert "global_projects" in names
    assert "app_settings" in names
    await db.close()


@pytest.mark.asyncio
async def test_fts_table_exists(tmp_path: Path):
    db = await open_project_db(tmp_path / "proj")
    rows = await db.fetchall(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='content_units_fts'"
    )
    assert len(rows) == 1, "content_units_fts FTS table not created"
    await db.close()


@pytest.mark.asyncio
async def test_wal_mode_enabled(tmp_path: Path):
    db = await open_project_db(tmp_path / "proj")
    row = await db.fetchone("PRAGMA journal_mode")
    assert row[0] == "wal"
    await db.close()


@pytest.mark.asyncio
async def test_foreign_keys_enabled(tmp_path: Path):
    db = await open_project_db(tmp_path / "proj")
    row = await db.fetchone("PRAGMA foreign_keys")
    assert row[0] == 1
    await db.close()


@pytest.mark.asyncio
async def test_migrations_idempotent(tmp_path: Path):
    root = tmp_path / "proj"
    db = await open_project_db(root)
    await db.close()
    db2 = await open_project_db(root)
    rows = await db2.fetchall("SELECT * FROM schema_migrations ORDER BY version")
    versions = [r["version"] for r in rows]
    assert len(versions) == len(set(versions)), "Duplicate migration entries"
    await db2.close()


@pytest.mark.asyncio
async def test_execute_and_fetchone(tmp_path: Path):
    db = await open_project_db(tmp_path / "proj")
    # Insert a project row first (required by FK)
    await db.execute(
        "INSERT INTO projects (id, name, slug, root_path) VALUES (?, ?, ?, ?)",
        ("p1", "Test", "test", str(tmp_path))
    )
    await db.commit()
    # Insert a content_unit
    await db.execute(
        "INSERT INTO content_units (id, project_id, unit_type, parent_kind, parent_id, body) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("cu-1", "p1", "page_body", "page", "pg-1", "hello world")
    )
    await db.commit()
    row = await db.fetchone("SELECT body FROM content_units WHERE id = ?", ("cu-1",))
    assert row["body"] == "hello world"
    await db.close()


@pytest.mark.asyncio
async def test_transaction_rollback_on_error(tmp_path: Path):
    db = await open_project_db(tmp_path / "proj")
    await db.execute(
        "INSERT INTO projects (id, name, slug, root_path) VALUES (?, ?, ?, ?)",
        ("p1", "Test", "test", str(tmp_path))
    )
    await db.commit()

    try:
        async with db.transaction():
            await db.execute(
                "INSERT INTO content_units (id, project_id, unit_type, parent_kind, parent_id, body) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("cu-tx", "p1", "page_body", "page", "pg-1", "will roll back")
            )
            raise RuntimeError("deliberate failure")
    except RuntimeError:
        pass

    row = await db.fetchone("SELECT id FROM content_units WHERE id = ?", ("cu-tx",))
    assert row is None, "Transaction should have been rolled back"
    await db.close()


@pytest.mark.asyncio
async def test_fetchall_returns_list(tmp_path: Path):
    db = await open_project_db(tmp_path / "proj")
    rows = await db.fetchall("SELECT * FROM schema_migrations")
    assert isinstance(rows, list)
    assert len(rows) >= 1
    await db.close()


@pytest.mark.asyncio
async def test_global_db_is_separate_from_project_db(tmp_path: Path):
    proj_db = await open_project_db(tmp_path / "proj")
    global_db = await open_global_db(tmp_path / "support")
    # global has global_projects, project db does not
    proj_tables = {r["name"] for r in await proj_db.fetchall(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    global_tables = {r["name"] for r in await global_db.fetchall(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert "global_projects" in global_tables
    assert "global_projects" not in proj_tables
    assert "sources" in proj_tables
    assert "sources" not in global_tables
    await proj_db.close()
    await global_db.close()
