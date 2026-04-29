"""
Knowledge compiler pipeline.

Generates and maintains wiki pages from normalized sources.

Compile flow:
1. For each normalized source that hasn't been compiled:
   a. Generate a source summary page (wiki/sources/<slug>.md)
2. For each significant entity with enough supporting sources:
   a. Generate or update a concept page (wiki/concepts/<slug>.md)
3. Update index pages
4. Update backlinks (relations table)

Architecture: §18, §42.4
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
from knowler_engine.llm.provider import LLMError, LLMProvider
from knowler_engine.storage.atomic import ConflictError, snapshot, write_atomic
from knowler_engine.system_pages import build_project_indexes, rebuild_project_log
from knowler_engine.graph import export_knowledge_graph
from knowler_engine.text_heuristics import (
    choose_document_title,
    derive_key_claims,
    derive_key_concepts,
    extract_abstract_or_excerpt,
)

log = structlog.get_logger(__name__)

_PROMPTS_DIR = pathlib.Path(__file__).parent.parent.parent / "prompts"
_jinja = Environment(loader=FileSystemLoader(str(_PROMPTS_DIR)), autoescape=False)

# Minimum sources needed before creating a concept page
_MIN_SOURCES_FOR_CONCEPT = 1

# Max sources to include in concept page context
_MAX_SOURCES_PER_CONCEPT = 5


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _slugify(text: str) -> str:
    s = text.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")[:80]


def _extract_frontmatter_and_body(markdown: str) -> tuple[str, str]:
    """Split --- frontmatter --- from body. Returns (frontmatter_block, body)."""
    if not markdown.startswith("---"):
        return "", markdown
    end = markdown.find("---", 3)
    if end < 0:
        return "", markdown
    return markdown[3:end].strip(), markdown[end + 3:].strip()


def _read_source_text(source: dict[str, Any]) -> str:
    from knowler_engine.text_heuristics import clean_extracted_text

    raw_path_str = source.get("raw_path") or ""
    raw_path = pathlib.Path(raw_path_str) if raw_path_str else None
    meta = json.loads(source.get("metadata_json") or "{}")

    extracted_text_path = meta.get("extracted_text_path")

    # 1. Try the stored extracted_text_path
    if extracted_text_path and pathlib.Path(extracted_text_path).exists():
        raw = pathlib.Path(extracted_text_path).read_text(encoding="utf-8", errors="replace")
        return clean_extracted_text(raw)

    # 2. For PDFs: look for a co-located *_text.md (same dir, source_id prefix)
    source_id = source.get("id", "")
    if raw_path and raw_path.exists() and source_id:
        sibling = raw_path.parent / f"{source_id}_text.md"
        if sibling.exists():
            raw = sibling.read_text(encoding="utf-8", errors="replace")
            return clean_extracted_text(raw)

    # 3. Fallback: plain text / markdown files (not PDF)
    if raw_path and raw_path.exists():
        try:
            raw = raw_path.read_text(encoding="utf-8", errors="replace")
            return clean_extracted_text(raw)
        except Exception:
            pass

    return ""


async def compile_source_summary(
    source_id: str,
    project_id: str,
    db: Any,
    vault: Any,
    llm: LLMProvider,
    tier: str = "balanced",
) -> str | None:
    """
    Compile a source summary wiki page.

    Returns the page_id, or None on failure.
    """
    source = await db.fetchone(
        "SELECT * FROM sources WHERE id=? AND project_id=?",
        (source_id, project_id),
    )
    if not source:
        return None

    source = dict(source)
    meta = json.loads(source.get("metadata_json") or "{}")
    source_text = _read_source_text(source)
    title = choose_document_title(source.get("title"), source_text, fallback=source.get("title"))
    summary = meta.get("summary") or extract_abstract_or_excerpt(source_text, max_chars=800) or title

    # Get entities for this source
    entities_rows = await db.fetchall(
        """
        SELECT e.entity_type, e.display_name as name
        FROM entities e
        WHERE e.project_id=? AND e.created_from_source_id=?
        ORDER BY e.confidence DESC LIMIT 10
        """,
        (project_id, source_id),
    )
    entities = [dict(r) for r in entities_rows]

    # Get claims for this source
    claims_rows = await db.fetchall(
        """
        SELECT claim_text as text, claim_kind, confidence
        FROM claims WHERE project_id=? AND created_from_source_id=?
        ORDER BY confidence DESC LIMIT 8
        """,
        (project_id, source_id),
    )
    claims = [dict(r) for r in claims_rows]

    # Build page metadata
    title = title or f"Source {source_id[:12]}"
    slug = _slugify(title)
    page_id = f"pg_{ULID()}"
    now = _now_iso()
    url = source.get("canonical_url") or ""

    # Prefer a source-linked page lookup so title/slug improvements do not create duplicates.
    existing_page_row = await db.fetchone(
        """
        SELECT p.id, p.file_path, p.body_sha256, p.slug
        FROM pages p
        LEFT JOIN relations r
          ON r.project_id = p.project_id
         AND r.from_kind = 'page'
         AND r.from_id = p.id
         AND r.relation_type = 'derived_from'
         AND r.to_kind = 'source'
        WHERE p.project_id=? AND p.page_type='source_summary'
          AND (r.to_id=? OR p.slug=?)
        LIMIT 1
        """,
        (project_id, source_id, slug),
    )

    wiki_path = vault.wiki_path("source_summary", slug)

    # Render prompt
    template = _jinja.get_template("compile_source_summary_v1.md")
    prompt = template.render(
        source_id=source_id,
        page_id=page_id,
        project_id=project_id,
        source_type=source.get("source_type", "unknown"),
        title=title,
        url=url,
        domain=source.get("domain", ""),
        source_date=source.get("source_date", ""),
        trust_level=source.get("trust_level", "unknown"),
        summary=summary,
        entities=entities,
        claims=claims,
        created_at=now,
        updated_at=now,
    )

    try:
        if not llm.is_configured():
            raise LLMError("LLM provider is not configured")
        llm_resp = await llm.complete(
            system_prompt="",
            user_message=prompt,
            tier=tier,
            max_tokens=1200,
            response_format="text",
        )
        page_content = llm_resp.text.strip()
    except LLMError as exc:
        log.error("compile_source_summary_llm_failed", source_id=source_id, error=str(exc))
        # Fallback: generate a useful page without LLM.
        page_content = _minimal_source_summary_page(
            page_id, project_id, source_id, source, title, summary, entities, claims, source_text, now
        )

    # Write atomically
    try:
        existing_rev = snapshot(wiki_path) if wiki_path.exists() else None
        write_atomic(wiki_path, page_content, expected_revision=existing_rev, tmp_dir=vault.tmp_dir)
    except ConflictError as exc:
        log.warning("compile_source_summary_conflict", path=str(wiki_path), error=str(exc))
        return None

    body_sha256 = hashlib.sha256(page_content.encode()).hexdigest()

    # Upsert page record
    if existing_page_row:
        old_path = pathlib.Path(existing_page_row["file_path"]) if existing_page_row["file_path"] else None
        if old_path and old_path != wiki_path and old_path.exists():
            old_path.unlink()
        await db.execute(
            """
            UPDATE pages SET title=?, slug=?, file_path=?, body_sha256=?, status='active',
            updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?
            """,
            (title, slug, str(wiki_path), body_sha256, existing_page_row["id"]),
        )
        page_id = existing_page_row["id"]
    else:
        await db.execute(
            """
            INSERT INTO pages(id, project_id, page_type, title, slug, file_path,
              frontmatter_json, body_sha256)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                page_id, project_id, "source_summary", title, slug, str(wiki_path),
                json.dumps({"source_id": source_id}), body_sha256,
            ),
        )

    # Record relation: page derived_from source
    rel_id = f"rel_{ULID()}"
    await db.execute(
        """
        INSERT OR IGNORE INTO relations(id, project_id, from_kind, from_id, relation_type, to_kind, to_id)
        VALUES (?,?,?,?,?,?,?)
        """,
        (rel_id, project_id, "page", page_id, "derived_from", "source", source_id),
    )

    # Update source status
    await db.execute(
        "UPDATE sources SET title=?, status='compiled', updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
        (title, source_id),
    )

    await db.commit()

    # Index in content_units for FTS
    await _index_page_content(db, project_id, page_id, title, page_content, source_id=source_id)

    log.info("source_summary_compiled", source_id=source_id, page_id=page_id)
    return page_id


