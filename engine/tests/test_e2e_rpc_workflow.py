"""Full RPC-level end-to-end workflow tests for Knowler."""
from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from knowler_engine.__main__ import _register_handlers, _register_job_handlers
from knowler_engine.jobs.runner import JobRunner
from knowler_engine.llm.provider import LLMProvider
from knowler_engine.project.manager import ProjectManager
from knowler_engine.server import AppContext, create_app
from knowler_engine.storage.db import open_global_db
from knowler_engine.ipc.router import Router


FIXTURES_DIR = Path(__file__).parent / "fixtures" / "e2e"


class _NullTransport:
    async def send(self, _message) -> None:
        return None


async def _rpc(client: AsyncClient, method: str, params: dict) -> dict:
    response = await client.post("/api/rpc", json={"method": method, "params": params})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True, body
    return body["result"]


async def _wait_for_project_idle(
    client: AsyncClient,
    project_id: str,
    timeout_s: float = 15.0,
) -> list[dict]:
    deadline = asyncio.get_running_loop().time() + timeout_s
    last_jobs: list[dict] = []
    while asyncio.get_running_loop().time() < deadline:
        last_jobs = (await _rpc(client, "jobs.list", {"project_id": project_id}))["jobs"]
        if last_jobs and all(job["status"] not in {"queued", "running"} for job in last_jobs):
            return last_jobs
        await asyncio.sleep(0.05)
    raise TimeoutError(f"Project {project_id} did not go idle in time")


