# Knowler — Local-First Knowledge Compiler

A file-first, AI-assisted research and knowledge operating system. Knowler watches your reading materials (bookmarks, PDFs, articles), extracts structured knowledge, and compiles it into an Obsidian-compatible wiki — automatically and with full offline capability.

---

## Architecture in one sentence

A **Python web server** (FastAPI) serves a **React frontend** in your browser and runs the **knowledge engine** in-process, storing everything in a **SQLite-backed, file-native vault**.

```
Browser (React UI)  ←—HTTP/WebSocket—→  Python server (FastAPI)
                                                 ↕
                                       SQLite vault on disk
```

---

## Quick start

```bash
pip install knowler
export ANTHROPIC_API_KEY="sk-ant-..."
knowler
# Opens http://localhost:7842 in your browser
```

---

## Prerequisites

| Tool | Version | Install |
|------|---------|---------|
| Python | 3.10+ | `brew install python` or [pyenv](https://github.com/pyenv/pyenv) |
| Node.js | 20 LTS | Only needed to build from source |

No Rust. No Xcode.

---

## Repository layout

```
Knowler/
├── app/                     # React + TypeScript frontend (Vite)
│   ├── src/
│   │   ├── components/      # Shared UI components
│   │   ├── screens/         # Route-level screens
│   │   ├── lib/ipc.ts       # HTTP + WebSocket engine client
│   │   └── types/           # TypeScript types
│   └── package.json
├── engine/                  # Python server + knowledge engine
│   ├── knowler_engine/
│   │   ├── server.py        # FastAPI app, WebSocket event bus
│   │   ├── __main__.py      # CLI entry point (knowler serve / engine)
│   │   ├── ipc/             # Router, types, error codes
│   │   ├── storage/         # SQLite, atomic writes, vault paths
│   │   ├── project/         # Project lifecycle
│   │   ├── jobs/            # Async job runner
│   │   ├── ingest/          # Bookmark / URL / PDF adapters
│   │   ├── normalize/       # LLM-powered normalization pipeline
│   │   ├── compile/         # Wiki page generation
│   │   ├── search/          # FTS5 + relation graph search
│   │   ├── query/           # 5-stage artifact synthesis
│   │   ├── maintenance/     # Deterministic knowledge health checks
│   │   └── static/          # Bundled React build (generated)
│   ├── migrations/sqlite/   # SQL migrations (applied in order)
│   ├── prompts/             # Jinja2 LLM prompt templates
│   ├── tests/               # pytest suite (144 tests)
│   └── pyproject.toml
├── docs/                    # Architecture decisions + setup notes
└── Makefile                 # Build orchestration
```

---

## Development setup

### 1. Engine

```bash
cd engine
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Run tests:
```bash
pytest tests/ -v
```

Start the engine server in dev mode (no frontend):
```bash
python -m knowler_engine serve --dev --no-browser
# API at http://localhost:7842/api/rpc
# WebSocket at ws://localhost:7842/ws
```

### 2. Frontend (hot reload during development)

```bash
cd app
npm install
npm run dev
# Open http://localhost:5173
# Vite proxies /api and /ws to the engine on port 7842
```

### 3. LLM API key

Set via the Settings screen in the app (stored in localStorage), or for dev:
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

---

## Building for distribution

### Option A — Development install (editable)

```bash
make install
knowler
```

### Option B — Wheel (pip-installable)

```bash
make release
# Produces engine/dist/knowler-0.1.0.tar.gz
# and engine/dist/knowler-0.1.0-py3-none-any.whl
pip install engine/dist/knowler-0.1.0-py3-none-any.whl
knowler
```

The `make build` step compiles the React frontend and copies the `dist/` folder into `engine/knowler_engine/static/` so the Python package is self-contained.

### Option C — PyPI

```bash
pip install knowler
knowler
```

### Publishing to PyPI

```bash
make release
make check-dist
make publish-testpypi   # optional dry run against TestPyPI
make publish-pypi
```

---

## IPC protocol

The browser and engine communicate over two channels:

**RPC (HTTP POST `/api/rpc`)** — request/response:
```json
// Request
POST /api/rpc
{"method": "project.create", "params": {"name": "My Research", "root_path": "/Users/you/Research"}}

// Response
{"ok": true, "result": {"project_id": "01HX...", "name": "My Research"}}
```

**Events (WebSocket `/ws`)** — server-pushed:
```json
{"type": "event", "event": "job.progress", "payload": {"job_id": "...", "progress": 0.45}}
{"type": "event", "event": "engine.ready", "payload": {"engine_version": "0.1.0"}}
```

---

## Vault structure

Each project is a folder on disk:

```
<project_root>/
├── wiki/                    # Compiled wiki pages (.md)
├── raw/                     # Immutable ingest copies
├── normalized/              # Structured JSON extractions
├── outputs/                 # Generated query artifacts
├── maintenance/             # Health check reports
├── config.yaml              # Project configuration
└── .knowler/
    ├── project.db           # SQLite database (WAL mode)
    └── tmp/                 # Temp files for atomic writes
```

Files outside the engine-owned directories are never touched.

---

## Privacy

- All source files stay on your machine. Only selected text is sent to the configured LLM provider.
- API keys are stored in **browser localStorage** for the frontend and in the OS keychain when saved through the engine.
- No telemetry, analytics, or background network calls beyond explicit LLM API requests.
- The server binds to `127.0.0.1` only — not accessible from other machines on your network.

---

## Environment variables

| Variable | Purpose |
|----------|---------|
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `OPENAI_API_KEY` | OpenAI API key |
| `KNOWLER_LLM_PROVIDER` | `anthropic` (default) or `openai` |
| `KNOWLER_WORKSPACE` | Override default workspace path |

Default workspace: `~/Library/Application Support/Knowler` (macOS) or `~/.knowler` (Linux/Windows).

---

## CLI reference

```
knowler                      # Start server on port 7842, open browser
knowler serve --port 9000    # Custom port
knowler serve --no-browser   # Don't auto-open browser
knowler serve --dev          # Verbose logging + dev mode
knowler engine               # Stdio NDJSON mode for scripting/integration
```

---

## Docs

- [`docs/dev_setup.md`](docs/dev_setup.md) — Detailed environment setup
- [`docs/e2e_testing.md`](docs/e2e_testing.md) — Automated and manual end-to-end test flows
- [`docs/status_summary.md`](docs/status_summary.md) — What's built, what's deferred
- [`docs/implementation_assumptions.md`](docs/implementation_assumptions.md) — Decisions made during scaffold
- [`docs/architecture_deviations.md`](docs/architecture_deviations.md) — Deviations from original spec
