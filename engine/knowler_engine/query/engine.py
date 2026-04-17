"""
Query engine.

Implements the full query lifecycle:
  user request → intent classification → query plan → evidence retrieval
  → evidence ranking → synthesis → artifact writing → provenance attachment

Architecture: §17 (complete section)
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
from datetime import datetime, timezone
from typing import Any

import structlog
from jinja2 import Environment, FileSystemLoader
from ulid import ULID

from knowler_engine.jobs.types import JobContext
from knowler_engine.llm.provider import LLMError, LLMProvider, parse_llm_json
from knowler_engine.search.fts import exact_search, relation_expand, search_project
from knowler_engine.storage.atomic import ConflictError, snapshot, write_atomic
from knowler_engine.system_pages import build_project_indexes, rebuild_project_log

log = structlog.get_logger(__name__)

_PROMPTS_DIR = pathlib.Path(__file__).parent.parent.parent / "prompts"
_jinja = Environment(loader=FileSystemLoader(str(_PROMPTS_DIR)), autoescape=False)

# Supported task types (§17.4)
TASK_TYPES = {
    "answer", "comparison", "study_guide", "reading_plan", "report",
    "slides", "checklist", "open_questions", "gap_analysis", "maintenance_report",
}

# Default evidence budget (§17.11)
_MAX_WIKI_PAGES = 8
_MAX_SOURCE_SUMMARIES = 5
_MAX_CLAIMS = 15


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _slugify(text: str) -> str:
    import re
    s = text.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s)
    return s.strip("-")[:80]


async def classify_intent(
    prompt: str,
    task_type_hint: str,
    output_format_hint: str,
    llm: LLMProvider,
) -> dict[str, Any]:
    """
    Stage 1: Classify the user's intent and produce a query plan.

    Non-LLM first: if task_type is explicit and not 'auto', use it directly.
    LLM: only for 'auto' or ambiguous requests.

    Returns a plan dict matching the query planner prompt contract (§42.5).
    """
    if task_type_hint != "auto" and task_type_hint in TASK_TYPES:
        # Deterministic: extract search terms from prompt
        words = [w.strip(".,?!") for w in prompt.split() if len(w) > 3]
        return {
            "task_type": task_type_hint,
            "output_format": output_format_hint or "markdown_report",
            "entities": [],
            "need_raw_sources": False,
            "need_wiki_pages": True,
            "need_claims": True,
            "retrieval_strategy": {
                "search_terms": words[:5],
                "relation_hops": 1,
                "preferred_unit_types": ["comparison", "page_body", "source_summary", "claim"],
                "per_entity_min_sources": 2,
            },
            "synthesis_plan": {"sections": []},
        }

    # LLM-assisted planning for 'auto' task type
    plan_system = """You are a query planner for Knowler knowledge system.
Classify the user's request and produce a retrieval plan as JSON.
Output only valid JSON matching this schema exactly."""

    plan_user = f"""User request: "{prompt}"

Available task types: {', '.join(sorted(TASK_TYPES))}

