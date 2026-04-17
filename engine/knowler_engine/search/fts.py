"""
Full-text search and structured retrieval.

Implements the layered retrieval strategy from architecture §17.9:
1. Exact matches over titles, concept names, aliases, headings
2. Full-text search via SQLite FTS5 over content_units
3. Relation-aware expansion using entities and page links
4. (No semantic/vector search in v1)

Ranking uses §17.10 weighted scoring.
"""
from __future__ import annotations

from typing import Any

import structlog

log = structlog.get_logger(__name__)

# Page type boost values (§17.10)
_PAGE_TYPE_BOOST = {
    "comparison": 3.0,
    "concept": 2.0,
    "source_summary": 1.5,
    "question": 1.2,
    "index": 0.5,
    "timeline": 1.0,
    "glossary": 0.8,
}

# Trust level boost values
_TRUST_BOOST = {
    "high": 2.0,
    "medium": 1.0,
    "low": 0.5,
    "unknown": 0.3,
}


async def search_project(
    db: Any,
    project_id: str,
    query: str,
    limit: int = 20,
    unit_types: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Full-text search over content_units_fts.

    Returns ranked results with score, unit_type, title, body excerpt.
    """
    results: list[dict[str, Any]] = []

    # FTS query
    fts_query = query.replace('"', '""')  # escape FTS special chars
    try:
        rows = await db.fetchall(
            f"""
            SELECT cu.id, cu.unit_type, cu.title, cu.body, cu.parent_kind, cu.parent_id,
                   cu.source_id, cu.page_id,
                   bm25(content_units_fts) as fts_score
            FROM content_units_fts
            JOIN content_units cu ON cu.rowid = content_units_fts.rowid
            WHERE content_units_fts MATCH ? AND cu.project_id = ?
            {"AND cu.unit_type IN (" + ",".join("?" * len(unit_types)) + ")" if unit_types else ""}
            ORDER BY fts_score
            LIMIT ?
            """,
            (
                fts_query,
                project_id,
                *(unit_types or []),
                limit,
            ),
        )
    except Exception as exc:
        log.warning("fts_search_failed", error=str(exc), query=query[:80])
        rows = []

    for row in rows:
        score = abs(float(row["fts_score"] or 0))
        unit_type = row["unit_type"] or ""

        # Apply page type boost
        if row["page_id"]:
            page = await db.fetchone(
                "SELECT page_type FROM pages WHERE id=?", (row["page_id"],)
            )
            if page:
                score *= _PAGE_TYPE_BOOST.get(page["page_type"], 1.0)

        # Apply source trust boost
        if row["source_id"]:
            src = await db.fetchone(
                "SELECT trust_level FROM sources WHERE id=?", (row["source_id"],)
            )
            if src:
                score *= _TRUST_BOOST.get(src["trust_level"], 0.3)

        body = row["body"] or ""
        results.append(
            {
                "content_unit_id": row["id"],
                "unit_type": unit_type,
                "title": row["title"],
                "body_excerpt": body[:500],
                "parent_kind": row["parent_kind"],
                "parent_id": row["parent_id"],
                "source_id": row["source_id"],
                "page_id": row["page_id"],
                "score": score,
            }
        )

    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:limit]


async def exact_search(
    db: Any,
    project_id: str,
    query: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """
    Layer 1: exact title/name matching.
    Searches pages.title and entities.canonical_name.
    """
    results = []

    # Exact page title match
    pages = await db.fetchall(
        """
        SELECT id, page_type, title, slug, file_path, status
        FROM pages WHERE project_id=? AND lower(title) LIKE lower(?) AND status='active'
        ORDER BY title LIMIT ?
        """,
        (project_id, f"%{query}%", limit),
    )
    for p in pages:
        results.append({
            "kind": "page",
            "id": p["id"],
            "title": p["title"],
            "page_type": p["page_type"],
            "file_path": p["file_path"],
            "score": 10.0 if p["title"].lower() == query.lower() else 5.0,
        })

    # Exact entity match
    entities = await db.fetchall(
        """
        SELECT id, entity_type, display_name, canonical_name
        FROM entities WHERE project_id=? AND (
          lower(canonical_name) LIKE lower(?) OR
          lower(display_name) LIKE lower(?)
        ) LIMIT ?
        """,
        (project_id, f"%{query}%", f"%{query}%", limit),
    )
    for e in entities:
        results.append({
            "kind": "entity",
            "id": e["id"],
            "title": e["display_name"],
            "entity_type": e["entity_type"],
            "score": 8.0 if e["canonical_name"] == query.lower() else 4.0,
        })

    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:limit]


async def relation_expand(
    db: Any,
    project_id: str,
    entity_ids: list[str],
    hops: int = 1,
) -> list[dict[str, Any]]:
    """
    Layer 3: relation-aware expansion.
    Returns entity IDs reachable within `hops` from the given entities.
    """
    visited = set(entity_ids)
    frontier = list(entity_ids)
    results = []

    for _ in range(hops):
        if not frontier:
            break
        next_frontier = []
        for eid in frontier:
            rows = await db.fetchall(
                """
                SELECT r.to_id, e.display_name, r.relation_type, r.confidence
                FROM relations r
                JOIN entities e ON e.id = r.to_id
                WHERE r.project_id=? AND r.from_id=? AND r.to_kind='entity'
                UNION
                SELECT r.from_id, e.display_name, r.relation_type, r.confidence
                FROM relations r
                JOIN entities e ON e.id = r.from_id
                WHERE r.project_id=? AND r.to_id=? AND r.from_kind='entity'
                """,
                (project_id, eid, project_id, eid),
            )
            for row in rows:
                nid = row["to_id"] if row["to_id"] != eid else row["from_id"]
                if nid not in visited:
                    visited.add(nid)
                    next_frontier.append(nid)
                    results.append({
                        "entity_id": nid,
                        "name": row["display_name"],
                        "relation_type": row["relation_type"],
                        "confidence": row["confidence"],
                    })
        frontier = next_frontier

    return results