def _minimal_source_summary_page(
    page_id: str,
    project_id: str,
    source_id: str,
    source: dict,
    title: str,
    summary: str,
    entities: list,
    claims: list,
    source_text: str,
    now: str,
) -> str:
    """Generate a minimal source summary page without LLM."""
    from knowler_engine.text_heuristics import is_placeholder_title

    url = source.get("canonical_url") or ""
    abstract = summary or extract_abstract_or_excerpt(source_text, max_chars=800)
    # If we still have no abstract and title is a real title (not an arxiv ID), use the title.
    # Avoid using a placeholder filename as the abstract — it produces confusing pages.
    if not abstract and not is_placeholder_title(title):
        abstract = title
    if not abstract:
        abstract = "Text extraction unavailable for this source."

    concept_lines = [f"- [[{e['name']}]]" for e in entities if e.get("name")]
    if not concept_lines:
        concept_lines = [f"- [[{concept}]]" for concept in derive_key_concepts(title, abstract)]
    claim_lines = [f"- {c['text']}" for c in claims[:5] if c.get("text")]
    if not claim_lines:
        claim_lines = [f"- {claim}" for claim in derive_key_claims(abstract)]

    return f"""---
id: "{page_id}"
project_id: "{project_id}"
page_type: "source_summary"
title: "{title}"
source_id: "{source_id}"
source_type: "{source.get('source_type', 'unknown')}"
source_url: "{url}"
status: "active"
created_at: "{now}"
updated_at: "{now}"
---

## Abstract

{abstract}

## Key Concepts

{chr(10).join(concept_lines) or "No concepts extracted."}

## Key Claims

{chr(10).join(claim_lines) or "No claims extracted."}

## Source Details

- Type: {source.get('source_type', 'unknown')}
- URL: {url or 'N/A'}
- Trust level: {source.get('trust_level', 'unknown')}
"""