Return JSON:
{{
  "task_type": "one of the task types above",
  "output_format": "%s",
  "entities": ["entity name 1", "entity name 2"],
  "need_raw_sources": false,
  "need_wiki_pages": true,
  "need_claims": true,
  "retrieval_strategy": {{
    "search_terms": ["term1", "term2"],
    "relation_hops": 1,
    "preferred_unit_types": ["comparison", "page_body", "source_summary", "claim"],
    "per_entity_min_sources": 2
  }},
  "synthesis_plan": {{
    "sections": []
  }}
}}""" % (output_format_hint or "markdown_report")

    try:
        resp = await llm.complete(plan_system, plan_user, tier="fast", max_tokens=500, response_format="json")
        plan = parse_llm_json(resp.text)
        # Validate
        if plan.get("task_type") not in TASK_TYPES:
            plan["task_type"] = "report"
        plan.setdefault("output_format", output_format_hint or "markdown_report")
        plan.setdefault("entities", [])
        plan.setdefault("retrieval_strategy", {"search_terms": []})
        return plan
    except Exception as exc:
        log.warning("query_plan_llm_failed", error=str(exc))
        # Fallback conservative plan
        words = [w.strip(".,?!") for w in prompt.split() if len(w) > 3]
        return {
            "task_type": "answer",
            "output_format": output_format_hint or "markdown_report",
            "entities": [],
            "need_wiki_pages": True,
            "need_claims": True,
            "retrieval_strategy": {"search_terms": words[:5], "relation_hops": 1},
            "synthesis_plan": {"sections": []},
        }


async def gather_evidence(
    db: Any,
    project_id: str,
    plan: dict[str, Any],
) -> dict[str, Any]:
    """
    Stage 2 + 3: Retrieve and rank evidence.

    Returns a structured evidence package.
    Non-LLM by design (§17.8).
    """
    evidence: dict[str, Any] = {
        "wiki_pages": [],
        "source_summaries": [],
        "claims": [],
        "source_ids": set(),
        "page_ids": set(),
    }

    search_terms = plan.get("retrieval_strategy", {}).get("search_terms", [])
    relation_hops = plan.get("retrieval_strategy", {}).get("relation_hops", 1)

    # Layer 1: Exact title/entity matching
    for term in search_terms[:3]:
        exact_results = await exact_search(db, project_id, term, limit=5)
        for r in exact_results:
            if r["kind"] == "page":
                evidence["page_ids"].add(r["id"])
            elif r["kind"] == "entity" and relation_hops > 0:
                expanded = await relation_expand(db, project_id, [r["id"]], hops=relation_hops)
                for rel_ent in expanded[:5]:
                    # Find pages about this related entity
                    pages = await db.fetchall(
                        """
                        SELECT p.id FROM pages p
                        JOIN relations r ON r.from_id = p.id AND r.to_id = ?
                        WHERE r.project_id=? AND r.from_kind='page' AND r.relation_type='about'
                        LIMIT 3
                        """,
                        (rel_ent["entity_id"], project_id),
                    )
                    for p in pages:
                        evidence["page_ids"].add(p["id"])

    # Layer 2: FTS over content_units
    all_terms = " ".join(search_terms[:5])
    if all_terms.strip():
        fts_results = await search_project(
            db, project_id, all_terms,
            limit=20,
            unit_types=plan.get("retrieval_strategy", {}).get("preferred_unit_types"),
        )
        for r in fts_results[:15]:
            if r["page_id"]:
                evidence["page_ids"].add(r["page_id"])
            if r["source_id"]:
                evidence["source_ids"].add(r["source_id"])

    # Load wiki pages
    for page_id in list(evidence["page_ids"])[:_MAX_WIKI_PAGES]:
        row = await db.fetchone(
            "SELECT id, page_type, title, file_path, status FROM pages WHERE id=? AND status='active'",
            (page_id,),
        )
        if not row:
            continue
        page_path = pathlib.Path(row["file_path"])
        if page_path.exists():
            body = page_path.read_text(encoding="utf-8", errors="replace")
            evidence["wiki_pages"].append({
                "id": page_id,
                "page_type": row["page_type"],
                "title": row["title"],
                "body": body,
            })

    # Load source summaries
    for sid in list(evidence["source_ids"])[:_MAX_SOURCE_SUMMARIES]:
        row = await db.fetchone(
            """
            SELECT id, title, canonical_url,
                   json_extract(metadata_json, '$.summary') as summary
            FROM sources WHERE id=? AND deleted_at IS NULL
            """,
            (sid,),
        )
        if row and row["summary"]:
            evidence["source_summaries"].append({
                "source_id": row["id"],
                "title": row["title"],
                "summary": row["summary"][:1000],
            })

    # Load claims
    if plan.get("need_claims", True) and search_terms:
        claim_rows = await db.fetchall(
            """
            SELECT claim_text as text, claim_kind, confidence
            FROM claims WHERE project_id=? AND status='active'
            ORDER BY confidence DESC LIMIT ?
            """,
            (project_id, _MAX_CLAIMS),
        )
        evidence["claims"] = [dict(r) for r in claim_rows]

    evidence["source_ids"] = list(evidence["source_ids"])
    evidence["page_ids"] = list(evidence["page_ids"])

    log.info(
        "evidence_gathered",
        project_id=project_id,
        pages=len(evidence["wiki_pages"]),
        sources=len(evidence["source_summaries"]),
        claims=len(evidence["claims"]),
    )
    return evidence


def _strip_frontmatter(markdown: str) -> str:
    if not markdown.startswith("---"):
        return markdown
    parts = markdown.split("---", 2)
    if len(parts) < 3:
        return markdown
    return parts[2].lstrip()


def _render_fallback_artifact(
    prompt: str,
    plan: dict[str, Any],
    evidence: dict[str, Any],
    reason: str,
) -> str:
    """Generate a deterministic answer when the LLM path cannot."""
    wiki_pages = evidence.get("wiki_pages", [])
    source_summaries = evidence.get("source_summaries", [])
    claims = evidence.get("claims", [])
    task_type = plan.get("task_type", "answer")

    if not wiki_pages and not source_summaries and not claims:
        return f"""## Answer

