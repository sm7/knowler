# Implementation Status Summary

**Version:** 0.1  
**Last updated:** 2026-04-04

---

## Architecture

Knowler runs as a **local Python web server** (FastAPI + uvicorn) that serves the React frontend and exposes the knowledge engine over HTTP/WebSocket. No Rust or desktop wrapper required.

```
Browser  ←── HTTP POST /api/rpc ──→  FastAPI server (Python)
         ←── WebSocket /ws      ──→        ↕
                                     SQLite vault
```

---

## What is complete

### Python Engine

**Server** (`server.py`)
- [x] FastAPI app with CORS middleware
- [x] `POST /api/rpc` — JSON-RPC dispatcher, calls registered handlers directly
- [x] `GET /ws` — WebSocket endpoint, broadcasts engine events to all connected clients
- [x] `GET /` — serves bundled React frontend from `knowler_engine/static/`
- [x] `GET /api/health` — health check
- [x] `WebSocketTransport` — replaces stdio transport for event emission
- [x] Auto-open browser on startup (with 1.5s delay to ensure server is ready)
- [x] `knowler serve` CLI subcommand (default)
- [x] `knowler engine` CLI subcommand (stdio NDJSON mode, for scripting)

**IPC layer** (`ipc/`)
- [x] `types.py` — `Request`, `SuccessResponse`, `ErrorResponse`, `Event`, `ErrorCode` (12 codes)
- [x] `transport.py` — Async stdio transport (kept for `engine` subcommand)
- [x] `router.py` — `@router.method` decorator, `EngineError`, `emit()`

**Storage** (`storage/`)
- [x] `db.py` — SQLite with WAL/FK pragmas, migration runner, `transaction()`
- [x] `atomic.py` — `write_atomic()`, conflict detection, `snapshot()`, `compute_sha256()`
- [x] `vault.py` — `Vault` dataclass, path helpers, `initialize()`, `is_engine_owned()`

**SQL migrations** (`migrations/sqlite/`)
- [x] `0001_initial.sql` — 14 tables + indexes
- [x] `0002_fts.sql` — FTS5 virtual table (porter + unicode61)
- [x] `0003_global.sql` — global projects + app settings

**Project manager** (`project/`)
- [x] `create_project()`, `open_project()`, `list_projects()`, `get_project()`, `close_all()`

**Job runner** (`jobs/`)
- [x] `asyncio.Queue`-based in-process queue, status lifecycle, `cancel()`, `run_forever()`
- [x] `log_event()` — persists to DB and emits WebSocket event

