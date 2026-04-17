"""Tests for ingest adapters: bookmark parser."""
import json
import plistlib
import pytest
from pathlib import Path
from unittest.mock import patch

from knowler_engine.ingest.adapters.bookmark import (
    parse_bookmark_html,
    parse_chromium_bookmarks_json,
    parse_safari_bookmarks_plist,
    should_auto_promote,
    build_source_rows,
    list_browser_bookmark_sources,
    load_browser_bookmarks,
)
from knowler_engine.ingest.adapters.pdf import ingest_pdf


SAMPLE_BOOKMARKS_HTML = """<!DOCTYPE NETSCAPE-Bookmark-file-1>
<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">
<TITLE>Bookmarks</TITLE>
<H1>Bookmarks</H1>
<DL><p>
    <DT><H3>Research</H3>
    <DL><p>
        <DT><A HREF="https://arxiv.org/abs/2401.00001" ADD_DATE="1700000000">Attention Is All You Need</A>
        <DT><A HREF="https://example.com/blog" ADD_DATE="1700000001">Some Blog Post</A>
    </DL><p>
    <DT><H3>Tools</H3>
    <DL><p>
        <DT><A HREF="https://github.com/org/repo" ADD_DATE="1700000002">A GitHub Repo</A>
    </DL><p>
</DL><p>
"""

SAMPLE_CHROMIUM_BOOKMARKS = {
    "roots": {
        "bookmark_bar": {
            "children": [
                {
                    "type": "folder",
                    "name": "Research",
                    "children": [
                        {
                            "type": "url",
                            "name": "Attention Is All You Need",
                            "url": "https://arxiv.org/abs/1706.03762",
                            "date_added": "13348540800000000",
                        }
                    ],
                }
            ]
        },
        "other": {
            "children": [
                {
                    "type": "url",
                    "name": "Example",
                    "url": "https://example.com/blog",
                    "date_added": "13348540800000001",
                }
            ]
        },
        "synced": {"children": []},
    }
}


@pytest.fixture
def bookmarks_file(tmp_path: Path) -> Path:
    f = tmp_path / "bookmarks.html"
    f.write_text(SAMPLE_BOOKMARKS_HTML, encoding="utf-8")
    return f


@pytest.fixture
def chromium_bookmarks_file(tmp_path: Path) -> Path:
    f = tmp_path / "Bookmarks"
    f.write_text(json.dumps(SAMPLE_CHROMIUM_BOOKMARKS), encoding="utf-8")
    return f


def test_parse_bookmark_html_returns_entries(bookmarks_file: Path):
    entries = parse_bookmark_html(bookmarks_file)
    assert len(entries) >= 3
    urls = [e["url"] for e in entries]
    assert "https://arxiv.org/abs/2401.00001" in urls
    assert "https://example.com/blog" in urls
    assert "https://github.com/org/repo" in urls


def test_parse_bookmark_html_captures_folder(bookmarks_file: Path):
    entries = parse_bookmark_html(bookmarks_file)
    by_url = {e["url"]: e for e in entries}
    assert "Research" in by_url["https://arxiv.org/abs/2401.00001"]["folder_path"]
    assert "Tools" in by_url["https://github.com/org/repo"]["folder_path"]


def test_parse_bookmark_html_captures_title(bookmarks_file: Path):
    entries = parse_bookmark_html(bookmarks_file)
    by_url = {e["url"]: e for e in entries}
    assert by_url["https://arxiv.org/abs/2401.00001"]["title"] == "Attention Is All You Need"


def test_parse_bookmark_html_captures_domain(bookmarks_file: Path):
    entries = parse_bookmark_html(bookmarks_file)
    by_url = {e["url"]: e for e in entries}
    assert by_url["https://arxiv.org/abs/2401.00001"]["domain"] == "arxiv.org"


def test_should_auto_promote_trusted_domain():
    entry = {"url": "https://arxiv.org/abs/2401.00001", "folder_path": "Random", "domain": "arxiv.org"}
    result = should_auto_promote(entry, trusted_domains=["arxiv.org"], trusted_folders=[])
    assert result is True


def test_should_auto_promote_trusted_folder():
    entry = {"url": "https://example.com/foo", "folder_path": "Research", "domain": "example.com"}
    result = should_auto_promote(entry, trusted_domains=[], trusted_folders=["Research"])
    assert result is True


