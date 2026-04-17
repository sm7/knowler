"""
Vault filesystem manager.

Responsible for creating and maintaining the Obsidian-compatible project vault
directory structure. Enforces path ownership rules.

Engine-owned paths (written by the engine):
  index.md              project-wide page catalog
  log.md                project-wide chronological activity log
  graph.json            serialized knowledge graph export
  GRAPH_REPORT.md       human-readable graph summary
  wiki/concepts/      concept pages
  wiki/sources/       source summary pages
  wiki/comparisons/   comparison pages
  wiki/timelines/     timeline pages
  wiki/questions/     open question pages
  wiki/indexes/       index pages
  normalized/         intermediate JSON representations
  outputs/            generated artifacts
  maintenance/        maintenance reports

User-owned paths (never modified by engine):
  raw/                original imported material
  (anything the user creates outside engine-known directories)

The engine never writes to a user-owned path without explicit conflict detection.
"""
from __future__ import annotations

import pathlib
from dataclasses import dataclass
from typing import Literal


# All subdirectory names that the engine will create
_ENGINE_DIRS = [
    "raw/bookmark_candidates",
    "raw/articles",
    "raw/papers",
    "raw/repos",
    "raw/notes",
    "raw/images",
    "raw/datasets",
    "normalized/sources",
    "normalized/entities",
    "normalized/relations",
    "normalized/claims",
    "wiki/concepts",
    "wiki/sources",
    "wiki/comparisons",
    "wiki/timelines",
    "wiki/questions",
    "wiki/indexes",
    "outputs/answers",
    "outputs/reports",
    "outputs/slides",
    "outputs/diagrams",
    "maintenance/reports",
    "maintenance/patches",
    "cache",
    "logs",
    ".knowler/tmp",
    ".knowler/locks",
]


@dataclass
class Vault:
    """Represents the filesystem structure of one project."""
    root: pathlib.Path

    # Sub-paths
    @property
    def raw(self) -> pathlib.Path:
        return self.root / "raw"

    @property
    def normalized(self) -> pathlib.Path:
        return self.root / "normalized"

    @property
    def wiki(self) -> pathlib.Path:
        return self.root / "wiki"

    @property
    def outputs(self) -> pathlib.Path:
        return self.root / "outputs"

    @property
    def maintenance(self) -> pathlib.Path:
        return self.root / "maintenance"

    @property
    def cache(self) -> pathlib.Path:
        return self.root / "cache"

    @property
    def logs(self) -> pathlib.Path:
        return self.root / "logs"

    @property
    def dot_knowler(self) -> pathlib.Path:
        return self.root / ".knowler"

    @property
    def tmp_dir(self) -> pathlib.Path:
        return self.dot_knowler / "tmp"

    @property
    def db_path(self) -> pathlib.Path:
        return self.dot_knowler / "project.db"

    @property
    def config_path(self) -> pathlib.Path:
        return self.root / "config.yaml"

    def wiki_path(
        self,
        page_type: Literal["concept", "source_summary", "comparison", "timeline", "question", "index", "glossary"],
        slug: str,
    ) -> pathlib.Path:
        subdir_map = {
            "concept": "concepts",
            "source_summary": "sources",
            "comparison": "comparisons",
            "timeline": "timelines",
            "question": "questions",
            "index": "indexes",
            "glossary": "indexes",
        }
        subdir = subdir_map.get(page_type, "concepts")
        return self.wiki / subdir / f"{slug}.md"

    def output_path(
        self,
        artifact_type: str,
        filename: str,
    ) -> pathlib.Path:
        subdir_map = {
            "answer": "answers",
            "report": "reports",
            "slides": "slides",
            "diagram": "diagrams",
            "memo": "reports",
            "study_guide": "reports",
            "checklist": "reports",
        }
        subdir = subdir_map.get(artifact_type, "answers")
        return self.outputs / subdir / filename

    def normalized_source_path(self, source_id: str) -> pathlib.Path:
        return self.normalized / "sources" / f"{source_id}.json"

    def raw_path_for(self, source_type: str, filename: str) -> pathlib.Path:
        subdir_map = {
            "bookmark": "bookmark_candidates",
            "url_article": "articles",
            "pdf": "papers",
            "repo_doc": "repos",
            "markdown_note": "notes",
            "image": "images",
            "dataset": "datasets",
        }
        subdir = subdir_map.get(source_type, "articles")
        return self.raw / subdir / filename

    def initialize(self) -> None:
        """Create the full directory structure for a new project."""
        for rel in _ENGINE_DIRS:
            (self.root / rel).mkdir(parents=True, exist_ok=True)

    def is_engine_owned(self, path: str | pathlib.Path) -> bool:
        """Return True if this path is in an engine-owned directory."""
        p = pathlib.Path(path).resolve()
        if p == (self.root / "index.md").resolve():
            return True
        if p == (self.root / "log.md").resolve():
            return True
        if p == (self.root / "graph.json").resolve():
            return True
        if p == (self.root / "GRAPH_REPORT.md").resolve():
            return True
        engine_roots = [
            (self.root / rel.split("/")[0]).resolve()
            for rel in ["normalized", "wiki", "outputs", "maintenance", "cache", ".knowler"]
        ]
        return any(str(p).startswith(str(r)) for r in engine_roots)


def create_vault(root: str | pathlib.Path) -> Vault:
    """Create and initialize a new vault at the given root path."""
    vault = Vault(root=pathlib.Path(root))
    vault.initialize()
    return vault


def open_vault(root: str | pathlib.Path) -> Vault:
    """Open an existing vault without re-initializing."""
    return Vault(root=pathlib.Path(root))
