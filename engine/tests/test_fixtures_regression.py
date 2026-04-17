"""
Regression tests for conftest fixtures.

Guards against the bugs fixed in the stabilization pass:
  - Vault.initialize() is synchronous (was incorrectly awaited)
  - open_project_db() takes project root, not db_path
  - ProjectManager() takes only global_db (no projects_dir arg)
"""
import pytest
from pathlib import Path

from knowler_engine.storage.vault import Vault
from knowler_engine.storage.db import open_global_db, open_project_db
from knowler_engine.project.manager import ProjectManager


@pytest.mark.asyncio
async def test_vault_initialize_is_sync(tmp_path: Path):
    v = Vault(root=tmp_path / "vault")
    # Must NOT be a coroutine — calling it directly should work without await
    result = v.initialize()
    assert result is None  # sync void function
    assert (tmp_path / "vault" / "wiki").exists()
    assert (tmp_path / "vault" / "raw").exists()
    assert (tmp_path / "vault" / ".knowler").exists()


@pytest.mark.asyncio
async def test_open_project_db_takes_root_not_db_path(tmp_path: Path):
    root = tmp_path / "myproject"
    root.mkdir()
    # Passing root (not root/.knowler/project.db) must work
    db = await open_project_db(root)
    assert (root / ".knowler" / "project.db").exists()
    await db.close()


@pytest.mark.asyncio
async def test_project_manager_constructor_no_projects_dir(tmp_path: Path):
    global_db = await open_global_db(tmp_path / "support")
    # Constructor takes only global_db
    pm = ProjectManager(global_db=global_db)
    assert pm is not None
    await global_db.close()
