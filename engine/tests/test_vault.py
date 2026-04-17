"""Tests for Vault path management and initialization."""
import pytest
from pathlib import Path

from knowler_engine.storage.vault import Vault, create_vault, open_vault


def test_vault_initialize_creates_dirs(tmp_path: Path):
    vault = Vault(root=tmp_path / "my_vault")
    vault.initialize()
    assert vault.wiki.exists()
    assert vault.raw.exists()
    assert vault.normalized.exists()
    assert vault.outputs.exists()
    assert vault.maintenance.exists()
    assert vault.dot_knowler.exists()


def test_vault_initialize_idempotent(tmp_path: Path):
    vault = Vault(root=tmp_path / "my_vault")
    vault.initialize()
    vault.initialize()  # should not raise


def test_create_vault_helper(tmp_path: Path):
    vault = create_vault(tmp_path / "created")
    assert vault.wiki.exists()
    assert vault.dot_knowler.exists()


def test_open_vault_helper(tmp_path: Path):
    root = tmp_path / "existing"
    root.mkdir()
    vault = open_vault(root)
    assert isinstance(vault, Vault)
    assert vault.root == root


def test_vault_db_path(tmp_path: Path):
    vault = Vault(root=tmp_path / "vault")
    assert vault.db_path == tmp_path / "vault" / ".knowler" / "project.db"


def test_vault_tmp_dir(tmp_path: Path):
    vault = Vault(root=tmp_path / "vault")
    assert vault.tmp_dir == vault.dot_knowler / "tmp"


def test_vault_wiki_path_concept(tmp_path: Path):
    vault = Vault(root=tmp_path / "vault")
    p = vault.wiki_path("concept", "machine-learning")
    assert p.suffix == ".md"
    assert "machine-learning" in p.name
    assert "concepts" in str(p)


def test_vault_wiki_path_source_summary(tmp_path: Path):
    vault = Vault(root=tmp_path / "vault")
    p = vault.wiki_path("source_summary", "my-source")
    assert "sources" in str(p)


def test_vault_output_path_report(tmp_path: Path):
    vault = Vault(root=tmp_path / "vault")
    p = vault.output_path("report", "summary.md")
    assert p.parent == vault.outputs / "reports"
    assert p.name == "summary.md"


def test_vault_output_path_answer(tmp_path: Path):
    vault = Vault(root=tmp_path / "vault")
    p = vault.output_path("answer", "result.md")
    assert p.parent == vault.outputs / "answers"


def test_vault_normalized_source_path(tmp_path: Path):
    vault = Vault(root=tmp_path / "vault")
    p = vault.normalized_source_path("src-123")
    assert p == vault.normalized / "sources" / "src-123.json"


def test_vault_raw_path_for_article(tmp_path: Path):
    vault = Vault(root=tmp_path / "vault")
    p = vault.raw_path_for("url_article", "article.md")
    assert p.parent == vault.raw / "articles"


def test_vault_raw_path_for_pdf(tmp_path: Path):
    vault = Vault(root=tmp_path / "vault")
    p = vault.raw_path_for("pdf", "paper.pdf")
    assert p.parent == vault.raw / "papers"


def test_vault_is_engine_owned_wiki(tmp_path: Path):
    vault = Vault(root=tmp_path / "vault")
    p = vault.wiki / "concepts" / "foo.md"
    assert vault.is_engine_owned(p)


def test_vault_is_engine_owned_normalized(tmp_path: Path):
    vault = Vault(root=tmp_path / "vault")
    p = vault.normalized / "sources" / "bar.json"
    assert vault.is_engine_owned(p)


def test_vault_is_engine_owned_outputs(tmp_path: Path):
    vault = Vault(root=tmp_path / "vault")
    p = vault.outputs / "reports" / "report.md"
    assert vault.is_engine_owned(p)


def test_vault_is_engine_owned_dot_knowler(tmp_path: Path):
    vault = Vault(root=tmp_path / "vault")
    p = vault.dot_knowler / "project.db"
    assert vault.is_engine_owned(p)


def test_vault_is_engine_owned_root_index(tmp_path: Path):
    vault = Vault(root=tmp_path / "vault")
    p = tmp_path / "vault" / "index.md"
    assert vault.is_engine_owned(p)


def test_vault_is_engine_owned_root_log(tmp_path: Path):
    vault = Vault(root=tmp_path / "vault")
    p = tmp_path / "vault" / "log.md"
    assert vault.is_engine_owned(p)


def test_vault_is_engine_owned_graph_exports(tmp_path: Path):
    vault = Vault(root=tmp_path / "vault")
    assert vault.is_engine_owned(tmp_path / "vault" / "graph.json")
    assert vault.is_engine_owned(tmp_path / "vault" / "GRAPH_REPORT.md")


def test_vault_is_not_engine_owned_user_file(tmp_path: Path):
    vault = Vault(root=tmp_path / "vault")
    # raw/ is NOT engine-owned (user puts original imports there)
    p = vault.raw / "articles" / "bar.md"
    assert not vault.is_engine_owned(p)


def test_vault_is_not_engine_owned_top_level(tmp_path: Path):
    vault = Vault(root=tmp_path / "vault")
    p = tmp_path / "vault" / "my_notes.md"
    assert not vault.is_engine_owned(p)
