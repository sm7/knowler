"""
Bookmark ingest adapter.

Parses browser-exported bookmark HTML files (Chrome, Safari, Firefox).
Creates source rows with source_type='bookmark' and ingest_state='needs_review'
unless trusted domain/folder rules auto-promote the bookmark.

Architecture: §45.2 — Bookmark ingest
"""
from __future__ import annotations

import configparser
import hashlib
import json
import os
import pathlib
import platform
import plistlib
import re
import shutil
import sqlite3
import subprocess
import tempfile
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from bs4 import BeautifulSoup
from ulid import ULID

log = structlog.get_logger(__name__)


_CHROMIUM_BROWSER_LABELS = {
    "chrome": "Google Chrome",
    "chromium": "Chromium",
    "brave": "Brave",
    "edge": "Microsoft Edge",
    "arc": "Arc",
}

_CHROMIUM_BROWSER_ROOTS = {
    "Darwin": {
        "chrome": pathlib.Path("Library/Application Support/Google/Chrome"),
        "chromium": pathlib.Path("Library/Application Support/Chromium"),
        "brave": pathlib.Path("Library/Application Support/BraveSoftware/Brave-Browser"),
        "edge": pathlib.Path("Library/Application Support/Microsoft Edge"),
        "arc": pathlib.Path("Library/Application Support/Arc/User Data"),
    },
    "Linux": {
        "chrome": pathlib.Path(".config/google-chrome"),
        "chromium": pathlib.Path(".config/chromium"),
        "brave": pathlib.Path(".config/BraveSoftware/Brave-Browser"),
        "edge": pathlib.Path(".config/microsoft-edge"),
        "arc": pathlib.Path(".config/Arc/User Data"),
    },
    "Windows": {
        "chrome": pathlib.Path("AppData/Local/Google/Chrome/User Data"),
        "chromium": pathlib.Path("AppData/Local/Chromium/User Data"),
        "brave": pathlib.Path("AppData/Local/BraveSoftware/Brave-Browser/User Data"),
        "edge": pathlib.Path("AppData/Local/Microsoft/Edge/User Data"),
        "arc": pathlib.Path("AppData/Local/Arc/User Data"),
    },
}

_BROWSER_BUNDLE_IDS = {
    "com.google.chrome": "chrome",
    "org.chromium.Chromium": "chromium",
    "com.brave.Browser": "brave",
    "com.microsoft.edgemac": "edge",
    "company.thebrowser.Browser": "arc",
    "com.apple.Safari": "safari",
    "org.mozilla.firefox": "firefox",
}

_BROWSER_LABELS = {
    **_CHROMIUM_BROWSER_LABELS,
    "safari": "Safari",
    "firefox": "Firefox",
}

_FIREFOX_PROFILE_ROOTS = {
    "Darwin": pathlib.Path("Library/Application Support/Firefox"),
    "Linux": pathlib.Path(".mozilla/firefox"),
    "Windows": pathlib.Path("AppData/Roaming/Mozilla/Firefox"),
}