**Ingest** (`ingest/`)
- [x] Bookmark HTML parser (Netscape format, handles html.parser's broken DT nesting)
- [x] URL fetcher (httpx + trafilatura + markdownify)
- [x] PDF extractor (pypdf)
- [x] Job handlers: `handle_ingest_bookmarks`, `handle_ingest_urls`, `handle_ingest_files`

**Normalize** (`normalize/`)
- [x] LLM normalization with Jinja2 prompts, validation, one repair attempt

**Compile** (`compile/`)
- [x] `compile_source_summary()`, `compile_concept_page()`, `build_indexes()`
- [x] Atomic writes with conflict detection, FTS sync

**Search** (`search/`)
- [x] FTS5 MATCH + scoring, `exact_search()`, `relation_expand()` (1–2 hop graph)

**Query** (`query/`)
- [x] 5-stage pipeline: classify → gather → synthesize → write → emit phase events

**Maintenance** (`maintenance/`)
- [x] 5 checks: orphan pages, duplicate entities, weak claims, stale pages, missing comparisons

**LLM** (`llm/`)
- [x] Anthropic + OpenAI backends, key resolution (env → Keychain), `parse_llm_json()`

**IPC method handlers** (`__main__.py`)
- [x] `engine.handshake`, `engine.shutdown`
- [x] `project.create`, `project.open`, `project.list`, `project.get`
- [x] `sources.importBookmarks`, `sources.importUrls`, `sources.importFiles`
- [x] `sources.list`, `sources.promote`, `sources.reject`
- [x] `compile.runProject`
- [x] `query.run`, `query.list`
- [x] `search.run`
- [x] `maintenance.run`, `maintenance.listFindings`, `maintenance.ignoreFinding`
- [x] `artifacts.list`, `artifacts.get`
- [x] `jobs.list`, `jobs.cancel`

### Frontend (React + TypeScript)

- [x] `lib/ipc.ts` — `engineCall<T>()` (fetch), WebSocket event bus, all event listeners
- [x] `App.tsx` — 3-column shell, engine status, routing
- [x] `components/Sidebar.tsx`, `EngineStatusBanner.tsx`, `JobStatusPanel.tsx`
- [x] `screens/ProjectsScreen.tsx` — project list, create, open
- [x] `screens/InboxScreen.tsx` — source review queue
- [x] `screens/BuildScreen.tsx` — compile trigger + job log
- [x] `screens/AskScreen.tsx` — query input + artifact output
- [x] `screens/ArtifactsScreen.tsx` — artifact browser
- [x] `screens/HealthScreen.tsx` — maintenance findings
- [x] `screens/SettingsScreen.tsx` — API key + LLM provider config

### Build tooling

- [x] `Makefile` — `dev`, `build`, `install`, `release`, `clean` targets
- [x] `engine/pyproject.toml` — package name `knowler`, `knowler` CLI entry point, fastapi/uvicorn deps
- [x] `app/vite.config.ts` — dev proxy to engine port 7842

### Tests (`engine/tests/`) — 144 passing

- [x] `test_ipc_types.py` — envelope types, 12 error codes
- [x] `test_ipc_router.py` — dispatch, error handling, emit
- [x] `test_storage_atomic.py` — atomic write, conflict detection
- [x] `test_storage_db.py` — migrations, WAL, FK, transactions
- [x] `test_vault.py` — initialization, path helpers
- [x] `test_project_manager.py` — create, list, slug conflict
- [x] `test_ingest_adapters.py` — bookmark parser, auto-promote, dedup
- [x] `test_search_fts.py` — FTS match, scoring, relation graph
- [x] `test_maintenance_checks.py` — all 5 checks
- [x] `test_llm_provider.py` — provider, parse_llm_json, model tiers
- [x] `test_jobs_runner.py` — enqueue, execute, lifecycle
- [x] `test_e2e_happy_path.py` — end-to-end project creation through search

---

## What is NOT complete (v2 items)

| Feature | Notes |
|---------|-------|
| **Auto-ingest watch** | File system watcher for continuous background ingestion |
| **Sync / export** | Obsidian sync or PARA export |
| **Local model support** | Ollama or MLX backend (privacy/offline mode) |
| **Windows / Linux** | Paths assume macOS; minor tweaks needed for other platforms |
| **Relation graph UI** | Data in `relations` table; no visual graph view yet |
| **Source history UI** | `source_versions` table populated; no diff view in UI |
| **Reading plan execution** | Task type listed in UI; handler not wired |
| **Slides output** | Marp format in task list; no template yet |
| **App icons** | Placeholder only |

---

## How to verify

```bash
# 1. Install
cd engine
source .venv/bin/activate
pip install -e ".[dev]"

# 2. Run tests
pytest tests/ -v   # expect 144 passed

# 3. Start server
python -m knowler_engine serve --dev --no-browser

# 4. Hit the health endpoint
curl http://localhost:7842/api/health
# {"status":"ok","engine_version":"0.1.0"}

# 5. Make an RPC call
curl -s -X POST http://localhost:7842/api/rpc \
  -H "Content-Type: application/json" \
  -d '{"method":"engine.handshake","params":{"protocol_version":1}}'
# {"ok":true,"result":{"protocol_version":1,"engine_version":"0.1.0",...}}
```