Knowler does not yet have enough compiled evidence to answer this {task_type} request.

## Why This Happened

- Query: {prompt}
- Reason: {reason}
- Matching wiki pages: 0
- Matching source summaries: 0
- Matching claims: 0

## Next Steps

- Import or approve the sources you want included.
- Run **Build** so the wiki and indexes are generated.
- Ask a narrower question that matches the current project pages.

## Caveats

This fallback answer is deterministic and intentionally conservative. It will not invent information that is not already in the project.
"""

    wiki_lines = []
    for page in wiki_pages[:5]:
        excerpt = _strip_frontmatter(page["body"]).strip().replace("\n", " ")
        excerpt = re.sub(r"\s+", " ", excerpt)[:220].rstrip()
        wiki_lines.append(f"- [[{page['title']}]] (`{page['id']}`): {excerpt}")

    source_lines = []
    for source in source_summaries[:5]:
        summary = re.sub(r"\s+", " ", (source.get("summary") or "").strip())[:220].rstrip()
        source_lines.append(f"- {source['title'] or source['source_id']} (`{source['source_id']}`): {summary}")

    claim_lines = [
        f"- {claim['text']} (confidence {claim['confidence']:.2f})"
        for claim in claims[:6]
        if claim.get("text")
    ]

    return f"""## Answer

Knowler generated a fallback {task_type} response because the LLM synthesis path was unavailable or unsuitable for this request.

## Evidence From The Current Wiki

{chr(10).join(wiki_lines) or "_No wiki pages matched directly._"}

## Source Summaries

{chr(10).join(source_lines) or "_No source summaries were retrieved._"}

## Key Claims

{chr(10).join(claim_lines) or "_No explicit claims were available._"}

## Caveats

- Fallback reason: {reason}
- This answer is assembled deterministically from existing project evidence.
- It is safer than silence, but less synthesized than the normal LLM-backed answer path.
"""


async def _index_page_content(
    db: Any,
    project_id: str,
    page_id: str,
    title: str,
    content: str,
    source_id: str | None = None,
) -> None:
    await db.execute(
        "DELETE FROM content_units WHERE project_id=? AND parent_kind='page' AND parent_id=?",
        (project_id, page_id),
    )

    unit_id = f"cu_{ULID()}"
    token_count = len(content.split())

    await db.execute(
        """
        INSERT INTO content_units(id, project_id, unit_type, parent_kind, parent_id,
          ordinal, title, body, token_count, source_id, page_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (unit_id, project_id, "page_body", "page", page_id, 0, title, content, token_count, source_id, page_id),
    )
    await db.execute(
        "INSERT INTO content_units_fts(rowid, title, body, project_id) VALUES (last_insert_rowid(), ?, ?, ?)",
        (title, content, project_id),
    )
    await db.commit()


