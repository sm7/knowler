"""
Project manager.

Handles project creation, opening, listing, and configuration.
One project = one vault directory + one SQLite database.
The global DB tracks all known projects for the Projects screen.
"""
from __future__ import annotations

import json
import pathlib
import re
import uuid
from dataclasses import dataclass
from typing import Any

import yaml
from ulid import ULID

import structlog

from knowler_engine.ipc.router import EngineError
from knowler_engine.ipc.types import ErrorCode
from knowler_engine.storage.db import Database, open_project_db
from knowler_engine.storage.vault import Vault, create_vault, open_vault
from knowler_engine.system_pages import build_project_indexes, rebuild_project_log

log = structlog.get_logger(__name__)


@dataclass
class Project:
    id: str
    name: str
    slug: str
    root_path: str
    status: str
    config: dict[str, Any]
    db: Database
    vault: Vault


class ProjectManager:
    """
    Manages the lifecycle of projects.

    Holds a registry of open project handles (project_id -> Project).
    The global DB is used for project listing.
    """

    def __init__(self, global_db: Database) -> None:
        self._global_db = global_db
        self._open_projects: dict[str, Project] = {}

    async def create_project(
        self,
        name: str,
        slug: str | None,
        root_path: str,
        config: dict[str, Any] | None = None,
    ) -> Project:
        """
        Create a new project at root_path.

        - Initializes the vault directory structure
        - Creates config.yaml
        - Opens and migrates the project DB
        - Registers in the global DB
        """
        if slug is None:
            slug = _slugify(name)

        # Validate
        if not slug:
            raise EngineError(ErrorCode.VALIDATION_ERROR, "slug cannot be empty")
        if await self._global_project_exists(slug=slug):
            raise EngineError(ErrorCode.CONFLICT, f"A project with slug '{slug}' already exists")

        root = pathlib.Path(root_path).expanduser().resolve()
        if root.exists() and root.is_file():
            raise EngineError(
                ErrorCode.CONFLICT,
                f"Root path {root_path} is a file. Choose a directory instead.",
            )
        if (root / "config.yaml").exists():
            raise EngineError(
                ErrorCode.CONFLICT,
                f"{root} is already a Knowler project. Use open_project instead.",
            )

        project_id = f"proj_{ULID()}"
        cfg = config or {}
        cfg.setdefault("default_model_tier", "balanced")
        cfg.setdefault("trusted_domains", [])
        cfg.setdefault("privacy_mode", "local_first")

        # Initialize vault
        vault = create_vault(root)

        # Write config.yaml
        config_data = {
            "id": project_id,
            "name": name,
            "slug": slug,
            "version": 1,
            **cfg,
        }
        config_yaml = yaml.dump(config_data, default_flow_style=False, allow_unicode=True)
        (root / "config.yaml").write_text(config_yaml, encoding="utf-8")

        # Open and migrate project DB
        db = await open_project_db(root)

        # Insert project row into project DB
        await db.execute(
            """
            INSERT INTO projects(id, name, slug, root_path, config_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (project_id, name, slug, str(root), json.dumps(cfg)),
        )
        await db.commit()

        # Register in global DB
        await self._global_db.execute(
            """
            INSERT INTO global_projects(id, name, slug, root_path)
            VALUES (?, ?, ?, ?)
            """,
            (project_id, name, slug, str(root)),
        )
        await self._global_db.commit()

        await build_project_indexes(project_id, db, vault)
        await rebuild_project_log(project_id, db, vault)

        project = Project(
            id=project_id,
            name=name,
            slug=slug,
            root_path=str(root),
            status="active",
            config=cfg,
            db=db,
            vault=vault,
        )
        self._open_projects[project_id] = project
        log.info("project_created", project_id=project_id, slug=slug, path=str(root))
        return project

    async def open_project(self, root_path: str) -> Project:
        """Open an existing project from its root path."""
        root = pathlib.Path(root_path).expanduser().resolve()
        config_file = root / "config.yaml"
        db: Database | None = None
        if config_file.exists():
            with open(config_file, "r", encoding="utf-8") as f:
                cfg_data = yaml.safe_load(f) or {}
        else:
            cfg_data, db = await self._recover_legacy_project(root)

        project_id = cfg_data.get("id")
        name = cfg_data.get("name", root.name)
        slug = cfg_data.get("slug", _slugify(name))

        if project_id and project_id in self._open_projects:
            return self._open_projects[project_id]

        if db is None:
            db = await open_project_db(root)

        # Reconcile: ensure global DB knows about this project
        row = await db.fetchone("SELECT id FROM projects LIMIT 1")
        if row:
            project_id = row["id"]
        elif project_id:
            await db.execute(
                "INSERT OR IGNORE INTO projects(id, name, slug, root_path) VALUES (?,?,?,?)",
                (project_id, name, slug, str(root)),
            )
            await db.commit()

        await self._global_db.execute(
            """
            INSERT INTO global_projects(id, name, slug, root_path, updated_at)
            VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            ON CONFLICT(root_path) DO UPDATE SET
              name=excluded.name,
              slug=excluded.slug,
              updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            """,
            (project_id or str(uuid.uuid4()), name, slug, str(root)),
        )
        await self._global_db.commit()

        vault = open_vault(root)

        project = Project(
            id=project_id or "",
            name=name,
            slug=slug,
            root_path=str(root),
            status=cfg_data.get("status", "active"),
            config={k: v for k, v in cfg_data.items() if k not in ("id", "name", "slug", "version")},
            db=db,
            vault=vault,
        )
        if project_id:
            self._open_projects[project_id] = project
        log.info("project_opened", project_id=project_id, slug=slug, path=str(root))
        return project

    async def _recover_legacy_project(
        self, root: pathlib.Path
    ) -> tuple[dict[str, Any], Database]:
        """
        Recover an older project layout that has a .knowler database but no config.yaml.
        """
        db_path = root / ".knowler" / "project.db"
        if not db_path.exists():
            raise EngineError(
                ErrorCode.NOT_FOUND,
                f"No config.yaml found at {root}. Is this a Knowler project?",
            )

        db = await open_project_db(root)
        row = await db.fetchone(
            "SELECT id, name, slug, root_path, config_json FROM projects ORDER BY rowid ASC LIMIT 1"
        )
        if not row:
            await db.close()
            raise EngineError(
                ErrorCode.NOT_FOUND,
                f"No config.yaml found at {root} and the project database has no metadata.",
            )

        config_json = row["config_json"] or "{}"
        try:
            cfg = json.loads(config_json)
        except Exception:
            cfg = {}

        cfg_data = {
            "id": row["id"],
            "name": row["name"] or root.name,
            "slug": row["slug"] or _slugify(row["name"] or root.name),
            "version": 1,
            **cfg,
        }
        config_yaml = yaml.dump(cfg_data, default_flow_style=False, allow_unicode=True)
        (root / "config.yaml").write_text(config_yaml, encoding="utf-8")
        log.info("project_config_recovered", project_id=cfg_data["id"], path=str(root))
        return cfg_data, db

    async def list_projects(self) -> list[dict[str, Any]]:
        """Return summary rows from the global DB."""
        rows = await self._global_db.fetchall(
            "SELECT id, name, slug, root_path, status, created_at, updated_at FROM global_projects ORDER BY updated_at DESC"
        )
        projects = []
        for row in rows:
            project = dict(row)
            project["project_id"] = project["id"]
            projects.append(project)
        return projects

    async def get_project(self, project_id: str) -> Project:
        """Return an open project handle, opening it if needed."""
        if project_id in self._open_projects:
            return self._open_projects[project_id]

        row = await self._global_db.fetchone(
            "SELECT root_path FROM global_projects WHERE id = ?", (project_id,)
        )
        if not row:
            raise EngineError(ErrorCode.NOT_FOUND, f"Project {project_id} not found")

        return await self.open_project(row["root_path"])

    async def delete_project(self, project_id: str) -> dict[str, Any]:
        """
        Remove a project from Knowler's global registry while keeping files on disk.
        """
        row = await self._global_db.fetchone(
            "SELECT id, name, slug, root_path, status FROM global_projects WHERE id = ?",
            (project_id,),
        )
        if not row:
            raise EngineError(ErrorCode.NOT_FOUND, f"Project {project_id} not found")

        project = self._open_projects.pop(project_id, None)
        if project is not None:
            await project.db.close()

        await self._global_db.execute(
            "DELETE FROM global_projects WHERE id = ?",
            (project_id,),
        )
        await self._global_db.commit()

        deleted = dict(row)
        deleted["project_id"] = deleted["id"]
        return deleted

    async def close_all(self) -> None:
        """Close all open project DBs."""
        for project in self._open_projects.values():
            await project.db.close()
        self._open_projects.clear()

    async def _global_project_exists(self, slug: str) -> bool:
        row = await self._global_db.fetchone(
            "SELECT id FROM global_projects WHERE slug = ?", (slug,)
        )
        return row is not None


def _slugify(name: str) -> str:
    """Convert name to a URL/filesystem-safe slug."""
    s = name.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")
