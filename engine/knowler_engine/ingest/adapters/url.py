"""
URL / web article ingest adapter.

Fetches a URL, extracts the main article content using trafilatura,
and converts it to markdown. Stores raw text in raw/articles/.

Architecture: §45, §27.5
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import urllib.parse
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog
from ulid import ULID

log = structlog.get_logger(__name__)

# Try optional libraries — fail gracefully if not installed
try:
    import trafilatura
    HAS_TRAFILATURA = True
except ImportError:
    HAS_TRAFILATURA = False
    log.warning("trafilatura_not_installed", note="URL text extraction will be limited")

try:
    from markdownify import markdownify as md
    HAS_MARKDOWNIFY = True
except ImportError:
    HAS_MARKDOWNIFY = False


_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

_TIMEOUT = httpx.Timeout(30.0)


async def fetch_url(url: str, timeout_s: float = 30.0) -> tuple[bytes, str]:
    """
    Fetch a URL asynchronously.

    Returns (raw_bytes, content_type).
    Raises httpx.HTTPError on failures.
    """
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_s),
        follow_redirects=True,
        headers={"User-Agent": _USER_AGENT},
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "text/html")
        return resp.content, content_type


def extract_article(html_bytes: bytes, url: str) -> dict[str, Any]:
    """
    Extract article text + metadata from HTML.

    Returns dict with: title, text, author, date, language
    """
    html_str = html_bytes.decode("utf-8", errors="replace")

    if HAS_TRAFILATURA:
        # trafilatura is the primary extraction engine
        metadata = trafilatura.extract_metadata(html_str, default_url=url)
        text = trafilatura.extract(
            html_str,
            include_comments=False,
            include_tables=True,
            output_format="markdown",
            url=url,
        ) or ""

        return {
            "title": (metadata.title if metadata else None) or _extract_title_from_html(html_str),
            "text": text,
            "author": metadata.author if metadata else None,
            "date": metadata.date if metadata else None,
            "language": metadata.language if metadata else None,
            "description": metadata.description if metadata else None,
        }
    else:
        # Fallback: basic HTML → text via markdownify or strip tags
        title = _extract_title_from_html(html_str)
        if HAS_MARKDOWNIFY:
            text = md(html_str, strip=["script", "style"])
        else:
            import re
            text = re.sub(r"<[^>]+>", " ", html_str)
            text = re.sub(r"\s+", " ", text).strip()
        return {
            "title": title,
            "text": text[:50000],  # rough cap
            "author": None,
            "date": None,
            "language": None,
            "description": None,
        }


def _extract_title_from_html(html: str) -> str:
    """Extract <title> tag content."""
    import re
    m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def _extract_domain(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")
    except Exception:
        return ""


async def ingest_url(
    url: str,
    project_id: str,
    raw_articles_dir: pathlib.Path,
    trusted_domains: list[str] | None = None,
) -> dict[str, Any]:
    """
    Fetch and ingest a URL as a source record.

    Returns a source row dict ready for DB insertion.
    Raises httpx.HTTPError on fetch failures.
    """
    source_id = f"src_{ULID()}"
    domain = _extract_domain(url)
    trusted_domains = trusted_domains or []

    log.info("fetching_url", url=url[:200], source_id=source_id)
    raw_bytes, content_type = await fetch_url(url)

    if "pdf" in content_type.lower():
        # Redirect to PDF adapter
        raise ValueError(f"URL {url} returned PDF content — use ingest_pdf instead")

    article = extract_article(raw_bytes, url)
    title = article.get("title") or url[:200]
    text = article.get("text", "")

    # Write raw markdown to disk
    safe_title = "".join(c if c.isalnum() or c in " -_." else "_" for c in (title or domain))[:80]
    raw_filename = f"{source_id}_{safe_title}.md"
    raw_path = raw_articles_dir / raw_filename
    raw_articles_dir.mkdir(parents=True, exist_ok=True)

    # Build the stored markdown document
    frontmatter = "\n".join([
        "---",
        f"source_id: {source_id}",
        f"url: \"{url}\"",
        f"title: \"{title}\"",
        f"fetched_at: \"{datetime.now(tz=timezone.utc).isoformat()}\"",
        "---",
        "",
    ])
    doc_content = frontmatter + text
    raw_path.write_text(doc_content, encoding="utf-8")

    checksum = hashlib.sha256(raw_bytes).hexdigest()
    is_trusted = any(domain.endswith(td.lower()) for td in trusted_domains)

    return {
        "id": source_id,
        "project_id": project_id,
        "source_type": "url_article",
        "origin_type": "manual_url",
        "title": title[:1000] if title else None,
        "canonical_url": url,
        "display_url": url[:500],
        "domain": domain[:255],
        "raw_path": str(raw_path),
        "mime_type": content_type[:100],
        "checksum_sha256": checksum,
        "byte_size": len(raw_bytes),
        "trust_level": "medium" if is_trusted else "unknown",
        "status": "pending",
        "ingest_state": "approved" if is_trusted else "needs_review",
        "language_code": article.get("language"),
        "source_date": article.get("date"),
        "metadata_json": json.dumps(
            {
                "author": article.get("author"),
                "description": article.get("description"),
                "content_type": content_type,
            }
        ),
    }
