"""Tests for project creation, opening, and listing."""
import pytest
import pytest_asyncio
from pathlib import Path

from knowler_engine.storage.db import open_global_db
from knowler_engine.project.manager import ProjectManager
from knowler_engine.ipc.router import EngineError


@pytest_asyncio.fixture
async def manager(tmp_path: Path):
    db = await open_global_db(tmp_path / "support")
    mgr = ProjectManager(global_db=db)
    yield mgr
    await db.close()


@pytest.mark.asyncio
async def test_create_project_returns_project(manager: ProjectManager, tmp_path: Path):
    proj = await manager.create_project(
        name="My Research",
        slug=None,
        root_path=str(tmp_path / "my-research"),
    )
    assert proj.id is not None
    assert proj.name == "My Research"
    assert proj.slug == "my-research"


@pytest.mark.asyncio
async def test_create_project_initializes_vault(manager: ProjectManager, tmp_path: Path):
    root = tmp_path / "vault-test"
    proj = await manager.create_project(name="Vault Test", slug=None, root_path=str(root))
    assert (root / ".knowler" / "project.db").exists()
    assert (root / "wiki").exists()
    assert (root / "raw").exists()
    assert (root / "outputs").exists()


@pytest.mark.asyncio
async def test_create_project_writes_config(manager: ProjectManager, tmp_path: Path):
    root = tmp_path / "config-test"
    proj = await manager.create_project(name="Config Test", slug=None, root_path=str(root))
    config_file = root / "config.yaml"
    assert config_file.exists()
    content = config_file.read_text()
    assert "Config Test" in content


@pytest.mark.asyncio
async def test_create_project_registers_in_global_db(manager: ProjectManager, tmp_path: Path):
    root = tmp_path / "global-reg"
    proj = await manager.create_project(name="Global Reg", slug=None, root_path=str(root))
    projects = await manager.list_projects()
    ids = [p["id"] for p in projects]
    assert proj.id in ids


@pytest.mark.asyncio
async def test_create_project_allows_existing_content_folder(
    manager: ProjectManager, tmp_path: Path
):
    root = tmp_path / "existing-content"
    root.mkdir()
    (root / "paper.pdf").write_text("stub", encoding="utf-8")
    (root / "notes.md").write_text("# Notes", encoding="utf-8")

    proj = await manager.create_project(
        name="Existing Content",
        slug=None,
        root_path=str(root),
    )

    assert proj.root_path == str(root.resolve())
    assert (root / "config.yaml").exists()
    assert (root / ".knowler" / "project.db").exists()


@pytest.mark.asyncio
async def test_open_project_by_path(manager: ProjectManager, tmp_path: Path):
    root = tmp_path / "open-test"
    created = await manager.create_project(name="Open Test", slug=None, root_path=str(root))
    opened = await manager.open_project(str(root))
    assert opened.id == created.id


@pytest.mark.asyncio
async def test_open_project_recovers_missing_config_from_project_db(
    manager: ProjectManager, tmp_path: Path
):
    root = tmp_path / "legacy-open-test"
    created = await manager.create_project(name="Legacy Open Test", slug=None, root_path=str(root))
    (root / "config.yaml").unlink()

    opened = await manager.open_project(str(root))

    assert opened.id == created.id
    assert opened.name == "Legacy Open Test"
    assert (root / "config.yaml").exists()


@pytest.mark.asyncio
async def test_list_projects_empty_initially(tmp_path: Path):
    db = await open_global_db(tmp_path / "fresh-support")
    mgr = ProjectManager(global_db=db)
    projects = await mgr.list_projects()
    assert projects == []
    await db.close()


@pytest.mark.asyncio
async def test_list_projects_returns_dicts(manager: ProjectManager, tmp_path: Path):
    root = tmp_path / "dict-test"
    proj = await manager.create_project(name="Dict Test", slug=None, root_path=str(root))
    projects = await manager.list_projects()
    assert len(projects) >= 1
    assert isinstance(projects[0], dict)
    assert "id" in projects[0]
    assert "project_id" in projects[0]
    assert "name" in projects[0]


@pytest.mark.asyncio
async def test_get_project_by_id(manager: ProjectManager, tmp_path: Path):
    root = tmp_path / "get-by-id"
    proj = await manager.create_project(name="Get By ID", slug=None, root_path=str(root))
    fetched = await manager.get_project(proj.id)
    assert fetched.id == proj.id
    assert fetched.name == "Get By ID"


@pytest.mark.asyncio
async def test_delete_project_removes_it_from_global_registry(
    manager: ProjectManager, tmp_path: Path
):
    root = tmp_path / "delete-project"
    proj = await manager.create_project(name="Delete Me", slug=None, root_path=str(root))

    deleted = await manager.delete_project(proj.id)

    assert deleted["id"] == proj.id
    assert deleted["name"] == "Delete Me"
    projects = await manager.list_projects()
    assert all(p["project_id"] != proj.id for p in projects)
    with pytest.raises(EngineError):
        await manager.get_project(proj.id)


@pytest.mark.asyncio
async def test_get_nonexistent_project_raises(manager: ProjectManager):
    with pytest.raises(EngineError):
        await manager.get_project("nonexistent-id-123")


@pytest.mark.asyncio
async def test_slugify_handles_spaces(manager: ProjectManager, tmp_path: Path):
    root = tmp_path / "spaces-slug"
    proj = await manager.create_project(name="My Cool Project", slug=None, root_path=str(root))
    assert " " not in proj.slug
    assert proj.slug == "my-cool-project"


@pytest.mark.asyncio
async def test_create_project_conflict_on_existing_slug(manager: ProjectManager, tmp_path: Path):
    root1 = tmp_path / "proj-a"
    root2 = tmp_path / "proj-b"
    await manager.create_project(name="Same Slug", slug="same-slug", root_path=str(root1))
    with pytest.raises(EngineError):
        await manager.create_project(name="Same Slug 2", slug="same-slug", root_path=str(root2))


@pytest.mark.asyncio
async def test_close_all_clears_open_projects(manager: ProjectManager, tmp_path: Path):
    root = tmp_path / "close-test"
    proj = await manager.create_project(name="Close Test", slug=None, root_path=str(root))
    assert proj.id in manager._open_projects
    await manager.close_all()
    assert len(manager._open_projects) == 0