def _minimal_concept_page(
    page_id: str,
    project_id: str,
    entity_name: str,
    entity_type: str,
    aliases: list[str],
    sources: list[dict[str, Any]],
    related_entities: list[dict[str, Any]],
    source_ids: list[str],
    related_entity_ids: list[str],
    now: str,
) -> str:
    """Generate a deterministic concept page when the LLM is unavailable."""
    summaries = [summary for summary in (s.get("summary", "").strip() for s in sources) if summary]

    overview = summaries[0] if summaries else f"[[{entity_name}]] is a {entity_type} mentioned in the project sources."
    claim_texts: list[str] = []
    entity_lower = entity_name.lower()
    for source in sources:
        for claim in source.get("claims", []):
            text = (claim.get("text") or "").strip()
            if not text:
                continue
            if entity_lower in text.lower() or not claim_texts:
                if text not in claim_texts:
                    claim_texts.append(text)
            if len(claim_texts) >= 4:
                break
        if len(claim_texts) >= 4:
            break
    if not claim_texts and summaries:
        claim_texts = derive_key_claims(" ".join(summaries), limit=3)

    why_it_matters = (
        claim_texts[0]
        if claim_texts
        else f"[[{entity_name}]] appears in {len(sources)} supporting source{'s' if len(sources) != 1 else ''}, so it is part of the project's active knowledge graph."
    )

    relationship_lines = []
    for rel in related_entities[:6]:
        if rel["from_name"] == entity_name:
            relationship_lines.append(f"- [[{entity_name}]] {rel['relation_type']} [[{rel['to_name']}]]")
        elif rel["to_name"] == entity_name:
            relationship_lines.append(f"- [[{rel['from_name']}]] {rel['relation_type']} [[{entity_name}]]")
    if not relationship_lines:
        relationship_lines = ["- No explicit graph relationships extracted yet."]

    supporting_lines = []
    for source in sources:
        source_ref = f"[[{source['title']}]]"
        if source["id"] != source["title"]:
            source_ref = f"{source_ref} (`{source['id']}`)"
        supporting_lines.append(f"- {source_ref}")
        for claim in source.get("claims", [])[:2]:
            text = (claim.get("text") or "").strip()
            if text:
                supporting_lines.append(f"- Evidence: {text}")
    if not supporting_lines:
        supporting_lines = ["- No supporting sources linked yet."]

    open_questions = [
        f"- Which other sources in the project materially expand or challenge [[{entity_name}]]?",
        f"- What implementation details or tradeoffs around [[{entity_name}]] are still missing from the current notes?",
    ]
    if related_entities:
        related_names = []
        for rel in related_entities[:2]:
            related_names.append(rel["to_name"] if rel["from_name"] == entity_name else rel["from_name"])
        comparisons = ", ".join(f"[[{name}]]" for name in related_names)
        open_questions.append(f"- How does [[{entity_name}]] compare with {comparisons}?")

    related_page_lines = [f"- [[{name}]]" for name in aliases[:4]]
    seen_related = {alias.lower() for alias in aliases}
    for rel in related_entities[:6]:
        other_name = rel["to_name"] if rel["from_name"] == entity_name else rel["from_name"]
        if other_name.lower() not in seen_related:
            related_page_lines.append(f"- [[{other_name}]]")
            seen_related.add(other_name.lower())
    for source in sources[:4]:
        if source["title"].lower() not in seen_related:
            related_page_lines.append(f"- [[{source['title']}]]")
            seen_related.add(source["title"].lower())
    if not related_page_lines:
        related_page_lines = ["- No related pages available yet."]

    return f"""---
id: "{page_id}"
project_id: "{project_id}"
page_type: "concept"
title: "{entity_name}"
derived_from: {json.dumps(source_ids)}
related_entities: {json.dumps(related_entity_ids)}
status: "active"
created_at: "{now}"
updated_at: "{now}"
---

## Overview

{overview}

## Why It Matters

{why_it_matters}

## Key Relationships

{chr(10).join(relationship_lines)}

## Supporting Sources

{chr(10).join(supporting_lines)}

## Open Questions

{chr(10).join(open_questions[:3])}

## Related Pages

{chr(10).join(related_page_lines)}
"""


