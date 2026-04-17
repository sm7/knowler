# End-to-End Testing

This repo now has two E2E layers:

1. A deterministic automated workflow test that exercises the real HTTP RPC surface, job queue, normalization, compile, search, query, graph, and maintenance paths without requiring a live LLM key.
2. A manual browser smoke test using a reusable fixture folder so you can validate the full UI flow in `localhost:5173` or the packaged app on `localhost:7842`.

## Automated E2E

Run the dedicated E2E suite:

```bash
make test-e2e
```

This runs [test_e2e_rpc_workflow.py](/Users/smukhopa/workspaces/test/Knowler/engine/tests/test_e2e_rpc_workflow.py), which covers:

- `project.create`
- `project.open`
- `project.importAndBuild`
- job queue completion for ingest, normalize, compile
- `sources.list`
- `pages.list`
- `search.run`
- `graph.knowledgeMap`
- `query.run` and `artifacts.get`
- `maintenance.run` and `maintenance.listFindings`
- `index.md` and `log.md` regeneration
- bookmark HTML import through `sources.importBookmarks`

Run the whole engine suite:

```bash
make test
```

## Manual Fixture

Create a reusable fixture folder:

```bash
make e2e-fixture
```

Or create it at a specific location:

```bash
bash ./scripts/create_e2e_fixture.sh /tmp/knowler-manual-e2e
```

The fixture contains:

- three markdown knowledge sources that should compile into source summaries and concept pages
- one bookmark HTML file for Inbox bookmark import

## Manual Browser Test

Start dev mode:

```bash
make dev
```

Use:

- `http://localhost:5173` for frontend development with hot reload
- `http://localhost:7842` for the packaged Python-served frontend

### Flow

1. Create the fixture folder with `make e2e-fixture`.
2. In `Projects`, create a new project pointing at that folder.
3. Wait for the initial import/build to finish.
4. Confirm:
   - `Inbox` shows the imported markdown sources
   - `Build` is enabled
   - `Wiki` contains source summary and concept pages
   - `Map` shows non-empty groups and nodes
   - `Health` loads findings without crashing
5. In `Ask`, ask:
   - `How do transformers, RLHF, and retrieval augmented generation connect in this project?`
6. Confirm:
   - an answer artifact is produced
   - a filed question page appears under the wiki
   - `log.md` contains the query entry
7. Go back to `Inbox` and import `research-bookmarks.html`.
8. Confirm:
   - bookmark sources appear in the Inbox
   - trusted domains such as `arxiv.org` and `openai.com` can be auto-approved if configured

## Expected Outputs

After a successful run, the project root should contain:

- `index.md`
- `log.md`
- `wiki/`
- `raw/`
- `normalized/`
- `outputs/`
- `.knowler/project.db`

The automated suite uses the same fixture content stored in [engine/tests/fixtures/e2e](/Users/smukhopa/workspaces/test/Knowler/engine/tests/fixtures/e2e).
