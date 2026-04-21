"""
Normalization pipeline.

Converts raw source text into structured metadata using LLM-assisted extraction.
Stores results in:
  - entities table
  - claims table
  - relations table
  - source_versions table
  - normalized/sources/<source_id>.json (filesystem)

Architecture §16.3, §42.3, §38.2

Non-LLM: deterministic metadata extraction (title, url, domain, checksum)
LLM: semantic extraction (entities, claims, relations, summary)
"""
from __future__ import annotations

import json
import pathlib
from typing import Any

import structlog
from jinja2 import Environment, FileSystemLoader
from ulid import ULID

from knowler_engine.jobs.types import JobContext
from knowler_engine.llm.provider import LLMError, LLMProvider, parse_llm_json
from knowler_engine.storage.atomic import write_atomic
from knowler_engine.text_heuristics import (
    choose_document_title,
    derive_key_claims,
    derive_key_concepts,
    extract_abstract_or_excerpt,
)

log = structlog.get_logger(__name__)

_PROMPTS_DIR = pathlib.Path(__file__).parent.parent.parent / "prompts"
_jinja = Environment(loader=FileSystemLoader(str(_PROMPTS_DIR)), autoescape=False)

# Max characters of extracted text to send to LLM
_TEXT_EXCERPT_CHARS = 12000

# Validation rules (from architecture §42.3)
_VALID_ENTITY_TYPES = {"concept", "person", "org", "method", "dataset", "tool", "topic"}
_VALID_CLAIM_KINDS = {"fact", "comparison", "definition", "open_question", "recommendation"}


def _read_source_text(source_row: dict[str, Any]) -> str:
    """Read and return extractable text from a source record."""
    from knowler_engine.text_heuristics import clean_extracted_text

    raw_path = source_row.get("raw_path", "")
    p = pathlib.Path(raw_path) if raw_path else None
    meta = json.loads(source_row.get("metadata_json") or "{}")

    extracted_text_path = meta.get("extracted_text_path")

    # 1. Try the stored extracted_text_path
    if extracted_text_path and pathlib.Path(extracted_text_path).exists():
        raw = pathlib.Path(extracted_text_path).read_text(encoding="utf-8", errors="replace")
        return clean_extracted_text(raw)[:_TEXT_EXCERPT_CHARS]

    # 2. For PDFs: look for a co-located *_text.md file (same dir, same source_id prefix)
    source_id = source_row.get("id", "")
    if p and p.exists() and source_id:
        sibling = p.parent / f"{source_id}_text.md"
        if sibling.exists():
            raw = sibling.read_text(encoding="utf-8", errors="replace")
            return clean_extracted_text(raw)[:_TEXT_EXCERPT_CHARS]

    # 3. Fallback: try reading as plain text (works for .md / .txt, not PDF)
    if p and p.exists():
        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
            return clean_extracted_text(raw)[:_TEXT_EXCERPT_CHARS]
        except Exception:
            pass

    return ""


def _fallback_normalization(source_row: dict[str, Any], text: str) -> dict[str, Any]:
    title = choose_document_title(source_row.get("title"), text, fallback=source_row.get("title"))
    summary = extract_abstract_or_excerpt(text, max_chars=800) or title or "Normalization unavailable"
    entities = [
        {
            "name": concept,
            "entity_type": "concept",
            "aliases": [],
            "confidence": 0.35,
        }
        for concept in derive_key_concepts(title, summary)
    ]
    claims = [
        {
            "text": claim,
            "claim_kind": "fact",
            "confidence": 0.3,
        }
        for claim in derive_key_claims(summary)
    ]
    return {
        "summary": summary,
        "entities": entities,
        "claims": claims,
        "relations": [],
        "topic_labels": [],
        "quality_flags": ["deterministic_fallback"],
    }


