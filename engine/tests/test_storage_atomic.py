"""Tests for atomic write and conflict detection."""
from pathlib import Path

import pytest

from knowler_engine.storage.atomic import (
    compute_sha256,
    snapshot,
    write_atomic,
    ConflictError,
    FileRevision,
)


def test_write_atomic_creates_file(tmp_path: Path):
    target = tmp_path / "out.md"
    write_atomic(target, "hello world", expected_revision=None, tmp_dir=tmp_path)
    assert target.read_text() == "hello world"


def test_write_atomic_no_tmp_file_remains(tmp_path: Path):
    target = tmp_path / "out.md"
    write_atomic(target, "content", expected_revision=None, tmp_dir=tmp_path)
    tmp_files = list(tmp_path.glob("*.tmp"))
    assert tmp_files == [], f"Temp files remain: {tmp_files}"


def test_write_atomic_overwrite_with_matching_revision(tmp_path: Path):
    target = tmp_path / "page.md"
    target.write_text("v1")
    rev = snapshot(target)
    write_atomic(target, "v2", expected_revision=rev, tmp_dir=tmp_path)
    assert target.read_text() == "v2"


def test_write_atomic_raises_conflict_on_hash_mismatch(tmp_path: Path):
    target = tmp_path / "page.md"
    target.write_text("original")
    rev = snapshot(target)
    # Modify file between snapshot and write
    target.write_text("modified by someone else")
    with pytest.raises(ConflictError):
        write_atomic(target, "my new content", expected_revision=rev, tmp_dir=tmp_path)


def test_compute_sha256_consistent(tmp_path: Path):
    f = tmp_path / "data.txt"
    f.write_bytes(b"hello")
    h1 = compute_sha256(f)
    h2 = compute_sha256(f)
    assert h1 == h2
    assert len(h1) == 64  # hex sha256


def test_snapshot_returns_revision(tmp_path: Path):
    f = tmp_path / "data.txt"
    f.write_text("some content")
    rev = snapshot(f)
    assert isinstance(rev, FileRevision)
    assert rev.sha256 is not None
    assert rev.mtime > 0
    assert rev.size > 0


def test_snapshot_nonexistent_returns_none(tmp_path: Path):
    rev = snapshot(tmp_path / "nonexistent.md")
    assert rev is None


def test_write_atomic_new_file_with_none_revision(tmp_path: Path):
    target = tmp_path / "brand_new.md"
    write_atomic(target, "brand new", expected_revision=None, tmp_dir=tmp_path)
    assert target.exists()
    assert target.read_text() == "brand new"


def test_write_atomic_preserves_file_mode(tmp_path: Path):
    target = tmp_path / "script.md"
    write_atomic(target, "# doc", expected_revision=None, tmp_dir=tmp_path, mode=0o644)
    stat = target.stat()
    assert oct(stat.st_mode)[-3:] == "644"


def test_write_atomic_returns_file_revision(tmp_path: Path):
    target = tmp_path / "ret.md"
    rev = write_atomic(target, "content", expected_revision=None, tmp_dir=tmp_path)
    assert isinstance(rev, FileRevision)
    assert rev.sha256 is not None


def test_write_atomic_creates_parent_dirs(tmp_path: Path):
    target = tmp_path / "deep" / "nested" / "file.md"
    write_atomic(target, "nested content", expected_revision=None)
    assert target.exists()
    assert target.read_text() == "nested content"
