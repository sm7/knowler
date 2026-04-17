"""
PDF ingest adapter.

Extracts text and metadata from PDF files using pypdf.
Stores extracted text as markdown in raw/papers/.

Architecture: §11, §27.5
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import shutil
from datetime import datetime, timezone
from typing import Any

import structlog
from ulid import ULID

from knowler_engine.text_heuristics import choose_document_title

log = structlog.get_logger(__name__)

try:
    import pypdf
    HAS_PYPDF = True
except ImportError:
    HAS_PYPDF = False
    log.warning("pypdf_not_installed", note="PDF text extraction disabled")


def extract_pdf(pdf_path: str | pathlib.Path) -> dict[str, Any]:
    """
    Extract text and metadata from a PDF.

    Returns dict with: title, author, text, page_count, date, language
    """
    path = pathlib.Path(pdf_path)

    if not HAS_PYPDF:
        return {
            "title": path.stem,
            "author": None,
            "text": f"[PDF text extraction unavailable — pypdf not installed]\n\nFile: {path.name}",
            "page_count": 0,
            "date": None,
            "language": None,
        }

    with open(path, "rb") as f:
        reader = pypdf.PdfReader(f)
        meta = reader.metadata or {}
        page_count = len(reader.pages)

        # Extract text from each page
        pages: list[str] = []
        for i, page in enumerate(reader.pages):
            try:
                page_text = page.extract_text() or ""
                if page_text.strip():
                    pages.append(f"## Page {i + 1}\n\n{page_text.strip()}")
            except Exception as exc:
                log.warning("pdf_page_extract_failed", page=i, error=str(exc))

        full_text = "\n\n".join(pages)

        # Clean up metadata fields — don't fall back to path.stem here;
        # ingest_pdf will use the original source filename as fallback.
        title = _clean_meta(meta.get("/Title") or meta.get("Title"))
        author = _clean_meta(meta.get("/Author") or meta.get("Author"))
        date_raw = _clean_meta(meta.get("/CreationDate") or meta.get("CreationDate"))
        date_iso = _parse_pdf_date(date_raw) if date_raw else None

    return {
        "title": title,
        "author": author,
        "text": full_text,
        "page_count": page_count,
        "date": date_iso,
        "language": None,  # PDF doesn't reliably carry language metadata
    }


def _clean_meta(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


def _parse_pdf_date(date_str: str) -> str | None:
    """
    Parse PDF date format: D:YYYYMMDDHHmmSSOHH'mm'
    Returns ISO 8601 or None.
    """
    import re
    m = re.match(r"D:(\d{4})(\d{2})(\d{2})", date_str)
    if m:
        y, mo, d = m.group(1), m.group(2), m.group(3)
        return f"{y}-{mo}-{d}"
    return None


async def ingest_pdf(
    file_path: str | pathlib.Path,
    project_id: str,
    raw_papers_dir: pathlib.Path,
    copy_file: bool = True,
) -> dict[str, Any]:
    """
    Ingest a PDF file as a source record.

    Parameters
    ----------
    file_path: Path to the uploaded PDF
    project_id: Target project
    raw_papers_dir: Where to copy/store the raw PDF
    copy_file: If True, copy the file into raw_papers_dir; otherwise reference in-place.

    Returns source row dict for DB insertion.
    """
    src = pathlib.Path(file_path)
    if not src.exists():
        raise FileNotFoundError(f"PDF not found: {file_path}")

    source_id = f"src_{ULID()}"
    raw_papers_dir.mkdir(parents=True, exist_ok=True)

    # Copy raw PDF into vault
    dest_filename = f"{source_id}_{src.name}"
    dest_pdf = raw_papers_dir / dest_filename
    if copy_file:
        shutil.copy2(str(src), str(dest_pdf))
    else:
        dest_pdf = src

    # Compute checksum on original
    with open(src, "rb") as f:
        raw_bytes = f.read()
    checksum = hashlib.sha256(raw_bytes).hexdigest()

    # Extract text
    log.info("extracting_pdf", source_id=source_id, path=str(src))
    extracted = extract_pdf(dest_pdf)

    # Write extracted text as markdown alongside the PDF
    text_filename = f"{source_id}_text.md"
    text_path = raw_papers_dir / text_filename
    title = choose_document_title(extracted.get("title"), extracted.get("text", ""), fallback=src.stem)
    frontmatter = "\n".join([
        "---",
        f"source_id: {source_id}",
        f"title: \"{title}\"",
        f"author: \"{extracted.get('author') or ''}\"",
        f"page_count: {extracted.get('page_count', 0)}",
        f"source_date: \"{extracted.get('date') or ''}\"",
        f"extracted_at: \"{datetime.now(tz=timezone.utc).isoformat()}\"",
        "---",
        "",
    ])
    text_path.write_text(frontmatter + extracted["text"], encoding="utf-8")

    return {
        "id": source_id,
        "project_id": project_id,
        "source_type": "pdf",
        "origin_type": "file_upload",
        "title": title[:1000],
        "canonical_url": None,
        "display_url": src.name[:500],
        "domain": None,
        "raw_path": str(dest_pdf),
        "mime_type": "application/pdf",
        "checksum_sha256": checksum,
        "byte_size": len(raw_bytes),
        "trust_level": "medium",
        "status": "pending",
        "ingest_state": "approved",  # PDFs are manually uploaded — assume approved
        "language_code": extracted.get("language"),
        "source_date": extracted.get("date"),
        "metadata_json": json.dumps(
            {
                "author": extracted.get("author"),
                "page_count": extracted.get("page_count", 0),
                "extracted_text_path": str(text_path),
                "original_filename": src.name,
            }
        ),
    }