def _validate_normalization(data: dict) -> dict:
    """
    Validate and clean the LLM normalization output.
    Returns cleaned data or raises ValueError.
    Architecture §42.3 validation rules.
    """
    if not isinstance(data.get("summary"), str) or not data["summary"].strip():
        raise ValueError("summary is required")
    data["summary"] = data["summary"][:800]

    # Clean entities
    clean_entities = []
    for e in data.get("entities", []):
        if not isinstance(e.get("name"), str) or not e["name"].strip():
            continue
        et = e.get("entity_type", "concept")
        if et not in _VALID_ENTITY_TYPES:
            et = "concept"
        conf = float(e.get("confidence", 0.8))
        conf = max(0.0, min(1.0, conf))
        clean_entities.append({
            "name": e["name"].strip()[:200],
            "entity_type": et,
            "aliases": [str(a) for a in e.get("aliases", [])[:5]],
            "confidence": conf,
        })
    data["entities"] = clean_entities[:15]

    # Clean claims
    clean_claims = []
    for c in data.get("claims", []):
        if not isinstance(c.get("text"), str) or not c["text"].strip():
            continue
        if len(c["text"]) > 400:
            continue
        ck = c.get("claim_kind", "fact")
        if ck not in _VALID_CLAIM_KINDS:
            ck = "fact"
        conf = float(c.get("confidence", 0.5))
        conf = max(0.0, min(1.0, conf))
        clean_claims.append({
            "text": c["text"].strip(),
            "claim_kind": ck,
            "confidence": conf,
        })
    data["claims"] = clean_claims[:10]

    # Clean relations
    clean_relations = []
    for r in data.get("relations", []):
        if not isinstance(r.get("from_name"), str) or not r["from_name"].strip():
            continue
        if not isinstance(r.get("to_name"), str) or not r["to_name"].strip():
            continue
        conf = float(r.get("confidence", 0.8))
        conf = max(0.0, min(1.0, conf))
        clean_relations.append({
            "from_name": r["from_name"].strip()[:200],
            "relation_type": r.get("relation_type", "related_to")[:50],
            "to_name": r["to_name"].strip()[:200],
            "confidence": conf,
        })
    data["relations"] = clean_relations[:10]

    data["topic_labels"] = [str(t)[:50] for t in data.get("topic_labels", [])[:6]]
    data["quality_flags"] = [str(f)[:50] for f in data.get("quality_flags", [])[:4]]

    return data


