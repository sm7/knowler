"""
Entity resolution and deduplication.

Solves the core problem: the same real-world concept arrives from multiple
sources under slightly different surface forms ("GPT-4", "GPT 4", "GPT4"),
creating disconnected graph nodes that the query engine can't traverse together.

Two levels of resolution:

1. Insert-time (fast path) — called during normalize_source for each entity.
   Checks exact normalized-key match and alias overlap against existing entities
   of the same type. O(k) where k = entities of the same type in the project.
   Catches the common case without touching the rest of the graph.

2. Batch resolver — run as a dedicated job after normalization completes.
   Loads all entities for the project, clusters them with Union-Find using the
   same matching rules, and merges each cluster into its highest-confidence
   representative. Reassigns all relation FKs so the graph stays consistent.

Normalization rules (normalize_for_comparison):
  - Lowercase
  - Hyphens / underscores / slashes / dots → spaces
  - Letter–digit and digit–letter boundaries split with a space
    so "GPT4" and "GPT 4" and "GPT-4" all produce "gpt 4"
  - Remaining non-word punctuation stripped
  - Multiple spaces collapsed
"""
from __future__ import annotations

import json
import re
from typing import Any

import structlog

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def normalize_for_comparison(name: str) -> str:
    """
    Produce a canonical comparison key from an entity name.

    "GPT-4", "GPT 4", "GPT4" → "gpt 4"
    "transformer" → "transformer"
    "LLaMA-2" → "ll a ma 2"  (lowercase then boundary split)
    """
    s = name.lower().strip()
    # Common separators become spaces
    s = re.sub(r"[-_/\\.]", " ", s)
    # Split letter–digit and digit–letter boundaries
    s = re.sub(r"([a-z])(\d)", r"\1 \2", s)
    s = re.sub(r"(\d)([a-z])", r"\1 \2", s)
    # Drop remaining punctuation (parens, commas, etc.)
    s = re.sub(r"[^\w\s]", "", s)
    # Collapse whitespace
    return re.sub(r"\s+", " ", s).strip()


def _norm_aliases(aliases: list[str]) -> set[str]:
    return {normalize_for_comparison(a) for a in aliases if a.strip()}


# ---------------------------------------------------------------------------
# Insert-time resolution
# ---------------------------------------------------------------------------

async def find_matching_entity(
    db: Any,
    project_id: str,
    name: str,
    entity_type: str,
    aliases: list[str],
) -> str | None:
    """
    Check whether an entity equivalent to (name, entity_type) already exists.

    Matching rules (in priority order):
      1. Normalized canonical name is identical
      2. Incoming name matches an existing entity's alias (normalized)
      3. Incoming alias matches an existing entity's canonical name (normalized)
      4. Any incoming alias matches any existing entity's alias (normalized)

    Only entities of the same entity_type are compared; "Python" the tool and
    "Python" the concept are kept separate intentionally.

    Returns the existing entity id, or None if no match found.
    """
    norm_key = normalize_for_comparison(name)
    norm_new_aliases = _norm_aliases(aliases)

    rows = await db.fetchall(
        """
        SELECT id, canonical_name, aliases_json
        FROM entities WHERE project_id=? AND entity_type=?
        """,
        (project_id, entity_type),
    )

    for row in rows:
        existing_norm = normalize_for_comparison(row["canonical_name"])
        existing_aliases_raw: list[str] = json.loads(row["aliases_json"] or "[]")
        existing_norm_aliases = _norm_aliases(existing_aliases_raw)

        if norm_key == existing_norm:
            return row["id"]
        if norm_key in existing_norm_aliases:
            return row["id"]
        if existing_norm in norm_new_aliases:
            return row["id"]
        if norm_new_aliases & existing_norm_aliases:
            return row["id"]

    return None


async def add_alias_to_entity(
    db: Any,
    entity_id: str,
    new_display_name: str,
    new_aliases: list[str],
    new_confidence: float,
) -> None:
    """
    Absorb a surface form into an existing entity without creating a new row.
    Adds new_display_name and new_aliases to the entity's alias list.
    Bumps confidence to max(existing, new_confidence).
    """
    row = await db.fetchone(
        "SELECT aliases_json, confidence FROM entities WHERE id=?", (entity_id,)
    )
    if not row:
        return

    existing_aliases: list[str] = json.loads(row["aliases_json"] or "[]")
    alias_set = set(existing_aliases)
    alias_set.add(new_display_name)
    alias_set.update(new_aliases)

    updated_confidence = max(float(row["confidence"]), new_confidence)

    await db.execute(
        """
        UPDATE entities
        SET aliases_json=?, confidence=?,
            updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
        WHERE id=?
        """,
        (json.dumps(sorted(alias_set)), updated_confidence, entity_id),
    )


# ---------------------------------------------------------------------------
# Merge primitive
# ---------------------------------------------------------------------------

