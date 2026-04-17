# Implementation Assumptions

This file records every decision made where the architecture document was ambiguous or silent.
All assumptions aim to be the smallest reasonable interpretation consistent with the document.

---

## A001 — Product/path naming

**Architecture doc says:** Uses "Knoler" in example paths (e.g. `~/Knoler/Projects/...`)
**Assumption:** Internal code identifiers use `knowler`. Display name is "Knowler". The hidden metadata directory inside a project root is `.knowler/` (not `.knoler/`).
**Reason:** Consistent casing and typo avoidance. The display name shown to users is "Knowler."

---

## A002 — Engine packaging for v1

**Architecture doc says:** "Python 3.12 knowledge engine for v1"
**Assumption:** v1 is distributed primarily as a standard Python package on PyPI. Users install it with `pip install knowler`, and the `knowler` CLI starts the local FastAPI server and serves the bundled frontend.
**Reason:** The product goal is a pip-installable local application, not a desktop wrapper. A standard wheel + sdist release path is simpler to ship and maintain.

---

## A003 — Database path

**Architecture doc says:** `<project_root>/.knoler/project.db`
**Assumption:** Path is `<project_root>/.knowler/project.db` (matching assumption A001).
**Reason:** Consistent with the `knowler` naming convention.

---

## A004 — Global app state database

**Architecture doc says:** One SQLite DB per project. Silent on where to track the global list of known projects.
**Assumption:** A separate global DB lives at `~/Library/Application Support/Knowler/knowler.db` and holds just the `projects` table rows (id, name, slug, root_path, status). This allows the Projects screen to load without opening each project individually.
**Reason:** Standard Mac app convention. Keeps per-project DBs clean.

---

## A005 — LLM provider for v1

**Architecture doc says:** Supports multiple model tiers; mentions OpenAI implicitly but is provider-agnostic.
**Assumption:** v1 ships with Anthropic Claude as the default provider (claude-haiku-4-5 for fast tier, claude-sonnet-4-6 for balanced, claude-opus-4-6 for best). Provider adapter is abstracted so adding OpenAI is straightforward. API keys stored in macOS Keychain via the `keyring` Python library.
**Reason:** Claude has strong structured output support. The arch doc is provider-agnostic, so defaulting to Claude is a reasonable choice.

---

## A006 — URL fetching approach

**Architecture doc says:** URL/article ingest via the ingest worker.
**Assumption:** URL fetch uses `httpx` (async HTTP) + `trafilatura` for article extraction + `markdownify` for HTML-to-markdown. No headless browser in v1.
**Reason:** Matches the tech stack suggestions in section 27.5. Headless browser adds too much packaging complexity for v1.

---

## A007 — Bookmark import format

**Architecture doc says:** Browser-exported bookmark HTML files (Chrome, Safari, Firefox).
**Assumption:** Parsing uses Python's `html.parser` via `BeautifulSoup` on the exported `bookmarks.html`. No browser extension or API in v1.
**Reason:** Exactly what section 45.2 says.

---

## A008 — Job execution model

**Architecture doc says:** "in-process background workers or lightweight local job runner" and "avoid requiring Redis or external queues."
**Assumption:** Jobs run in Python `asyncio` tasks within the engine process. An `asyncio.Queue` serves as the in-process job queue. No external queue. Each job type has a dedicated async handler.
**Reason:** Simplest correct implementation for single-user local workload.

---

## A009 — File write conflict detection

**Architecture doc says:** Conflict detection using content hash and/or mtime.
**Assumption:** v1 uses both: record the SHA-256 of the file at last-write time and the mtime. Before any write, re-read and compare both. If either changed, surface conflict.
**Reason:** Belt-and-suspenders. mtime is cheap; SHA-256 is authoritative.

---

## A010 — Obsidian compatibility

**Architecture doc says:** Obsidian-compatible vault; Obsidian is optional.
**Assumption:** "Compatible" means: (1) all markdown files use `.md` extension, (2) internal wiki links use `[[Page Name]]` double-bracket syntax, (3) YAML frontmatter is valid. No `.obsidian/` folder is created automatically — user creates it if they open in Obsidian.
**Reason:** These three constraints are the minimum for Obsidian to correctly index and navigate the vault.

---

## A011 — Source ID format

**Architecture doc says:** `src_00123` style IDs in examples.
**Assumption:** IDs are `<prefix>_<ulid>` strings where prefix identifies the table (e.g., `src_`, `ent_`, `clm_`, `rel_`, `pg_`, `art_`, `job_`, `qr_`). ULIDs are used for lexicographic ordering and collision resistance.
**Reason:** ULIDs are time-sortable and collision-free. More practical than zero-padded integers.

---

## A012 — Web IPC vs stdio IPC

**Architecture doc says:** IPC between thin app and engine via stdio NDJSON.
**Assumption:** The primary UI talks to the engine over local HTTP + WebSocket (`/api/rpc` and `/ws`). The stdio NDJSON transport is retained only as a scripting/integration mode via `knowler engine`.
**Reason:** The browser-served frontend and pip-installable distribution are simpler when the engine is the only long-lived process. HTTP/WebSocket keeps the protocol explicit while preserving a lightweight scripting path.

---

## A013 — Atomic write pattern

**Architecture doc says:** Atomic writes using temp file + rename.
**Assumption:** Temp files are written to `<project_root>/.knowler/tmp/<uuid>.tmp` then renamed to final path. If the target directory doesn't exist, it is created atomically using `os.makedirs(exist_ok=True)`.
**Reason:** `os.rename()` is atomic on POSIX (same filesystem). Cross-device renames fall back to copy+delete.

---

## A014 — Search FTS synchronization

**Architecture doc says:** Keep `content_units_fts` synchronized from application code during inserts and updates.
**Assumption:** Every write to `content_units` calls a helper that inserts/updates the FTS index in the same transaction.
**Reason:** Exactly what the architecture doc prescribes.

---

## A015 — Prompt template format

**Architecture doc says:** Templates in `engine/prompts/*.md`.
**Assumption:** Templates use Jinja2 syntax with `{{ variable }}` interpolation. The template engine is `jinja2`.
**Reason:** Industry standard for Python templating. Allows complex prompts with conditionals.
