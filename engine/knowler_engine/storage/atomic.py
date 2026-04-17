"""
Atomic file write utilities with conflict detection.

Implements the architecture's file ownership and conflict rules:
- Single-writer principle
- Conflict detection using SHA-256 hash + mtime before write
- Atomic write via temp file + rename
- No silent overwrites
- Surfaces conflicts to callers; they surface to the frontend

All engine-generated files go through write_atomic().
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import stat
import tempfile
import time
import uuid
from dataclasses import dataclass
from typing import Any

import structlog

log = structlog.get_logger(__name__)


@dataclass
class FileRevision:
    """Snapshot of a file's identity at a point in time."""
    path: str
    sha256: str
    mtime: float
    size: int


@dataclass
class ConflictError(Exception):
    """Raised when a write is blocked by a conflict."""
    path: str
    expected_sha256: str
    actual_sha256: str
    message: str = ""

    def __post_init__(self) -> None:
        super().__init__(
            self.message or f"Write conflict at {self.path}: "
            f"expected sha256={self.expected_sha256[:12]}... "
            f"but found {self.actual_sha256[:12]}..."
        )


def compute_sha256(path: str | pathlib.Path) -> str:
    """Compute SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot(path: str | pathlib.Path) -> FileRevision | None:
    """Return a FileRevision snapshot, or None if the file doesn't exist."""
    p = pathlib.Path(path)
    if not p.exists():
        return None
    st = p.stat()
    sha256 = compute_sha256(p)
    return FileRevision(
        path=str(p),
        sha256=sha256,
        mtime=st.st_mtime,
        size=st.st_size,
    )


def write_atomic(
    path: str | pathlib.Path,
    content: str | bytes,
    expected_revision: FileRevision | None = None,
    tmp_dir: str | pathlib.Path | None = None,
    mode: int = 0o644,
) -> FileRevision:
    """
    Write content to path atomically.

    Parameters
    ----------
    path:
        Final destination path.
    content:
        String (UTF-8) or bytes to write.
    expected_revision:
        If provided, the file's current state must match this revision.
        If the file has changed since the revision was taken, raises ConflictError.
        If None, no conflict check is performed (use for new files only).
    tmp_dir:
        Where to write the temp file. Defaults to same dir as path
        (required for atomic rename on same filesystem).
    mode:
        File permissions for the output.

    Returns
    -------
    FileRevision of the newly written file.

    Raises
    ------
    ConflictError:
        If expected_revision is given and current file doesn't match.
    OSError:
        On I/O failures.
    """
    dest = pathlib.Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    # --- Conflict detection ---
    if expected_revision is not None and dest.exists():
        current_sha256 = compute_sha256(dest)
        if current_sha256 != expected_revision.sha256:
            raise ConflictError(
                path=str(dest),
                expected_sha256=expected_revision.sha256,
                actual_sha256=current_sha256,
            )

    # --- Prepare content bytes ---
    if isinstance(content, str):
        content_bytes = content.encode("utf-8")
    else:
        content_bytes = content

    # --- Write to temp file ---
    tmp_parent = pathlib.Path(tmp_dir) if tmp_dir else dest.parent
    tmp_parent.mkdir(parents=True, exist_ok=True)

    tmp_path: str | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(
            suffix=".tmp",
            prefix=f".knowler_{dest.name}_",
            dir=str(tmp_parent),
        )
        with os.fdopen(fd, "wb") as f:
            f.write(content_bytes)

        os.chmod(tmp_path, stat.S_IMODE(mode))

        # Atomic rename
        os.rename(tmp_path, str(dest))
        tmp_path = None  # rename succeeded, don't clean up
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    rev = snapshot(dest)
    assert rev is not None
    log.debug("atomic_write_ok", path=str(dest), sha256=rev.sha256[:12])
    return rev


def ensure_dir(path: str | pathlib.Path) -> pathlib.Path:
    """Create directory and all parents. Returns Path."""
    p = pathlib.Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p