def test_should_auto_promote_neither():
    entry = {"url": "https://random-site.com/page", "folder_path": "Misc", "domain": "random-site.com"}
    result = should_auto_promote(entry, trusted_domains=["arxiv.org"], trusted_folders=["Research"])
    assert result is False


def test_build_source_rows_deduplicates_urls(tmp_path: Path):
    entries = [
        {"url": "https://arxiv.org/abs/1", "title": "Paper A", "folder_path": "Research", "domain": "arxiv.org"},
        {"url": "https://arxiv.org/abs/1", "title": "Paper A Duplicate", "folder_path": "Research", "domain": "arxiv.org"},
        {"url": "https://arxiv.org/abs/2", "title": "Paper B", "folder_path": "Research", "domain": "arxiv.org"},
    ]
    rows = build_source_rows(
        entries, project_id="proj-1",
        trusted_domains=[], trusted_folders=[],
        raw_dir=tmp_path,
    )
    urls = [r["canonical_url"] for r in rows]
    assert len(urls) == len(set(urls)), "Duplicate URLs not deduplicated"
    assert len(rows) == 2


def test_build_source_rows_assigns_ids(tmp_path: Path):
    entries = [
        {"url": "https://arxiv.org/abs/1", "title": "Paper", "folder_path": "Research", "domain": "arxiv.org"},
    ]
    rows = build_source_rows(
        entries, project_id="proj-1",
        trusted_domains=[], trusted_folders=[],
        raw_dir=tmp_path,
    )
    assert rows[0]["id"] is not None
    assert rows[0]["id"].startswith("src_")


def test_build_source_rows_writes_stub_files(tmp_path: Path):
    entries = [
        {"url": "https://arxiv.org/abs/1", "title": "Paper", "folder_path": "Research", "domain": "arxiv.org"},
    ]
    rows = build_source_rows(
        entries, project_id="proj-1",
        trusted_domains=[], trusted_folders=[],
        raw_dir=tmp_path,
    )
    stub_path = Path(rows[0]["raw_path"])
    assert stub_path.exists()
    data = json.loads(stub_path.read_text())
    assert data["url"] == "https://arxiv.org/abs/1"


def test_build_source_rows_auto_promotes_trusted_domain(tmp_path: Path):
    entries = [
        {"url": "https://arxiv.org/abs/1", "title": "Paper", "folder_path": "Random", "domain": "arxiv.org"},
    ]
    rows = build_source_rows(
        entries, project_id="proj-1",
        trusted_domains=["arxiv.org"], trusted_folders=[],
        raw_dir=tmp_path,
    )
    assert rows[0]["ingest_state"] == "approved"
    assert rows[0]["trust_level"] == "medium"


def test_build_source_rows_leaves_unknown_as_needs_review(tmp_path: Path):
    entries = [
        {"url": "https://random.com/page", "title": "Random", "folder_path": "Misc", "domain": "random.com"},
    ]
    rows = build_source_rows(
        entries, project_id="proj-1",
        trusted_domains=[], trusted_folders=[],
        raw_dir=tmp_path,
    )
    assert rows[0]["ingest_state"] == "needs_review"


def test_parse_bookmark_html_empty_html(tmp_path: Path):
    f = tmp_path / "empty.html"
    f.write_text("<DL></DL>", encoding="utf-8")
    entries = parse_bookmark_html(f)
    assert entries == []


def test_parse_bookmark_html_skips_javascript_urls(tmp_path: Path):
    html = """<DL><DT><A HREF="javascript:void(0)">JS Link</A></DL>"""
    f = tmp_path / "js.html"
    f.write_text(html, encoding="utf-8")
    entries = parse_bookmark_html(f)
    # javascript: urls may be returned from parser but filtered by build_source_rows
    rows = build_source_rows(entries, project_id="p1", trusted_domains=[], trusted_folders=[], raw_dir=tmp_path)
    assert not any(r["canonical_url"].startswith("javascript:") for r in rows)