async def compile_concept_page(
    entity_id: str,
    project_id: str,
    db: Any,
    vault: Any,
    llm: LLMProvider,
    tier: str = "balanced",
) -> str | None:
    """
    Compile or update a concept page for an entity.

    Returns the page_id, or None if skipped/failed.
    """
    entity = await db.fetchone(
        "SELECT * FROM entities WHERE id=? AND project_id=?",
        (entity_id, project_id),
    )
    if not entity:
        return None

    entity = dict(entity)
    entity_name = entity["display_name"]

    # Find supporting sources via mentions relation and direct creation
    source_ids_rows = await db.fetchall(
        """
        SELECT DISTINCT s.id as source_id, s.title, s.canonical_url,
               COALESCE(json_extract(s.metadata_json, '$.summary'), '') as summary
        FROM sources s
        WHERE s.project_id=? AND s.deleted_at IS NULL
          AND (
            s.id = ? OR
            s.id IN (
              SELECT evidence_source_id FROM relations
              WHERE project_id=? AND to_kind='entity' AND to_id=? AND from_kind='source'
            )
          )
        ORDER BY s.created_at DESC LIMIT ?
        """,
        (project_id, entity.get("created_from_source_id", ""), project_id, entity_id, _MAX_SOURCES_PER_CONCEPT),
    )

    sources = []
    source_ids = []
    for row in source_ids_rows:
        sid = row["source_id"]
        source_ids.append(sid)
        claims_rows = await db.fetchall(
            "SELECT claim_text as text, claim_kind, confidence FROM claims WHERE project_id=? AND created_from_source_id=? ORDER BY confidence DESC LIMIT 5",
            (project_id, sid),
        )
        sources.append({
            "id": sid,
            "title": row["title"] or sid,
            "summary": row["summary"],
            "claims": [dict(r) for r in claims_rows],
        })

    if not sources and not entity.get("description"):
        log.debug("concept_compile_skipped_no_sources", entity_id=entity_id)
        return None

    # Find related entities (1-hop)
    related_rows = await db.fetchall(
        """
        SELECT r.from_id, r.to_id, r.relation_type,
               e1.display_name as from_name, e2.display_name as to_name
        FROM relations r
        JOIN entities e1 ON e1.id = r.from_id
        JOIN entities e2 ON e2.id = r.to_id
        WHERE r.project_id=? AND (r.from_id=? OR r.to_id=?)
          AND r.from_kind='entity' AND r.to_kind='entity'
        LIMIT 10
        """,
        (project_id, entity_id, entity_id),
    )
    related_entities = [dict(r) for r in related_rows]
    related_entity_ids = list({r["from_id"] for r in related_entities} | {r["to_id"] for r in related_entities} - {entity_id})

    slug = _slugify(entity_name)
    wiki_path = vault.wiki_path("concept", slug)
    page_id = f"pg_{ULID()}"
    now = _now_iso()

    aliases = json.loads(entity.get("aliases_json") or "[]")

    # Check for existing page
    existing_page_row = await db.fetchone(
        "SELECT id, file_path, body_sha256 FROM pages WHERE project_id=? AND page_type='concept' AND slug=?",
        (project_id, slug),
    )
    existing_content = wiki_path.read_text(encoding="utf-8") if wiki_path.exists() else None

    template = _jinja.get_template("compile_concept_page_v1.md")
    prompt = template.render(
        entity_name=entity_name,
        entity_type=entity["entity_type"],
        aliases=aliases,
        sources=sources,
        related_entities=related_entities,
        existing_page=existing_content,
        page_id=page_id,
        project_id=project_id,
        source_ids=source_ids,
        related_entity_ids=related_entity_ids,
        created_at=now,
        updated_at=now,
    )

    try:
        if not llm.is_configured():
            raise LLMError("LLM provider is not configured")
        llm_resp = await llm.complete(
            system_prompt="",
            user_message=prompt,
            tier=tier,
            max_tokens=1500,
            response_format="text",
        )
        page_content = llm_resp.text.strip()
    except LLMError as exc:
        log.error("compile_concept_llm_failed", entity_id=entity_id, error=str(exc))
        page_content = _minimal_concept_page(
            page_id=page_id,
            project_id=project_id,
            entity_name=entity_name,
            entity_type=entity["entity_type"],
            aliases=aliases,
            sources=sources,
            related_entities=related_entities,
            source_ids=source_ids,
            related_entity_ids=related_entity_ids,
            now=now,
        )

    # Write atomically
    try:
        existing_rev = snapshot(wiki_path) if wiki_path.exists() else None
        write_atomic(wiki_path, page_content, expected_revision=existing_rev, tmp_dir=vault.tmp_dir)
    except ConflictError as exc:
        log.warning("compile_concept_conflict", path=str(wiki_path), error=str(exc))
        return None

    body_sha256 = hashlib.sha256(page_content.encode()).hexdigest()

    if existing_page_row:
        await db.execute(
            "UPDATE pages SET body_sha256=?, status='active', updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
            (body_sha256, existing_page_row["id"]),
        )
        page_id = existing_page_row["id"]
    else:
        await db.execute(
            """
            INSERT INTO pages(id, project_id, page_type, title, slug, file_path, frontmatter_json, body_sha256)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                page_id, project_id, "concept", entity_name, slug, str(wiki_path),
                json.dumps({"entity_id": entity_id, "source_ids": source_ids}),
                body_sha256,
            ),
        )

    # Record relation: page about entity
    rel_id = f"rel_{ULID()}"
    await db.execute(
        "INSERT OR IGNORE INTO relations(id, project_id, from_kind, from_id, relation_type, to_kind, to_id) VALUES (?,?,?,?,?,?,?)",
        (rel_id, project_id, "page", page_id, "about", "entity", entity_id),
    )

    await db.commit()

    # Index content
    await _index_page_content(db, project_id, page_id, entity_name, page_content)

    log.info("concept_page_compiled", entity_id=entity_id, page_id=page_id)
    return page_id


async def _index_page_content(
    db: Any,
    project_id: str,
    page_id: str,
    title: str,
    content: str,
    source_id: str | None = None,
) -> None:
    """Add or update a content_unit and FTS entry for a page."""
    # Remove stale FTS entries before deleting content_units (rowid link would become dangling)
    await db.execute(
        """
        DELETE FROM content_units_fts WHERE rowid IN (
            SELECT rowid FROM content_units
            WHERE project_id=? AND parent_kind='page' AND parent_id=?
        )
        """,
        (project_id, page_id),
    )
    # Remove existing units for this page
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

    # Update FTS
    await db.execute(
        "INSERT INTO content_units_fts(rowid, title, body, project_id) VALUES (last_insert_rowid(), ?, ?, ?)",
        (title, content, project_id),
    )

    await db.commit()


async def build_indexes(
    project_id: str,
    db: Any,
    vault: Any,
) -> None:
    """Build/rebuild the wiki index pages (topic indexes, concept list)."""
    await build_project_indexes(project_id, db, vault)


async def handle_compile_project(ctx: JobContext) -> dict[str, Any]:
    """
    Job handler: compile all uncompiled sources and update concept pages.

    Payload:
      scope: 'incremental' | 'full'
    """
    scope = ctx.payload.get("scope", "incremental")
    project = await ctx.app_ctx.project_manager.get_project(ctx.project_id)
    db = project.db
    vault = project.vault
    llm = ctx.app_ctx.llm
    tier = project.config.get("default_model_tier", "balanced")

    await ctx.set_progress(0.02, "Starting compilation")
    await ctx.log_event("info", "step_started", "Starting compilation")

    # Step 1: Compile source summaries for normalized sources
    if scope == "incremental":
        sources = await db.fetchall(
            "SELECT id FROM sources WHERE project_id=? AND status='normalized' AND deleted_at IS NULL",
            (ctx.project_id,),
        )
    else:
        sources = await db.fetchall(
            "SELECT id FROM sources WHERE project_id=? AND status IN ('normalized','compiled') AND deleted_at IS NULL",
            (ctx.project_id,),
        )

    compiled_sources = 0
    total_sources = len(sources)
    for index, row in enumerate(sources, start=1):
        if ctx.cancelled:
            break
        sid = row["id"]
        try:
            page_id = await compile_source_summary(sid, ctx.project_id, db, vault, llm, tier)
            if page_id:
                compiled_sources += 1
        except Exception as exc:
            log.error("compile_source_summary_error", source_id=sid, error=str(exc))
        if total_sources > 0 and (index == total_sources or index == 1 or index % 5 == 0):
            source_progress = 0.08 + (index / total_sources) * 0.34
            await ctx.set_progress(
                source_progress,
                f"Compiled {index}/{total_sources} source summaries",
            )

    await ctx.set_progress(0.44, f"Compiled {compiled_sources} source summaries")
    await ctx.log_event("info", "log", f"Compiled {compiled_sources} source summaries")

    # Step 2: Compile concept pages for entities with enough support
    entities = await db.fetchall(
        "SELECT id FROM entities WHERE project_id=? ORDER BY confidence DESC",
        (ctx.project_id,),
    )

    compiled_concepts = 0
    total_entities = len(entities)
    if total_entities > 0:
        await ctx.set_progress(0.48, f"Compiling concept pages for {total_entities} entities")
    for index, row in enumerate(entities, start=1):
        if ctx.cancelled:
            break
        eid = row["id"]
        try:
            page_id = await compile_concept_page(eid, ctx.project_id, db, vault, llm, tier)
            if page_id:
                compiled_concepts += 1
        except Exception as exc:
            log.error("compile_concept_error", entity_id=eid, error=str(exc))
        if total_entities > 0 and (index == total_entities or index == 1 or index % 10 == 0):
            concept_progress = 0.48 + (index / total_entities) * 0.42
            await ctx.set_progress(
                concept_progress,
                f"Processed {index}/{total_entities} concept pages",
            )

    await ctx.set_progress(0.92, f"Compiled {compiled_concepts} concept pages")
    await ctx.log_event("info", "log", f"Compiled {compiled_concepts} concept pages")

    # Step 3: Rebuild indexes
    await ctx.set_progress(0.96, "Rebuilding index and log pages")
    await build_indexes(ctx.project_id, db, vault)
    await rebuild_project_log(ctx.project_id, db, vault)
    await ctx.set_progress(0.985, "Writing graph exports")
    graph_export = await export_knowledge_graph(db, ctx.project_id, vault)
    await ctx.log_event(
        "info",
        "log",
        f"Wrote graph exports to {graph_export['graph_path']} and {graph_export['report_path']}",
    )

    await ctx.set_progress(1.0, "Compilation complete")
    await ctx.log_event("info", "step_finished", "Compilation complete")

    return {
        "compiled_sources": compiled_sources,
        "compiled_concepts": compiled_concepts,
        "scope": scope,
        "graph_path": graph_export["graph_path"],
        "graph_report_path": graph_export["report_path"],
    }