async def _expand_source_ids_from_pages(
    db: Any,
    project_id: str,
    page_ids: list[str],
) -> list[str]:
    if not page_ids:
        return []
    placeholders = ",".join("?" for _ in page_ids)
    rows = await db.fetchall(
        f"""
        SELECT DISTINCT to_id AS source_id
        FROM relations
        WHERE project_id=?
          AND from_kind='page'
          AND relation_type='derived_from'
          AND to_kind='source'
          AND from_id IN ({placeholders})
        """,
        (project_id, *page_ids),
    )
    return [row["source_id"] for row in rows if row["source_id"]]


async def _write_question_page(
    prompt: str,
    artifact_content: str,
    artifact_id: str,
    query_run_id: str,
    project_id: str,
    db: Any,
    vault: Any,
    source_ids: list[str],
    page_ids: list[str],
) -> str:
    """Persist a query result into the wiki as a durable question page."""
    title = prompt[:80].rstrip(".? ") or artifact_id
    slug = _slugify(title)
    wiki_path = vault.wiki_path("question", slug)
    now = _now_iso()

    expanded_source_ids = list(dict.fromkeys(source_ids + await _expand_source_ids_from_pages(db, project_id, page_ids)))
    body = _strip_frontmatter(artifact_content).strip()
    page_id = f"pg_{ULID()}"

    page_content = f"""---
id: "{page_id}"
project_id: "{project_id}"
page_type: "question"
title: "{title}"
source_query_run_id: "{query_run_id}"
answer_artifact_id: "{artifact_id}"
sources_used: {json.dumps(expanded_source_ids)}
pages_used: {json.dumps(page_ids)}
status: "active"
created_at: "{now}"
updated_at: "{now}"
---

## Question

{prompt}

## Answer

{body or "No answer content was generated."}
"""

    existing_page_row = await db.fetchone(
        "SELECT id FROM pages WHERE project_id=? AND page_type='question' AND slug=?",
        (project_id, slug),
    )

    try:
        existing_rev = snapshot(wiki_path) if wiki_path.exists() else None
        write_atomic(wiki_path, page_content, expected_revision=existing_rev, tmp_dir=vault.tmp_dir)
    except ConflictError:
        write_atomic(wiki_path, page_content, tmp_dir=vault.tmp_dir)

    body_sha256 = hashlib.sha256(page_content.encode()).hexdigest()
    if existing_page_row:
        page_id = existing_page_row["id"]
        await db.execute(
            """
            UPDATE pages
            SET title=?, file_path=?, body_sha256=?, status='active',
                updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
            WHERE id=?
            """,
            (title, str(wiki_path), body_sha256, page_id),
        )
    else:
        await db.execute(
            """
            INSERT INTO pages(id, project_id, page_type, title, slug, file_path, frontmatter_json, body_sha256)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                page_id,
                project_id,
                "question",
                title,
                slug,
                str(wiki_path),
                json.dumps({"source_query_run_id": query_run_id, "artifact_id": artifact_id}),
                body_sha256,
            ),
        )

    for source_id in expanded_source_ids:
        await db.execute(
            """
            INSERT OR IGNORE INTO relations(id, project_id, from_kind, from_id, relation_type, to_kind, to_id)
            VALUES (?,?,?,?,?,?,?)
            """,
            (f"rel_{ULID()}", project_id, "page", page_id, "derived_from", "source", source_id),
        )

    for referenced_page_id in page_ids:
        await db.execute(
            """
            INSERT OR IGNORE INTO relations(id, project_id, from_kind, from_id, relation_type, to_kind, to_id)
            VALUES (?,?,?,?,?,?,?)
            """,
            (f"rel_{ULID()}", project_id, "page", page_id, "references", "page", referenced_page_id),
        )

    await db.commit()
    await _index_page_content(db, project_id, page_id, title, page_content)
    return page_id


async def synthesize_artifact(
    prompt: str,
    plan: dict[str, Any],
    evidence: dict[str, Any],
    artifact_id: str,
    query_run_id: str,
    project_id: str,
    llm: LLMProvider,
    tier: str,
) -> str:
    """
    Stage 4: Generate the artifact using the LLM synthesizer.

    Returns the markdown content of the artifact.
    """
    task_type = plan.get("task_type", "report")
    output_format = plan.get("output_format", "markdown_report")

    # Build title
    title = prompt[:80].strip()
    if len(prompt) > 80:
        title += "..."

    source_ids = evidence.get("source_ids", [])
    page_ids = evidence.get("page_ids", [])
    now = _now_iso()

    template = _jinja.get_template("synthesize_report_v1.md")
    synthesis_prompt = template.render(
        prompt=prompt,
        task_type=task_type,
        output_format=output_format,
        wiki_pages=evidence.get("wiki_pages", []),
        source_summaries=evidence.get("source_summaries", []),
        claims=evidence.get("claims", []),
        artifact_id=artifact_id,
        project_id=project_id,
        title=title,
        query_run_id=query_run_id,
        source_ids=source_ids,
        page_ids=page_ids,
        created_at=now,
    )

    resp = await llm.complete(
        system_prompt="",
        user_message=synthesis_prompt,
        tier=tier,  # type: ignore
        max_tokens=2000,
        response_format="text",
    )
    return resp.text.strip()


async def write_artifact(
    content: str,
    artifact_id: str,
    query_run_id: str,
    project_id: str,
    task_type: str,
    title: str,
    db: Any,
    vault: Any,
    source_ids: list[str],
    page_ids: list[str],
) -> dict[str, Any]:
    """
    Stage 5: Write artifact to disk and record in DB.

    Returns artifact metadata dict.
    """
    slug = _slugify(title)
    ext = ".md"
    filename = f"{artifact_id}_{slug}{ext}"
    artifact_path = vault.output_path(task_type, filename)
    artifact_path.parent.mkdir(parents=True, exist_ok=True)

    # Append provenance footer if not already present
    if "## Sources Used" not in content and "## Sources used" not in content:
        source_lines = "\n".join(f"- {sid}" for sid in source_ids[:10])
        page_lines = "\n".join(f"- {pid}" for pid in page_ids[:10])
        now = _now_iso()
        provenance = f"""

---

## Sources Used

{source_lines or "_No sources_"}

## Pages Used

{page_lines or "_No pages_"}

---
*Generated by Knowler at {now}*
"""
        content = content + provenance

    write_atomic(artifact_path, content, tmp_dir=vault.tmp_dir)
    sha256 = hashlib.sha256(content.encode()).hexdigest()

    await db.execute(
        """
        INSERT INTO artifacts(id, project_id, artifact_type, title, file_path, source_query_run_id, metadata_json)
        VALUES (?,?,?,?,?,?,?)
        """,
        (
            artifact_id, project_id, task_type, title, str(artifact_path),
            query_run_id,
            json.dumps({"source_ids": source_ids, "page_ids": page_ids, "sha256": sha256}),
        ),
    )

    # Update query run with artifact id
    await db.execute(
        "UPDATE query_runs SET result_artifact_id=?, status='succeeded', finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
        (artifact_id, query_run_id),
    )
    await db.commit()

    # Index artifact for FTS
    unit_id = f"cu_{ULID()}"
    await db.execute(
        """
        INSERT INTO content_units(id, project_id, unit_type, parent_kind, parent_id, ordinal, title, body, artifact_id)
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (unit_id, project_id, "artifact_body", "artifact", artifact_id, 0, title, content[:5000], artifact_id),
    )
    await db.execute(
        "INSERT INTO content_units_fts(rowid, title, body, project_id) VALUES (last_insert_rowid(), ?, ?, ?)",
        (title, content[:5000], project_id),
    )
    await db.commit()

    log.info("artifact_written", artifact_id=artifact_id, path=str(artifact_path))

    return {
        "artifact_id": artifact_id,
        "artifact_path": str(artifact_path),
        "title": title,
        "artifact_type": task_type,
    }