def test_parse_chromium_bookmarks_json_returns_entries(chromium_bookmarks_file: Path):
    entries = parse_chromium_bookmarks_json(chromium_bookmarks_file)
    by_url = {entry["url"]: entry for entry in entries}

    assert "https://arxiv.org/abs/1706.03762" in by_url
    assert by_url["https://arxiv.org/abs/1706.03762"]["folder_path"] == "Bookmarks Bar / Research"
    assert by_url["https://example.com/blog"]["folder_path"] == "Other Bookmarks"


def test_parse_safari_bookmarks_plist_returns_entries(tmp_path: Path):
    plist_path = tmp_path / "Bookmarks.plist"
    with plist_path.open("wb") as fh:
        plistlib.dump(
            {
                "Children": [
                    {
                        "Title": "Favorites",
                        "WebBookmarkType": "WebBookmarkTypeList",
                        "Children": [
                            {
                                "WebBookmarkType": "WebBookmarkTypeLeaf",
                                "URIDictionary": {"title": "OpenAI"},
                                "URLString": "https://openai.com",
                            }
                        ],
                    }
                ]
            },
            fh,
        )

    entries = parse_safari_bookmarks_plist(plist_path)

    assert entries == [
        {
            "title": "OpenAI",
            "url": "https://openai.com",
            "folder_path": "Favorites",
            "added_at": None,
            "domain": "openai.com",
        }
    ]


def test_list_browser_bookmark_sources_detects_default_chrome(tmp_path: Path):
    home = tmp_path / "home"
    user_data = home / "Library/Application Support/Google/Chrome"
    default_profile = user_data / "Default"
    default_profile.mkdir(parents=True)
    (default_profile / "Bookmarks").write_text(json.dumps(SAMPLE_CHROMIUM_BOOKMARKS), encoding="utf-8")
    (user_data / "Local State").write_text(json.dumps({"profile": {"last_used": "Default"}}), encoding="utf-8")

    sources = list_browser_bookmark_sources(
        home_dir=home,
        system_name="Darwin",
        default_browser_bundle_id="com.google.chrome",
    )

    assert sources[0]["browser_id"] == "chrome"
    assert sources[0]["is_default"] is True
    assert sources[0]["profile"] == "Default"


def test_load_browser_bookmarks_reads_local_chrome_profile(tmp_path: Path):
    home = tmp_path / "home"
    default_profile = home / "Library/Application Support/Google/Chrome/Default"
    default_profile.mkdir(parents=True)
    (default_profile / "Bookmarks").write_text(json.dumps(SAMPLE_CHROMIUM_BOOKMARKS), encoding="utf-8")

    source, entries = load_browser_bookmarks("chrome", home_dir=home, system_name="Darwin")

    assert source["browser_id"] == "chrome"
    assert len(entries) == 2


def test_build_source_rows_stores_browser_metadata(tmp_path: Path):
    entries = [
        {"url": "https://arxiv.org/abs/1", "title": "Paper", "folder_path": "Research", "domain": "arxiv.org"},
    ]
    rows = build_source_rows(
        entries,
        project_id="proj-1",
        trusted_domains=[],
        trusted_folders=[],
        raw_dir=tmp_path,
        browser_source="chrome",
        browser_profile="Default",
    )
    metadata = json.loads(rows[0]["metadata_json"])
    assert metadata["browser_source"] == "chrome"
    assert metadata["browser_profile"] == "Default"


@pytest.mark.asyncio
async def test_ingest_pdf_infers_title_from_first_page_text(tmp_path: Path):
    pdf_path = tmp_path / "1810.04805v2.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake pdf")
    raw_dir = tmp_path / "raw" / "papers"

    extracted = {
        "title": None,
        "author": None,
        "text": (
            "## Page 1\n\n"
            "BERT: Pre-training of Deep Bidirectional Transformers for\n"
            "Language Understanding\n"
            "Jacob Devlin Ming-Wei Chang Kenton Lee Kristina Toutanova\n"
            "Google AI Language\n"
            "{jacobdevlin,mingweichang,kentonl,kristout}@google.com\n"
            "Abstract\n"
            "We introduce a new language representation model called BERT.\n"
        ),
        "page_count": 16,
        "date": "2019-05-24",
        "language": None,
    }

    with patch("knowler_engine.ingest.adapters.pdf.extract_pdf", return_value=extracted):
        row = await ingest_pdf(pdf_path, "proj_1", raw_dir)

    assert row["title"] == "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding"