def _copy_fixture_sources(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for name in (
        "transformer_foundations.md",
        "chat_model_alignment.md",
        "retrieval_augmented_generation.md",
    ):
        (destination / name).write_text((FIXTURES_DIR / name).read_text(encoding="utf-8"), encoding="utf-8")


@pytest_asyncio.fixture
async def rpc_client(tmp_path: Path):
    global_db = await open_global_db(tmp_path / "support")
    project_manager = ProjectManager(global_db=global_db)
    router = Router(_NullTransport())
    llm = LLMProvider(provider="openai", api_key=None)

    ctx = AppContext(
        global_db=global_db,
        project_manager=project_manager,
        job_runner=None,
        llm=llm,
        router=router,
    )
    job_runner = JobRunner(ctx)
    ctx.job_runner = job_runner

    _register_handlers(router, ctx)
    _register_job_handlers(job_runner)

    app = create_app(ctx)
    runner_task = asyncio.create_task(job_runner.run_forever())

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client, tmp_path
    finally:
        job_runner.stop()
        runner_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await runner_task
        await project_manager.close_all()
        await global_db.close()


@pytest.mark.asyncio
async def test_full_rpc_workflow_import_build_query_graph_and_maintenance(rpc_client):
    client, tmp_path = rpc_client

    project_root = tmp_path / "manual-project"
    _copy_fixture_sources(project_root)

    created = await _rpc(
        client,
        "project.create",
        {"name": "RPC Workflow Project", "root_path": str(project_root)},
    )
    project_id = created["project_id"]

    listed = await _rpc(client, "project.list", {})
    assert any(project["project_id"] == project_id for project in listed["projects"])

    reopened = await _rpc(client, "project.open", {"root_path": str(project_root)})
    assert reopened["project_id"] == project_id

    imported = await _rpc(client, "project.importAndBuild", {"project_id": project_id})
    assert imported["files_found"] == 3

    jobs = await _wait_for_project_idle(client, project_id)
    assert any(job["job_type"] == "ingest_files" and job["status"] == "succeeded" for job in jobs)
    assert any(job["job_type"] == "normalize_source" and job["status"] == "succeeded" for job in jobs)
    assert any(job["job_type"] == "compile_project" and job["status"] == "succeeded" for job in jobs)

    sources = (await _rpc(client, "sources.list", {"project_id": project_id}))["sources"]
    assert len(sources) == 3
    assert all(source["status"] == "compiled" for source in sources)

    pages = (await _rpc(client, "pages.list", {"project_id": project_id}))["pages"]
    assert len(pages) >= 3
    assert any(page["page_type"] == "source_summary" for page in pages)
    assert any(page["page_type"] == "concept" for page in pages)

    search = await _rpc(
        client,
        "search.run",
        {"project_id": project_id, "query": "RLHF", "limit": 10},
    )
    assert search["results"], "Expected search results after compile"

    graph = await _rpc(client, "graph.knowledgeMap", {"project_id": project_id})
    assert graph["stats"]["node_count"] > 0
    assert graph["stats"]["group_count"] > 0
    assert graph["stats"]["explicit_edge_count"] >= 0
    assert graph["stats"]["inferred_edge_count"] >= 0
    assert graph["groups"][0]["rationale"]

    query = await _rpc(
        client,
        "query.run",
        {
            "project_id": project_id,
            "prompt": "How do transformers, RLHF, and retrieval augmented generation connect in this project?",
            "task_type": "answer",
            "output_format": "markdown_note",
            "model_tier": "balanced",
            "file_back": True,
        },
    )
    await _wait_for_project_idle(client, project_id)

    query_run = await _rpc(
        client,
        "query.get",
        {"project_id": project_id, "query_run_id": query["query_run_id"]},
    )
    assert query_run["status"] == "succeeded"
    assert query_run["result_artifact_id"]

    artifact = await _rpc(
        client,
        "artifacts.get",
        {"project_id": project_id, "artifact_id": query_run["result_artifact_id"]},
    )
    artifact_content = artifact["content"] or ""
    assert len(artifact_content) > 120
    assert "transform" in artifact_content.lower()

    pages_after_query = (await _rpc(client, "pages.list", {"project_id": project_id}))["pages"]
    assert any(page["page_type"] == "question" for page in pages_after_query)

    maintenance_job = await _rpc(client, "maintenance.run", {"project_id": project_id})
    assert maintenance_job["job_id"].startswith("job_")
    await _wait_for_project_idle(client, project_id)

    findings = await _rpc(client, "maintenance.listFindings", {"project_id": project_id})
    assert isinstance(findings["findings"], list)

    index_path = project_root / "index.md"
    log_path = project_root / "log.md"
    graph_path = project_root / "graph.json"
    graph_report_path = project_root / "GRAPH_REPORT.md"
    assert index_path.exists()
    assert log_path.exists()
    assert graph_path.exists()
    assert graph_report_path.exists()
    assert "Project Index" in index_path.read_text(encoding="utf-8")
    log_text = log_path.read_text(encoding="utf-8")
    assert "query | How do transformers, RLHF" in log_text
    assert "Task type: answer" in log_text
    assert '"nodes": [' in graph_path.read_text(encoding="utf-8")
    assert "Knowledge Graph Report" in graph_report_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_bookmark_html_ingest_via_rpc_imports_rows_into_inbox(rpc_client):
    client, tmp_path = rpc_client

    project_root = tmp_path / "bookmark-project"
    project_root.mkdir(parents=True, exist_ok=True)
    created = await _rpc(
        client,
        "project.create",
        {
            "name": "Bookmark Workflow Project",
            "root_path": str(project_root),
            "config": {"trusted_domains": ["arxiv.org", "openai.com"]},
        },
    )
    project_id = created["project_id"]

    bookmark_job = await _rpc(
        client,
        "sources.importBookmarks",
        {
            "project_id": project_id,
            "bookmark_file_path": str(FIXTURES_DIR / "research-bookmarks.html"),
        },
    )
    assert bookmark_job["job_id"].startswith("job_")
    await _wait_for_project_idle(client, project_id)

    sources = (await _rpc(client, "sources.list", {"project_id": project_id}))["sources"]
    assert len(sources) == 3
    assert all(source["source_type"] == "bookmark" for source in sources)
    assert any(source["ingest_state"] == "approved" for source in sources)
    assert any(source["ingest_state"] == "needs_review" for source in sources)
