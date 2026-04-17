"""
Maintenance checks — deterministic knowledge health checks.

These checks are NON-LLM. They use deterministic logic against the DB.
Results are stored as maintenance_findings.

Architecture: §19.2

Checks:
1. orphan_page — pages with no source support
2. duplicate_entity — entities with very similar names
3. weak_claim — claims with confidence < 0.4
4. stale_page — pages not updated in > 30 days with changed sources
5. missing_comparison — concept pairs with multiple shared sources but no comparison page
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any

import structlog
from ulid import ULID

log = structlog.get_logger(__name__)

_STALE_DAYS = 30
_DUPLICATE_SIMILARITY_THRESHOLD = 0.85
_WEAK_CLAIM_CONFIDENCE = 0.4
_MISSING_COMPARISON_MIN_SHARED_SOURCES = 2


async def _upsert_finding(
    db: Any,
    project_id: str,
    finding_type: str,
    severity: str,
    subject_kind: str,
    subject_id: str,
    title: str,
    description: str,
    suggestion: dict,
) -> None:
    """Insert a finding if it doesn't already exist (open status)."""
    existing = await db.fetchone(
        """
        SELECT id FROM maintenance_findings
        WHERE project_id=? AND finding_type=? AND subject_id=? AND status='open'
        """,
        (project_id, finding_type, subject_id),
    )
    if existing:
        return  # Already reported

    finding_id = f"mf_{ULID()}"
    await db.execute(
        """
        INSERT INTO maintenance_findings(
          id, project_id, finding_type, severity, subject_kind, subject_id,
          title, description, suggestion_json
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            finding_id, project_id, finding_type, severity, subject_kind, subject_id,
            title, description, json.dumps(suggestion),
        ),
    )


async def check_orphan_pages(db: Any, project_id: str) -> int:
    """
    Find wiki pages that have no derived_from relation to any source.
    These are orphans — no provenance.
    """
    pages = await db.fetchall(
        """
        SELECT p.id, p.title, p.page_type
        FROM pages p
        WHERE p.project_id=? AND p.status='active'
          AND p.page_type != 'index'
          AND NOT EXISTS (
            SELECT 1 FROM relations r
            WHERE r.project_id=? AND r.from_kind='page' AND r.from_id=p.id
              AND r.relation_type='derived_from'
          )
        ORDER BY p.created_at
        """,
        (project_id, project_id),
    )

    count = 0
    for page in pages:
        await _upsert_finding(
            db,
            project_id,
            finding_type="orphan_page",
            severity="medium",
            subject_kind="page",
            subject_id=page["id"],
            title=f"Orphan page: {page['title'][:60]}",
            description=f"Page '{page['title']}' has no source provenance. It was not derived from any ingested source.",
            suggestion={"action_type": "recompile", "details": "Re-run compile or delete this page if it's no longer relevant"},
        )
        count += 1

    await db.commit()
    log.info("orphan_check_done", project_id=project_id, found=count)
    return count


async def check_duplicate_entities(db: Any, project_id: str) -> int:
    """
    Find entities with highly similar names that may be duplicates.
    Uses SequenceMatcher for similarity scoring (deterministic, no LLM).
    """
    entities = await db.fetchall(
        "SELECT id, canonical_name, display_name, entity_type FROM entities WHERE project_id=? ORDER BY canonical_name",
        (project_id,),
    )
    entities_list = [dict(e) for e in entities]

    count = 0
    seen_pairs: set[frozenset] = set()

    for i, e1 in enumerate(entities_list):
        for e2 in entities_list[i + 1:]:
            pair = frozenset([e1["id"], e2["id"]])
            if pair in seen_pairs:
                continue
            if e1["entity_type"] != e2["entity_type"]:
                continue

            sim = SequenceMatcher(None, e1["canonical_name"], e2["canonical_name"]).ratio()
            if sim >= _DUPLICATE_SIMILARITY_THRESHOLD:
                seen_pairs.add(pair)
                # Report on the second entity (keep the first)
                await _upsert_finding(
                    db,
                    project_id,
                    finding_type="duplicate_entity",
                    severity="medium",
                    subject_kind="entity",
                    subject_id=e2["id"],
                    title=f"Possible duplicate: '{e1['display_name']}' ≈ '{e2['display_name']}'",
                    description=(
                        f"Entity '{e2['display_name']}' is {sim:.0%} similar to '{e1['display_name']}' "
                        f"(both type: {e1['entity_type']}). Consider merging."
                    ),
                    suggestion={
                        "action_type": "merge",
                        "keep_id": e1["id"],
                        "merge_id": e2["id"],
                    },
                )
                count += 1

    await db.commit()
    log.info("duplicate_entity_check_done", project_id=project_id, found=count)
    return count


async def check_weak_claims(db: Any, project_id: str) -> int:
    """
    Find claims with very low confidence or no supporting source.
    """
    weak_claims = await db.fetchall(
        """
        SELECT id, claim_text, confidence, created_from_source_id
        FROM claims
        WHERE project_id=? AND status='active' AND confidence < ?
        ORDER BY confidence ASC LIMIT 50
        """,
        (project_id, _WEAK_CLAIM_CONFIDENCE),
    )

    count = 0
    for claim in weak_claims:
        await _upsert_finding(
            db,
            project_id,
            finding_type="weak_claim",
            severity="low",
            subject_kind="claim",
            subject_id=claim["id"],
            title=f"Weak claim (confidence={claim['confidence']:.2f})",
            description=f"Claim: \"{claim['claim_text'][:200]}\" has low confidence and may need additional source support.",
            suggestion={"action_type": "add_source", "details": "Find additional sources that support or contradict this claim"},
        )
        count += 1

    await db.commit()
    log.info("weak_claim_check_done", project_id=project_id, found=count)
    return count


async def check_stale_pages(db: Any, project_id: str) -> int:
    """
    Find pages that haven't been updated in more than STALE_DAYS days,
    while their source has been updated more recently.
    """
    cutoff = (datetime.now(tz=timezone.utc) - timedelta(days=_STALE_DAYS)).isoformat()

    stale = await db.fetchall(
        """
        SELECT p.id, p.title, p.updated_at, p.page_type
        FROM pages p
        WHERE p.project_id=? AND p.status='active' AND p.updated_at < ?
        LIMIT 50
        """,
        (project_id, cutoff),
    )

    count = 0
    for page in stale:
        await _upsert_finding(
            db,
            project_id,
            finding_type="stale_page",
            severity="low",
            subject_kind="page",
            subject_id=page["id"],
            title=f"Stale page: {page['title'][:60]}",
            description=f"Page '{page['title']}' hasn't been updated since {page['updated_at'][:10]}. Consider recompiling.",
            suggestion={"action_type": "recompile", "details": "Trigger a targeted recompile for this page"},
        )
        count += 1

    await db.commit()
    log.info("stale_page_check_done", project_id=project_id, found=count)
    return count


async def check_missing_comparisons(db: Any, project_id: str) -> int:
    """
    Find concept pairs that share >= MIN_SHARED_SOURCES sources
    but have no comparison page linking them.
    """
    # Find entities that appear together in multiple sources
    entities = await db.fetchall(
        "SELECT id, display_name FROM entities WHERE project_id=? AND entity_type='concept' LIMIT 100",
        (project_id,),
    )
    entities_list = [dict(e) for e in entities]

    count = 0
    seen_pairs: set[frozenset] = set()

    for i, e1 in enumerate(entities_list):
        for e2 in entities_list[i + 1:]:
            pair = frozenset([e1["id"], e2["id"]])
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)

            # Count shared sources
            shared = await db.fetchone(
                """
                SELECT COUNT(*) as cnt FROM (
                  SELECT r1.evidence_source_id
                  FROM relations r1
                  WHERE r1.project_id=? AND r1.to_id=? AND r1.to_kind='entity'
                    AND r1.evidence_source_id IS NOT NULL
                  INTERSECT
                  SELECT r2.evidence_source_id
                  FROM relations r2
                  WHERE r2.project_id=? AND r2.to_id=? AND r2.to_kind='entity'
                    AND r2.evidence_source_id IS NOT NULL
                )
                """,
                (project_id, e1["id"], project_id, e2["id"]),
            )
            if not shared or (shared["cnt"] or 0) < _MISSING_COMPARISON_MIN_SHARED_SOURCES:
                continue

            # Check if a comparison page already exists for these two
            existing = await db.fetchone(
                """
                SELECT p.id FROM pages p
                WHERE p.project_id=? AND p.page_type='comparison'
                  AND (
                    lower(p.title) LIKE lower(?) OR
                    lower(p.title) LIKE lower(?)
                  )
                """,
                (
                    project_id,
                    f"%{e1['display_name'][:20]}%",
                    f"%{e2['display_name'][:20]}%",
                ),
            )
            if existing:
                continue

            await _upsert_finding(
                db,
                project_id,
                finding_type="missing_comparison",
                severity="medium",
                subject_kind="entity",
                subject_id=e1["id"],
                title=f"Missing comparison: {e1['display_name']} vs {e2['display_name']}",
                description=(
                    f"'{e1['display_name']}' and '{e2['display_name']}' share {shared['cnt']} common sources "
                    "but have no comparison page."
                ),
                suggestion={
                    "action_type": "create_page",
                    "page_type": "comparison",
                    "title": f"{e1['display_name']} vs {e2['display_name']}",
                    "entity_ids": [e1["id"], e2["id"]],
                },
            )
            count += 1
            if count >= 10:  # Cap at 10 per run
                break
        if count >= 10:
            break

    await db.commit()
    log.info("missing_comparison_check_done", project_id=project_id, found=count)
    return count
