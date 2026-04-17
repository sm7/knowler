"""
Shared pytest fixtures for Knowler engine tests.
"""
import asyncio
import json
import tempfile
from pathlib import Path
from typing import AsyncGenerator

import pytest
import pytest_asyncio

from knowler_engine.storage.db import Database, open_project_db, open_global_db
from knowler_engine.storage.vault import Vault
from knowler_engine.project.manager import ProjectManager


@pytest.fixture(scope="session")
def event_loop_policy():
    return asyncio.DefaultEventLoopPolicy()


@pytest_asyncio.fixture
async def tmp_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest_asyncio.fixture
async def vault(tmp_dir: Path) -> Vault:
    v = Vault(root=tmp_dir / "test_vault")
    v.initialize()  # sync — creates all engine-owned directories
    return v


@pytest_asyncio.fixture
async def project_db(vault: Vault) -> AsyncGenerator[Database, None]:
    db = await open_project_db(vault.root)  # expects project root, not db path
    try:
        yield db
    finally:
        await db.close()


@pytest_asyncio.fixture
async def global_db(tmp_dir: Path) -> AsyncGenerator[Database, None]:
    db_path = tmp_dir / "global.db"
    db = await open_global_db(db_path)
    try:
        yield db
    finally:
        await db.close()


@pytest_asyncio.fixture
async def project_manager(global_db: Database) -> ProjectManager:
    return ProjectManager(global_db=global_db)