async def handle_query(ctx: JobContext) -> dict[str, Any]:
    """
    Job handler for query.run.

    Payload:
      query_run_id: str
      prompt: str
      task_type: str
      output_format: str
      model_tier: str
      file_back: bool
    """
    payload = ctx.payload
    query_run_id = payload.get("query_run_id")
    prompt = payload.get("prompt", "")
    task_type_hint = payload.get("task_type", "auto")
    output_format_hint = payload.get("output_format", "markdown_report")
    tier = payload.get("model_tier", "balanced")
    file_back = bool(payload.get("file_back", True))

    if not prompt:
        raise ValueError("prompt is required")

    project = await ctx.app_ctx.project_manager.get_project(ctx.project_id)
    db = project.db
    vault = project.vault
    llm = ctx.app_ctx.llm

    # Stage 1: Intent classification
    await ctx.log_event("info", "step_started", "Planning query")
    if hasattr(ctx.app_ctx, "router"):
        await ctx.app_ctx.router.emit("query.phase_changed", {"query_run_id": query_run_id, "phase": "planning"})

    plan = await classify_intent(prompt, task_type_hint, output_format_hint, llm)

    # Update query run with plan
    if query_run_id:
        await db.execute(
            "UPDATE query_runs SET plan_json=?, status='gathering' WHERE id=?",
            (json.dumps(plan), query_run_id),
        )
        await db.commit()

    # Stage 2+3: Evidence gathering
    await ctx.log_event("info", "step_started", "Gathering evidence")
    if hasattr(ctx.app_ctx, "router"):
        await ctx.app_ctx.router.emit("query.phase_changed", {"query_run_id": query_run_id, "phase": "retrieving"})

    evidence = await gather_evidence(db, ctx.project_id, plan)

    # Stage 4: Synthesis
    await ctx.log_event("info", "step_started", "Synthesizing artifact")
    if hasattr(ctx.app_ctx, "router"):
        await ctx.app_ctx.router.emit("query.phase_changed", {"query_run_id": query_run_id, "phase": "synthesizing"})

    artifact_id = f"art_{ULID()}"
    title = prompt[:80].rstrip(".? ")

    try:
        if not evidence["wiki_pages"] and not evidence["source_summaries"] and not evidence["claims"]:
            raise LLMError("No matching evidence available for this query")
        content = await synthesize_artifact(
            prompt=prompt,
            plan=plan,
            evidence=evidence,
            artifact_id=artifact_id,
            query_run_id=query_run_id or "",
            project_id=ctx.project_id,
            llm=llm,
            tier=tier,
        )
    except LLMError as exc:
        log.warning("query_synthesis_fallback", error=str(exc), query_run_id=query_run_id)
        await ctx.log_event("warn", "log", f"Using deterministic fallback answer: {exc}")
        content = _render_fallback_artifact(prompt, plan, evidence, reason=str(exc))

    # Stage 5: File back
    await ctx.log_event("info", "step_started", "Writing artifact")
    if hasattr(ctx.app_ctx, "router"):
        await ctx.app_ctx.router.emit("query.phase_changed", {"query_run_id": query_run_id, "phase": "writing"})

    artifact_meta = await write_artifact(
        content=content,
        artifact_id=artifact_id,
        query_run_id=query_run_id or "",
        project_id=ctx.project_id,
        task_type=plan["task_type"],
        title=title,
        db=db,
        vault=vault,
        source_ids=evidence.get("source_ids", []),
        page_ids=evidence.get("page_ids", []),
    )

    question_page_id: str | None = None
    if file_back and (evidence.get("source_ids") or evidence.get("page_ids")):
        question_page_id = await _write_question_page(
            prompt=prompt,
            artifact_content=content,
            artifact_id=artifact_id,
            query_run_id=query_run_id or "",
            project_id=ctx.project_id,
            db=db,
            vault=vault,
            source_ids=evidence.get("source_ids", []),
            page_ids=evidence.get("page_ids", []),
        )
        await build_project_indexes(ctx.project_id, db, vault)

    if hasattr(ctx.app_ctx, "router"):
        await ctx.app_ctx.router.emit("artifact.created", {
            "artifact_id": artifact_id,
            "project_id": ctx.project_id,
            "title": title,
            "query_run_id": query_run_id,
            "question_page_id": question_page_id,
        })

    await ctx.log_event("info", "step_finished", f"Artifact created: {title[:60]}")
    await rebuild_project_log(ctx.project_id, db, vault)
    return {**artifact_meta, "question_page_id": question_page_id}