def _extract_domain(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")
    except Exception:
        return ""


def _timestamp_to_iso(ts_str: str) -> str | None:
    """Convert a bookmark ADD_DATE (Unix timestamp) to ISO 8601."""
    try:
        ts = int(ts_str)
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (ValueError, TypeError):
        return None


def parse_bookmark_html(html_path: str | pathlib.Path) -> list[dict[str, Any]]:
    """
    Parse a browser-exported bookmarks HTML file.

    Returns a list of bookmark dicts with fields:
      title, url, folder_path, added_at, domain, raw_html_snippet
    """
    path = pathlib.Path(html_path)
    html = path.read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")
    bookmarks: list[dict[str, Any]] = []
    # Start from the first DL element to avoid html/body wrapping
    root = soup.find("dl") or soup
    _walk_folder(root, [], bookmarks)
    log.info("parsed_bookmarks", count=len(bookmarks), source=str(path))
    return bookmarks


def _home_dir(home_dir: pathlib.Path | str | None = None) -> pathlib.Path:
    return pathlib.Path(home_dir).expanduser() if home_dir is not None else pathlib.Path.home()


def _platform_name(system_name: str | None = None) -> str:
    return system_name or platform.system()


def _chromium_timestamp_to_iso(value: str | int | None) -> str | None:
    try:
        micros = int(value or 0)
    except (TypeError, ValueError):
        return None
    if micros <= 0:
        return None
    chrome_epoch = datetime(1601, 1, 1, tzinfo=timezone.utc)
    return (chrome_epoch + timedelta(microseconds=micros)).isoformat()


def _firefox_timestamp_to_iso(value: int | str | None) -> str | None:
    try:
        micros = int(value or 0)
    except (TypeError, ValueError):
        return None
    if micros <= 0:
        return None
    return datetime.fromtimestamp(micros / 1_000_000, tz=timezone.utc).isoformat()


def _safe_label(title: str | None) -> str | None:
    text = (title or "").strip()
    if not text:
        return None
    if text.startswith("com.apple."):
        return None
    return text


def _mac_launch_services_candidates(home: pathlib.Path) -> list[pathlib.Path]:
    return [
        home / "Library/Preferences/com.apple.LaunchServices/com.apple.launchservices.secure.plist",
        home / "Library/Preferences/com.apple.LaunchServices.plist",
    ]


def detect_default_browser_bundle_id(
    home_dir: pathlib.Path | str | None = None,
    system_name: str | None = None,
) -> str | None:
    system = _platform_name(system_name)
    if system != "Darwin":
        return None

    home = _home_dir(home_dir)
    for plist_path in _mac_launch_services_candidates(home):
        if not plist_path.exists():
            continue
        try:
            with plist_path.open("rb") as fh:
                data = plistlib.load(fh)
        except Exception:
            continue

        for handler in data.get("LSHandlers", []):
            if handler.get("LSHandlerURLScheme") not in {"http", "https"}:
                continue
            bundle_id = handler.get("LSHandlerRoleAll") or handler.get("LSHandlerRoleViewer")
            if bundle_id:
                return str(bundle_id)

    try:
        proc = subprocess.run(
            [
                "osascript",
                "-e",
                'id of application (path to default application for URL "https://example.com")',
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except Exception:
        return None

    bundle_id = (proc.stdout or "").strip()
    return bundle_id or None


def _chromium_user_data_root(
    browser_id: str,
    home_dir: pathlib.Path | str | None = None,
    system_name: str | None = None,
) -> pathlib.Path | None:
    system = _platform_name(system_name)
    rel_path = _CHROMIUM_BROWSER_ROOTS.get(system, {}).get(browser_id)
    if rel_path is None:
        return None
    return _home_dir(home_dir) / rel_path


def _read_chromium_last_used_profile(user_data_root: pathlib.Path) -> str | None:
    local_state = user_data_root / "Local State"
    if not local_state.exists():
        return None
    try:
        data = json.loads(local_state.read_text(encoding="utf-8"))
    except Exception:
        return None
    profile_name = data.get("profile", {}).get("last_used")
    if isinstance(profile_name, str) and profile_name.strip():
        return profile_name.strip()
    return None


def _resolve_chromium_source(
    browser_id: str,
    profile: str | None = None,
    home_dir: pathlib.Path | str | None = None,
    system_name: str | None = None,
) -> dict[str, Any] | None:
    user_data_root = _chromium_user_data_root(browser_id, home_dir=home_dir, system_name=system_name)
    if user_data_root is None or not user_data_root.exists():
        return None

    candidate_profiles: list[str] = []
    if profile:
        candidate_profiles.append(profile)
    else:
        last_used = _read_chromium_last_used_profile(user_data_root)
        if last_used:
            candidate_profiles.append(last_used)
        candidate_profiles.append("Default")
        for child in sorted(user_data_root.iterdir()):
            if child.is_dir() and (child.name == "Default" or child.name.startswith("Profile ")):
                candidate_profiles.append(child.name)

    seen: set[str] = set()
    for profile_name in candidate_profiles:
        if profile_name in seen:
            continue
        seen.add(profile_name)
        bookmark_path = user_data_root / profile_name / "Bookmarks"
        if bookmark_path.is_file():
            return {
                "browser_id": browser_id,
                "browser_name": _BROWSER_LABELS[browser_id],
                "profile": profile_name,
                "label": _BROWSER_LABELS[browser_id],
                "detail": f"{_BROWSER_LABELS[browser_id]} · {profile_name}",
                "bookmark_path": bookmark_path,
            }
    return None


def _firefox_root(
    home_dir: pathlib.Path | str | None = None,
    system_name: str | None = None,
) -> pathlib.Path | None:
    system = _platform_name(system_name)
    rel_path = _FIREFOX_PROFILE_ROOTS.get(system)
    if rel_path is None:
        return None
    if system == "Windows" and os.getenv("APPDATA"):
        return pathlib.Path(os.getenv("APPDATA", "")) / "Mozilla/Firefox"
    return _home_dir(home_dir) / rel_path


def _read_firefox_profile_paths(firefox_root: pathlib.Path) -> list[str]:
    profiles_ini = firefox_root / "profiles.ini"
    if not profiles_ini.exists():
        return []

    config = configparser.ConfigParser()
    config.read(profiles_ini)

    preferred: list[str] = []
    fallback: list[str] = []
    for section in config.sections():
        if not section.startswith("Profile"):
            continue
        raw_path = config.get(section, "Path", fallback="").strip()
        if not raw_path:
            continue
        is_relative = config.getboolean(section, "IsRelative", fallback=True)
        path_value = raw_path if is_relative else str(pathlib.Path(raw_path))
        if config.getboolean(section, "Default", fallback=False):
            preferred.append(path_value)
        else:
            fallback.append(path_value)
    return preferred + fallback


def _resolve_firefox_source(
    profile: str | None = None,
    home_dir: pathlib.Path | str | None = None,
    system_name: str | None = None,
) -> dict[str, Any] | None:
    firefox_root = _firefox_root(home_dir=home_dir, system_name=system_name)
    if firefox_root is None or not firefox_root.exists():
        return None

    candidates: list[pathlib.Path] = []
    if profile:
        candidates.append((firefox_root / profile).resolve())
    else:
        for profile_path in _read_firefox_profile_paths(firefox_root):
            path = pathlib.Path(profile_path)
            candidates.append((firefox_root / path).resolve() if not path.is_absolute() else path.resolve())
        for child in sorted((firefox_root / "Profiles").glob("*")) if (firefox_root / "Profiles").exists() else []:
            candidates.append(child.resolve())

    seen: set[pathlib.Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        bookmark_path = candidate / "places.sqlite"
        if bookmark_path.is_file():
            profile_name = candidate.name
            return {
                "browser_id": "firefox",
                "browser_name": _BROWSER_LABELS["firefox"],
                "profile": profile_name,
                "label": _BROWSER_LABELS["firefox"],
                "detail": f"{_BROWSER_LABELS['firefox']} · {profile_name}",
                "bookmark_path": bookmark_path,
            }
    return None


def _resolve_safari_source(
    home_dir: pathlib.Path | str | None = None,
    system_name: str | None = None,
) -> dict[str, Any] | None:
    if _platform_name(system_name) != "Darwin":
        return None
    bookmark_path = _home_dir(home_dir) / "Library" / "Safari" / "Bookmarks.plist"
    if not bookmark_path.is_file():
        return None
    return {
        "browser_id": "safari",
        "browser_name": _BROWSER_LABELS["safari"],
        "profile": None,
        "label": _BROWSER_LABELS["safari"],
        "detail": _BROWSER_LABELS["safari"],
        "bookmark_path": bookmark_path,
    }


def resolve_browser_bookmark_source(
    browser_id: str,
    profile: str | None = None,
    home_dir: pathlib.Path | str | None = None,
    system_name: str | None = None,
) -> dict[str, Any] | None:
    if browser_id in _CHROMIUM_BROWSER_LABELS:
        return _resolve_chromium_source(
            browser_id,
            profile=profile,
            home_dir=home_dir,
            system_name=system_name,
        )
    if browser_id == "safari":
        return _resolve_safari_source(home_dir=home_dir, system_name=system_name)
    if browser_id == "firefox":
        return _resolve_firefox_source(
            profile=profile,
            home_dir=home_dir,
            system_name=system_name,
        )
    return None


def list_browser_bookmark_sources(
    home_dir: pathlib.Path | str | None = None,
    system_name: str | None = None,
    default_browser_bundle_id: str | None = None,
) -> list[dict[str, Any]]:
    system = _platform_name(system_name)
    resolved_default_bundle_id = (
        default_browser_bundle_id
        if default_browser_bundle_id is not None
        else detect_default_browser_bundle_id(home_dir=home_dir, system_name=system)
    )
    default_browser_id = _BROWSER_BUNDLE_IDS.get(resolved_default_bundle_id or "")

    ordered_browser_ids = ["chrome", "safari", "firefox", "brave", "edge", "arc", "chromium"]
    sources: list[dict[str, Any]] = []
    for browser_id in ordered_browser_ids:
        source = resolve_browser_bookmark_source(
            browser_id,
            home_dir=home_dir,
            system_name=system,
        )
        if source is None:
            continue
        source["is_default"] = browser_id == default_browser_id
        sources.append(source)

    sources.sort(key=lambda item: (0 if item.get("is_default") else 1, item["label"]))
    return sources


def parse_chromium_bookmarks_json(bookmark_path: str | pathlib.Path) -> list[dict[str, Any]]:
    path = pathlib.Path(bookmark_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    bookmarks: list[dict[str, Any]] = []
    roots = data.get("roots", {})

    for key, display_name in (
        ("bookmark_bar", "Bookmarks Bar"),
        ("other", "Other Bookmarks"),
        ("synced", "Mobile Bookmarks"),
    ):
        root = roots.get(key)
        if not isinstance(root, dict):
            continue
        for child in root.get("children", []) or []:
            _walk_chromium_node(child, [display_name], bookmarks)

    log.info("parsed_chromium_bookmarks", count=len(bookmarks), source=str(path))
    return bookmarks


def _walk_chromium_node(
    node: dict[str, Any],
    folder_stack: list[str],
    results: list[dict[str, Any]],
) -> None:
    node_type = node.get("type")
    if node_type == "url":
        url = str(node.get("url", "")).strip()
        if not url:
            return
        title = str(node.get("name", "")).strip() or url
        results.append(
            {
                "title": title,
                "url": url,
                "folder_path": " / ".join(folder_stack),
                "added_at": _chromium_timestamp_to_iso(node.get("date_added")),
                "domain": _extract_domain(url),
            }
        )
        return

    if node_type != "folder":
        return

    folder_name = str(node.get("name", "")).strip()
    next_stack = folder_stack + [folder_name] if folder_name else folder_stack
    for child in node.get("children", []) or []:
        if isinstance(child, dict):
            _walk_chromium_node(child, next_stack, results)


def parse_safari_bookmarks_plist(bookmark_path: str | pathlib.Path) -> list[dict[str, Any]]:
    path = pathlib.Path(bookmark_path)
    with path.open("rb") as fh:
        data = plistlib.load(fh)

    bookmarks: list[dict[str, Any]] = []
    _walk_safari_node(data, [], bookmarks)
    log.info("parsed_safari_bookmarks", count=len(bookmarks), source=str(path))
    return bookmarks


def _walk_safari_node(
    node: Any,
    folder_stack: list[str],
    results: list[dict[str, Any]],
) -> None:
    if isinstance(node, list):
        for child in node:
            _walk_safari_node(child, folder_stack, results)
        return

    if not isinstance(node, dict):
        return

    bookmark_type = node.get("WebBookmarkType")
    title = _safe_label(node.get("Title")) or _safe_label(node.get("URIDictionary", {}).get("title"))
    if bookmark_type == "WebBookmarkTypeLeaf":
        url = str(node.get("URLString", "")).strip()
        if url:
            results.append(
                {
                    "title": title or url,
                    "url": url,
                    "folder_path": " / ".join(folder_stack),
                    "added_at": None,
                    "domain": _extract_domain(url),
                }
            )
        return

    next_stack = folder_stack + [title] if title else folder_stack
    children = node.get("Children")
    if children:
        _walk_safari_node(children, next_stack, results)


def parse_firefox_bookmarks_db(bookmark_path: str | pathlib.Path) -> list[dict[str, Any]]:
    source_path = pathlib.Path(bookmark_path)
    with tempfile.TemporaryDirectory(prefix="knowler-firefox-bookmarks-") as tmp_dir:
        copied_path = pathlib.Path(tmp_dir) / source_path.name
        shutil.copy2(source_path, copied_path)

        conn = sqlite3.connect(copied_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT id, type, parent, fk, title, dateAdded
                FROM moz_bookmarks
                """
            ).fetchall()
            place_rows = conn.execute("SELECT id, url FROM moz_places").fetchall()
        finally:
            conn.close()

    places = {row["id"]: row["url"] for row in place_rows if row["url"]}
    bookmark_rows = {row["id"]: dict(row) for row in rows}
    bookmarks: list[dict[str, Any]] = []

    for row in rows:
        if row["type"] != 1 or row["fk"] not in places:
            continue
        url = places[row["fk"]]
        folder_parts: list[str] = []
        parent_id = row["parent"]
        while parent_id in bookmark_rows:
            parent = bookmark_rows[parent_id]
            if parent["type"] == 2:
                label = _safe_label(parent.get("title"))
                if label:
                    folder_parts.append(label)
            next_parent = parent.get("parent")
            if next_parent == parent_id:
                break
            parent_id = next_parent

        folder_parts.reverse()
        bookmarks.append(
            {
                "title": row["title"] or url,
                "url": url,
                "folder_path": " / ".join(folder_parts),
                "added_at": _firefox_timestamp_to_iso(row["dateAdded"]),
                "domain": _extract_domain(url),
            }
        )

    log.info("parsed_firefox_bookmarks", count=len(bookmarks), source=str(source_path))
    return bookmarks


def load_browser_bookmarks(
    browser_id: str,
    profile: str | None = None,
    home_dir: pathlib.Path | str | None = None,
    system_name: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    source = resolve_browser_bookmark_source(
        browser_id,
        profile=profile,
        home_dir=home_dir,
        system_name=system_name,
    )
    if source is None:
        raise FileNotFoundError(f"No local bookmarks found for {browser_id}")

    if browser_id in _CHROMIUM_BROWSER_LABELS:
        bookmarks = parse_chromium_bookmarks_json(source["bookmark_path"])
    elif browser_id == "safari":
        bookmarks = parse_safari_bookmarks_plist(source["bookmark_path"])
    elif browser_id == "firefox":
        bookmarks = parse_firefox_bookmarks_db(source["bookmark_path"])
    else:
        raise ValueError(f"Unsupported browser source: {browser_id}")

    return source, bookmarks


def _walk_folder(
    node: Any,
    folder_stack: list[str],
    results: list[dict[str, Any]],
) -> None:
    """Recursively walk DL/DT structure of Netscape bookmark format.

    html.parser does not auto-close <dt> tags, so sibling DTs end up incorrectly
    nested inside each other.  We handle this in two ways:
    - After processing a bookmark DT, recurse into the DT itself so any DTs that
      html.parser nested inside it are also visited.
    - After processing a folder DT (h3 + inner DL), walk the DT's remaining children
      that appear *after* the inner DL (html.parser puts sibling DTs there too).
    """
    for child in node.children:
        if not hasattr(child, "name"):
            continue
        if child.name == "dt":
            h3 = child.find("h3", recursive=False)
            dl = child.find("dl", recursive=False)
            a = child.find("a", recursive=False)

            if h3 and dl:
                # Folder (html.parser style: inner DL is a direct child of DT)
                folder_name = h3.get_text(strip=True)
                _walk_folder(dl, folder_stack + [folder_name], results)
                # html.parser may nest sibling DTs inside this folder DT after the
                # inner DL (e.g. the next top-level folder ends up here).
                after_dl = False
                for sub in child.children:
                    if not hasattr(sub, "name"):
                        continue
                    if sub is dl:
                        after_dl = True
                        continue
                    if after_dl and sub.name in ("dt", "dl", "p"):
                        _walk_folder(sub, folder_stack, results)
            elif a and a.get("href"):
                # Bookmark
                url = a["href"].strip()
                title = a.get_text(strip=True) or url
                add_date = a.get("add_date") or a.get("added")
                results.append(
                    {
                        "title": title,
                        "url": url,
                        "folder_path": " / ".join(folder_stack) if folder_stack else "",
                        "added_at": _timestamp_to_iso(add_date) if add_date else None,
                        "domain": _extract_domain(url),
                    }
                )
                # html.parser nests the *next* sibling DT inside this bookmark DT;
                # recurse into it to pick those up.
                _walk_folder(child, folder_stack, results)
        elif child.name in ("dl", "p", "body", "html"):
            # Recurse into containers; <p> is a separator in Netscape bookmark format
            _walk_folder(child, folder_stack, results)


def should_auto_promote(
    bookmark: dict[str, Any],
    trusted_domains: list[str],
    trusted_folders: list[str],
) -> bool:
    """
    Return True if this bookmark should be auto-promoted to 'approved'.

    Rules (OR logic):
    1. domain is in trusted_domains list
    2. any part of folder_path matches a trusted_folder pattern
    """
    domain = bookmark.get("domain", "")
    folder = bookmark.get("folder_path", "").lower()

    if domain and any(domain.endswith(td.lower()) for td in trusted_domains):
        return True

    for tf in trusted_folders:
        if tf.lower() in folder:
            return True

    return False


def build_source_rows(
    bookmarks: list[dict[str, Any]],
    project_id: str,
    trusted_domains: list[str],
    trusted_folders: list[str],
    raw_dir: pathlib.Path,
    browser_source: str = "unknown",
    browser_profile: str | None = None,
) -> list[dict[str, Any]]:
    """
    Convert parsed bookmarks into source rows ready for DB insertion.

    Deduplicates by URL within this batch.
    """
    seen_urls: set[str] = set()
    rows: list[dict[str, Any]] = []

    for bm in bookmarks:
        url = bm.get("url", "").strip()
        if not url or url in seen_urls or url.startswith("javascript:"):
            continue
        seen_urls.add(url)

        source_id = f"src_{ULID()}"
        domain = bm.get("domain", _extract_domain(url))

        auto_promote = should_auto_promote(bm, trusted_domains, trusted_folders)
        ingest_state = "approved" if auto_promote else "needs_review"

        # Write a tiny stub file to raw/bookmark_candidates/
        stub_path = raw_dir / "bookmark_candidates" / f"{source_id}.json"
        stub_path.parent.mkdir(parents=True, exist_ok=True)
        stub_data = json.dumps(bm, ensure_ascii=False, indent=2)
        stub_path.write_text(stub_data, encoding="utf-8")

        checksum = hashlib.sha256(url.encode()).hexdigest()

        rows.append(
            {
                "id": source_id,
                "project_id": project_id,
                "source_type": "bookmark",
                "origin_type": "bookmark_import",
                "title": bm.get("title", url)[:1000],
                "canonical_url": url,
                "display_url": url[:500],
                "domain": domain[:255],
                "raw_path": str(stub_path),
                "mime_type": "application/x-bookmark",
                "checksum_sha256": checksum,
                "byte_size": len(stub_data.encode()),
                "trust_level": "medium" if auto_promote else "unknown",
                "status": "pending",
                "ingest_state": ingest_state,
                "source_date": bm.get("added_at"),
                "metadata_json": json.dumps(
                    {
                        "folder_path": bm.get("folder_path", ""),
                        "browser_source": browser_source,
                        "browser_profile": browser_profile,
                        "auto_promoted": auto_promote,
                    }
                ),
            }
        )

    return rows