async def normalize_source(
    source_id: str,
    project_id: str,
    db: Any,
    vault: Any,
    llm: LLMProvider,
    tier: str = "balanced",
) -> dict[str, Any]:
    """
    Normalize a single source: run LLM extraction, store results.

    Returns the normalized data dict.
    """
    # Load source record
    row = await db.fetchone(
        "SELECT * FROM sources WHERE id=? AND project_id=?",
        (source_id, project_id),
    )
    if not row:
        raise ValueError(f"Source {source_id} not found")

    source_row = dict(row)
    text = _read_source_text(source_row)

    resolved_title = choose_document_title(source_row.get("title"), text, fallback=source_row.get("title"))
    if resolved_title and resolved_title != source_row.get("title"):
        source_row["title"] = resolved_title

    if not text.strip():
        log.warning("normalize_source_no_text", source_id=source_id)
        # Store minimal normalization
        normalized = {
            "summary": source_row.get("title") or "No extractable text",
            "entities": [],
            "claims": [],
            "relations": [],
            "topic_labels": [],
            "quality_flags": ["no_text"],
        }
    else:
        # Render prompt template
        template = _jinja.get_template("normalize_source_v1.md")
        system_prompt = template.render(
            source_type=source_row.get("source_type", "unknown"),
            title=source_row.get("title", ""),
            url=source_row.get("canonical_url", ""),
            domain=source_row.get("domain", ""),
            text_excerpt=text,
        )

        try:
            if not llm.is_configured():
                raise LLMError("LLM provider is not configured")

            # Call LLM
            llm_resp = await llm.complete(
                system_prompt="",
                user_message=system_prompt,
                tier=tier,
                max_tokens=2000,
                response_format="json",
            )

            # Parse and validate
            try:
                raw_data = parse_llm_json(llm_resp.text)
                normalized = _validate_normalization(raw_data)
            except (json.JSONDecodeError, ValueError, KeyError) as exc:
                log.warning("normalization_validation_failed_retrying", source_id=source_id, error=str(exc))
                repair_prompt = (
                    f"The previous response failed validation with error: {exc}\n"
                    f"Previous response:\n{llm_resp.text[:2000]}\n\n"
                    "Please return ONLY valid JSON matching the required schema. No markdown."
                )
                repair_resp = await llm.complete(
                    system_prompt="",
                    user_message=repair_prompt,
                    tier="fast",  # type: ignore
                    max_tokens=2000,
                    response_format="json",
                )
                raw_data = parse_llm_json(repair_resp.text)
                normalized = _validate_normalization(raw_data)

        except Exception as exc:
            # Keep the source moving through the pipeline even when the LLM is unavailable.
            log.warning("normalization_llm_failed_using_fallback", source_id=source_id, error=str(exc))
            normalized = _fallback_normalization(source_row, text)
            normalized["quality_flags"].append("normalization_failed")

    # Persist entities
    for entity in normalized["entities"]:
        entity_id = f"ent_{ULID()}"
        canonical = entity["name"].lower().strip()
        try:
            await db.execute(
                """
                INSERT OR IGNORE INTO entities(
                  id, project_id, entity_type, canonical_name, display_name,
                  aliases_json, confidence, created_from_source_id
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    entity_id, project_id, entity["entity_type"],
                    canonical, entity["name"],
                    json.dumps(entity["aliases"]),
                    entity["confidence"], source_id,
                ),
            )
        except Exception:
            pass  # Unique constraint on (project_id, entity_type, canonical_name)

    await db.commit()

    # Persist claims
    for claim in normalized["claims"]:
        claim_id = f"clm_{ULID()}"
        await db.execute(
            """
            INSERT INTO claims(
              id, project_id, claim_text, claim_kind, confidence, created_from_source_id
            ) VALUES (?,?,?,?,?,?)
            """,
            (claim_id, project_id, claim["text"], claim["claim_kind"], claim["confidence"], source_id),
        )
    await db.commit()

    # Persist relations between entities
    for rel in normalized["relations"]:
        from_canonical = rel["from_name"].lower().strip()
        to_canonical = rel["to_name"].lower().strip()

        from_ent = await db.fetchone(
            "SELECT id FROM entities WHERE project_id=? AND canonical_name=? LIMIT 1",
            (project_id, from_canonical),
        )
        to_ent = await db.fetchone(
            "SELECT id FROM entities WHERE project_id=? AND canonical_name=? LIMIT 1",
            (project_id, to_canonical),
        )

        if from_ent and to_ent:
            rel_id = f"rel_{ULID()}"
            await db.execute(
                """
                INSERT INTO relations(
                  id, project_id, from_kind, from_id, relation_type,
                  to_kind, to_id, confidence, evidence_source_id
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    rel_id, project_id, "entity", from_ent["id"],
                    rel["relation_type"], "entity", to_ent["id"],
                    rel["confidence"], source_id,
                ),
            )
    await db.commit()

    # Write normalized JSON to filesystem
    norm_path = vault.normalized_source_path(source_id)
    write_atomic(norm_path, json.dumps(normalized, ensure_ascii=False, indent=2))

    # Create source_version record
    version_id = f"sv_{ULID()}"
    await db.execute(
        """
        INSERT INTO source_versions(
          id, source_id, version_no, parse_state, normalized_json_path
        ) VALUES (?,?,?,?,?)
        """,
        (version_id, source_id, 1, "normalized", str(norm_path)),
    )

    # Update source status
    meta = json.loads(source_row.get("metadata_json") or "{}")
    meta["summary"] = normalized["summary"]
    meta["topic_labels"] = normalized["topic_labels"]
    meta["quality_flags"] = normalized["quality_flags"]

    await db.execute(
        """
        UPDATE sources SET title=?, status='normalized', metadata_json=?,
        updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?
        """,
        (source_row.get("title"), json.dumps(meta), source_id),
    )
    await db.commit()

    log.info("source_normalized", source_id=source_id,
             entities=len(normalized["entities"]),
             claims=len(normalized["claims"]))

    return normalized


async def handle_normalize_source(ctx: JobContext) -> dict[str, Any]:
    """Job handler for normalizing a single source."""
    source_id = ctx.payload.get("source_id")
    if not source_id:
        raise ValueError("source_id is required")

    project = await ctx.app_ctx.project_manager.get_project(ctx.project_id)
    llm = ctx.app_ctx.llm

    await ctx.log_event("info", "step_started", f"Normalizing source {source_id[:12]}")
    result = await normalize_source(
        source_id=source_id,
        project_id=ctx.project_id,
        db=project.db,
        vault=project.vault,
        llm=llm,
        tier=project.config.get("default_model_tier", "balanced"),
    )
    await ctx.log_event("info", "step_finished", f"Normalized: {len(result['entities'])} entities")
    return result