async def merge_entities(
    db: Any,
    project_id: str,
    winner_id: str,
    loser_id: str,
) -> None:
    """
    Merge the loser entity into the winner.

    Steps:
      - Absorb loser's display_name and aliases into winner's alias list
      - Reassign relation FKs (from_id / to_id) from loser → winner
        using UPDATE OR IGNORE to handle pre-existing duplicate edges,
        then DELETE any loser relations that couldn't be reassigned
      - Delete the loser entity row

    Idempotent: if winner or loser no longer exist the call is a no-op.
    """
    winner = await db.fetchone(
        "SELECT id, display_name, aliases_json FROM entities WHERE id=?", (winner_id,)
    )
    loser = await db.fetchone(
        "SELECT id, display_name, aliases_json FROM entities WHERE id=?", (loser_id,)
    )
    if not winner or not loser:
        return

    # Build merged alias set
    winner_aliases: set[str] = set(json.loads(winner["aliases_json"] or "[]"))
    loser_aliases: list[str] = json.loads(loser["aliases_json"] or "[]")
    winner_aliases.add(loser["display_name"])
    winner_aliases.update(loser_aliases)
    winner_aliases.discard(winner["display_name"])  # don't alias yourself

    await db.execute(
        """
        UPDATE entities
        SET aliases_json=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
        WHERE id=?
        """,
        (json.dumps(sorted(winner_aliases)), winner_id),
    )

    # Reassign relations where loser is the source
    await db.execute(
        "UPDATE OR IGNORE relations SET from_id=? WHERE from_id=? AND project_id=?",
        (winner_id, loser_id, project_id),
    )
    await db.execute(
        "DELETE FROM relations WHERE from_id=? AND project_id=?",
        (loser_id, project_id),
    )

    # Reassign relations where loser is the target
    await db.execute(
        "UPDATE OR IGNORE relations SET to_id=? WHERE to_id=? AND project_id=?",
        (winner_id, loser_id, project_id),
    )
    await db.execute(
        "DELETE FROM relations WHERE to_id=? AND project_id=?",
        (loser_id, project_id),
    )

    await db.execute("DELETE FROM entities WHERE id=?", (loser_id,))
    await db.commit()

    log.info(
        "entity_merged",
        project_id=project_id,
        winner_id=winner_id,
        loser_id=loser_id,
        loser_display=loser["display_name"],
    )


# ---------------------------------------------------------------------------
# Batch resolver
# ---------------------------------------------------------------------------

class _UnionFind:
    def __init__(self, ids: list[str]) -> None:
        self.parent = {i: i for i in ids}

    def find(self, x: str) -> str:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]  # path compression
            x = self.parent[x]
        return x

    def union(self, x: str, y: str) -> None:
        px, py = self.find(x), self.find(y)
        if px != py:
            self.parent[px] = py


async def resolve_project_entities(
    db: Any,
    project_id: str,
) -> dict[str, int]:
    """
    Batch entity resolution pass for a project.

    Algorithm:
      1. Load all entities for the project
      2. For each entity_type group, compute normalized keys and alias sets
      3. Use Union-Find to cluster entities that match any of the four rules
      4. Within each cluster, elect the winner (highest confidence, id as
         tiebreaker) and merge all losers into it

    Returns {"merged": n, "clusters": m} stats.

    Safe to run multiple times — idempotent.
    """
    rows = await db.fetchall(
        """
        SELECT id, entity_type, canonical_name, display_name, aliases_json, confidence
        FROM entities WHERE project_id=?
        """,
        (project_id,),
    )
    if not rows:
        return {"merged": 0, "clusters": 0}

    # Build entity metadata indexed by id
    entities: dict[str, dict] = {}
    by_type: dict[str, list[str]] = {}
    for row in rows:
        eid = row["id"]
        aliases_raw: list[str] = json.loads(row["aliases_json"] or "[]")
        entities[eid] = {
            "id": eid,
            "entity_type": row["entity_type"],
            "norm_key": normalize_for_comparison(row["canonical_name"]),
            "norm_aliases": _norm_aliases(aliases_raw),
            "confidence": float(row["confidence"]),
        }
        by_type.setdefault(row["entity_type"], []).append(eid)

    uf = _UnionFind(list(entities.keys()))

    # Compare pairs within the same entity_type only
    for type_ids in by_type.values():
        for i, aid in enumerate(type_ids):
            a = entities[aid]
            for bid in type_ids[i + 1:]:
                b = entities[bid]
                if (
                    a["norm_key"] == b["norm_key"]
                    or a["norm_key"] in b["norm_aliases"]
                    or b["norm_key"] in a["norm_aliases"]
                    or (a["norm_aliases"] & b["norm_aliases"])
                ):
                    uf.union(aid, bid)

    # Group by cluster root
    clusters: dict[str, list[str]] = {}
    for eid in entities:
        root = uf.find(eid)
        clusters.setdefault(root, []).append(eid)

    merged_count = 0
    cluster_count = sum(1 for members in clusters.values() if len(members) > 1)

    for members in clusters.values():
        if len(members) <= 1:
            continue

        # Winner = highest confidence; break ties with id (deterministic)
        winner_id = max(
            members,
            key=lambda eid: (entities[eid]["confidence"], eid),
        )
        for loser_id in members:
            if loser_id == winner_id:
                continue
            # Re-check that both still exist (earlier merge may have deleted one)
            winner_row = await db.fetchone(
                "SELECT id FROM entities WHERE id=?", (winner_id,)
            )
            loser_row = await db.fetchone(
                "SELECT id FROM entities WHERE id=?", (loser_id,)
            )
            if not winner_row or not loser_row:
                continue
            await merge_entities(db, project_id, winner_id, loser_id)
            merged_count += 1

    log.info(
        "entity_resolution_complete",
        project_id=project_id,
        merged=merged_count,
        clusters=cluster_count,
    )
    return {"merged": merged_count, "clusters": cluster_count}
