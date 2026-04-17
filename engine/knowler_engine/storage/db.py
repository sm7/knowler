"""
SQLite database layer.

Manages per-project and global DB connections, runs migrations,
and provides a thin async query helper.

Architectural constraints:
- WAL mode, synchronous=NORMAL, foreign_keys=ON
- Migrations are applied atomically in ascending version order
- Migration files live in migrations/sqlite/*.sql
- Never edit a released migration file
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import re
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import aiosqlite
import structlog

log = structlog.get_logger(__name__)

# Path to migration files relative to this file's package root
_MIGRATIONS_DIR = (
    pathlib.Path(__file__).parent.parent.parent / "migrations" / "sqlite"
)


class Database:
    """Async SQLite database handle for a single DB file."""

    def __init__(self, db_path: str | pathlib.Path) -> None:
        self._db_path = pathlib.Path(db_path)
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        """Open the database and apply startup pragmas."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self._db_path))
        self._conn.row_factory = aiosqlite.Row

        await self._conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=NORMAL;
            PRAGMA foreign_keys=ON;
            PRAGMA temp_store=MEMORY;
            PRAGMA cache_size=-20000;
            PRAGMA busy_timeout=5000;
            """
        )
        log.info("db_opened", path=str(self._db_path))

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None
            log.info("db_closed", path=str(self._db_path))

    @property
    def conn(self) -> aiosqlite.Connection:
        if not self._conn:
            raise RuntimeError("Database not open — call open() first")
        return self._conn

    async def execute(
        self, sql: str, params: tuple | dict | None = None
    ) -> aiosqlite.Cursor:
        async with self._lock:
            if params is None:
                return await self.conn.execute(sql)
            return await self.conn.execute(sql, params)

    async def executemany(self, sql: str, params_list: list) -> None:
        async with self._lock:
            await self.conn.executemany(sql, params_list)

    async def fetchone(
        self, sql: str, params: tuple | dict | None = None
    ) -> aiosqlite.Row | None:
        cursor = await self.execute(sql, params)
        return await cursor.fetchone()

    async def fetchall(
        self, sql: str, params: tuple | dict | None = None
    ) -> list[aiosqlite.Row]:
        cursor = await self.execute(sql, params)
        return await cursor.fetchall()

    async def commit(self) -> None:
        async with self._lock:
            await self.conn.commit()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        """Async context manager for explicit transactions."""
        async with self._lock:
            await self.conn.execute("BEGIN")
        try:
            yield
            async with self._lock:
                await self.conn.execute("COMMIT")
        except Exception:
            async with self._lock:
                await self.conn.execute("ROLLBACK")
            raise

    async def run_migrations(self, migration_files: list[pathlib.Path] | None = None) -> None:
        """
        Apply pending migrations in ascending version order.

        On startup:
        1. Ensure schema_migrations table exists
        2. Determine which versions have been applied
        3. Apply pending files inside a transaction each
        4. If any fails, rollback and raise (engine startup aborts)
        """
        # Ensure migrations table exists
        await self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
              version    INTEGER PRIMARY KEY,
              name       TEXT NOT NULL,
              applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )
            """
        )
        await self.conn.commit()

        # Find applied versions
        cursor = await self.conn.execute("SELECT version FROM schema_migrations ORDER BY version")
        applied = {row[0] for row in await cursor.fetchall()}

        # Discover migration files
        if migration_files is None:
            migration_files = sorted(_MIGRATIONS_DIR.glob("*.sql"))

        for mf in migration_files:
            version = _parse_migration_version(mf.name)
            if version is None or version in applied:
                continue

            sql_text = mf.read_text(encoding="utf-8")
            log.info("applying_migration", version=version, file=mf.name)
            try:
                await self.conn.executescript(sql_text)
                await self.conn.commit()
                log.info("migration_applied", version=version)
            except Exception as exc:
                log.error("migration_failed", version=version, error=str(exc))
                raise RuntimeError(f"Migration {mf.name} failed: {exc}") from exc


def _parse_migration_version(filename: str) -> int | None:
    """Extract leading integer version from migration filename like '0001_initial.sql'."""
    m = re.match(r"^(\d+)_", filename)
    if m:
        return int(m.group(1))
    return None


async def open_project_db(project_root: str | pathlib.Path) -> Database:
    """Open (and migrate) the per-project database."""
    root = pathlib.Path(project_root)
    db_path = root / ".knowler" / "project.db"
    # Only apply project-specific migrations (not the global one)
    project_migrations = sorted(
        f for f in _MIGRATIONS_DIR.glob("*.sql")
        if _parse_migration_version(f.name) not in {3}  # 3 = global only
    )
    db = Database(db_path)
    await db.open()
    await db.run_migrations(project_migrations)
    return db


async def open_global_db(app_support_dir: str | pathlib.Path) -> Database:
    """Open (and migrate) the global app database."""
    support = pathlib.Path(app_support_dir)
    db_path = support / "knowler.db"
    global_migrations = sorted(
        f for f in _MIGRATIONS_DIR.glob("*.sql")
        if _parse_migration_version(f.name) == 3  # 3 = global only
    )
    db = Database(db_path)
    await db.open()
    await db.run_migrations(global_migrations)
    return db
