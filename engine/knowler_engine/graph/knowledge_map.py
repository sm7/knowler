"""
Build and export a visualization-friendly knowledge map from project entities and relations.

The output is intentionally UI-oriented:
  - nodes: concepts/entities with summaries and neighborhood hints
  - edges: explicit relations plus inferred shared-source links
  - groups: thematic communities for clustered layout
"""
from __future__ import annotations

import hashlib
import json
import math
import pathlib
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from knowler_engine.storage.atomic import write_atomic
from knowler_engine.system_pages import summarize_markdown

_MAX_NODES = 140
_MAX_SHARED_EDGES_PER_NODE = 4
_MAX_SOURCE_TITLES = 3
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9+-]{1,}")
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
_STOPWORDS = {
    "about",
    "across",
    "after",
    "also",
    "among",
    "analysis",
    "approach",
    "architecture",
    "based",
    "because",
    "being",
    "between",
    "called",
    "concept",
    "concepts",
    "could",
    "data",
    "details",
    "entity",
    "from",
    "group",
    "have",
    "idea",
    "ideas",
    "into",
    "itself",
    "knowledge",
    "layer",
    "layered",
    "linked",
    "many",
    "model",
    "models",
    "note",
    "notes",
    "paper",
    "page",
    "pages",
    "project",
    "related",
    "relation",
    "representation",
    "research",
    "source",
    "sources",
    "summary",
    "system",
    "that",
    "their",
    "them",
    "there",
    "these",
    "they",
    "this",
    "through",
    "using",
    "what",
    "when",
    "with",
}
_UPPER_TOKENS = {"rag", "rlhf", "ppo", "gpt", "bert", "llm", "ml", "nlp", "api", "ui", "ux"}


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _stable_noise(seed: str) -> float:
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return (int(digest[:8], 16) / 0xFFFFFFFF) - 0.5


def _page_summary(path_str: str | None, fallback: str) -> str:
    if not path_str:
        return fallback
    path = pathlib.Path(path_str)
    if not path.exists():
        return fallback
    content = path.read_text(encoding="utf-8", errors="replace")
    return summarize_markdown(content, fallback=fallback, max_chars=220)


def _page_content(path_str: str | None) -> str:
    if not path_str:
        return ""
    path = pathlib.Path(path_str)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _display_token(token: str) -> str:
    if token in _UPPER_TOKENS:
        return token.upper()
    if len(token) <= 3 and token.isalpha():
        return token.upper()
    return token.replace("-", " ").title()


def _extract_terms(*parts: str) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    text = " ".join(part for part in parts if part).lower()
    for token in _TOKEN_RE.findall(text):
        candidates = [token]
        if "-" in token or "+" in token:
            candidates.extend(part for part in re.split(r"[-+]", token) if part)
        for candidate in candidates:
            if candidate in _STOPWORDS or candidate.isdigit():
                continue
            if len(candidate) <= 2:
                continue
            if candidate not in seen:
                seen.add(candidate)
                ordered.append(candidate)
    return ordered[:16]


def _name_reference_variants(name: str | None) -> set[str]:
    if not name:
        return set()
    raw = name.lower().strip()
    if not raw:
        return set()
    variants = {raw}
    if raw.endswith("s") and len(raw) > 4:
        variants.add(raw[:-1])
    variants.add(raw.replace("-", " "))
    variants.add(re.sub(r"[^a-z0-9]+", "", raw))
    if raw.endswith("s") and len(raw) > 4:
        singular = raw[:-1]
        variants.add(singular.replace("-", " "))
        variants.add(re.sub(r"[^a-z0-9]+", "", singular))
    return {variant.strip() for variant in variants if variant.strip()}


def _page_mentions_name(markdown: str, compact_markdown: str, variants: set[str]) -> bool:
    lowered = markdown.lower()
    for variant in variants:
        if " " in variant or "-" in variant:
            if variant in lowered:
                return True
        elif variant.isalnum() and len(variant) >= 4:
            if variant in compact_markdown:
                return True
    return False


def _extract_wikilinks(markdown: str) -> set[str]:
    return {match.group(1).strip().lower() for match in _WIKILINK_RE.finditer(markdown)}


def _source_title_list(
    source_ids: set[str] | list[str],
    source_title_by_id: dict[str, str],
    limit: int = _MAX_SOURCE_TITLES,
) -> list[str]:
    titles = []
    for source_id in sorted(set(source_ids)):
        title = source_title_by_id.get(source_id) or source_id
        titles.append(title)
    return titles[:limit]


def _edge_pair(left: str, right: str) -> tuple[str, str]:
    return tuple(sorted((left, right)))


def _initial_group_labels(
    node_ids: list[str],
    adjacency: dict[str, list[tuple[str, float]]],
) -> dict[str, str]:
    labels = {node_id: node_id for node_id in node_ids}
    degree = {node_id: sum(weight for _, weight in adjacency.get(node_id, [])) for node_id in node_ids}

    for _ in range(8):
        changed = False
        for node_id in sorted(node_ids, key=lambda value: (-degree.get(value, 0.0), value)):
            scores: dict[str, float] = defaultdict(float)
            for neighbor_id, weight in adjacency.get(node_id, []):
                scores[labels[neighbor_id]] += weight
            if not scores:
                continue
            best_label = max(scores.items(), key=lambda item: (item[1], item[0]))[0]
            if best_label != labels[node_id]:
                labels[node_id] = best_label
                changed = True
        if not changed:
            break

    return labels


def _desired_group_count(node_count: int) -> int:
    if node_count <= 6:
        return max(1, min(2, node_count))
    return max(3, min(6, math.ceil(math.sqrt(node_count) / 1.4)))


def _group_affinity(
    members_a: list[str],
    members_b: list[str],
    edge_map: dict[tuple[str, str], dict[str, Any]],
    support_map: dict[str, set[str]],
    row_by_id: dict[str, dict[str, Any]],
    term_map: dict[str, list[str]],
) -> float:
    members_b_set = set(members_b)
    explicit_score = 0.0
    inferred_score = 0.0
    shared_support = 0
    lexical_score = 0.0

    for node_id in members_a:
        sources_a = support_map.get(node_id, set())
        terms_a = set(term_map.get(node_id, [])[:8])
        for other_id in members_b:
            if sources_a:
                shared_support += len(sources_a & support_map.get(other_id, set()))
            if terms_a:
                lexical_score += len(terms_a & set(term_map.get(other_id, [])[:8]))
            edge = edge_map.get(_edge_pair(node_id, other_id))
            if not edge:
                continue
            if edge["kind"] == "explicit":
                explicit_score += 1.0 + float(edge["weight"])
            else:
                inferred_score += float(edge["weight"])

    type_bonus = 0.0
    types_a = {row_by_id[node_id]["entity_type"] for node_id in members_a}
    types_b = {row_by_id[node_id]["entity_type"] for node_id in members_b}
    if types_a & types_b:
        type_bonus = 0.24

    return (explicit_score * 1.35) + (inferred_score * 0.32) + (shared_support * 0.07) + (lexical_score * 0.18) + type_bonus


def _cluster_groups(
    node_ids: list[str],
    adjacency: dict[str, list[tuple[str, float]]],
    edge_map: dict[tuple[str, str], dict[str, Any]],
    support_map: dict[str, set[str]],
    row_by_id: dict[str, dict[str, Any]],
    term_map: dict[str, list[str]],
) -> dict[str, str]:
    labels = _initial_group_labels(node_ids, adjacency)
    grouped_ids: dict[str, list[str]] = defaultdict(list)
    for node_id, label in labels.items():
        grouped_ids[label].append(node_id)

    target_groups = _desired_group_count(len(node_ids))
    while len(grouped_ids) > target_groups:
        label_items = list(grouped_ids.items())
        best_pair: tuple[str, str] | None = None
        best_affinity = float("-inf")

        for index, (label_a, members_a) in enumerate(label_items):
            for label_b, members_b in label_items[index + 1 :]:
                affinity = _group_affinity(members_a, members_b, edge_map, support_map, row_by_id, term_map)
                if len(members_a) == 1 or len(members_b) == 1:
                    affinity += 0.06
                if affinity > best_affinity:
                    best_affinity = affinity
                    best_pair = (label_a, label_b)

        if best_pair is None:
            break

        keep_label, merge_label = best_pair
        if len(grouped_ids[keep_label]) < len(grouped_ids[merge_label]):
            keep_label, merge_label = merge_label, keep_label

        grouped_ids[keep_label].extend(grouped_ids.pop(merge_label))

    collapsed_labels: dict[str, str] = {}
    for label, members in grouped_ids.items():
        for node_id in members:
            collapsed_labels[node_id] = label
    return collapsed_labels


def _group_theme_terms(
    members: list[str],
    term_map: dict[str, list[str]],
    global_term_frequency: Counter[str],
) -> list[str]:
    local_frequency: Counter[str] = Counter()
    for node_id in members:
        local_frequency.update(term_map.get(node_id, [])[:10])

    scored_terms: list[tuple[float, str]] = []
    for term, count in local_frequency.items():
        rarity_bonus = 1.35 / max(global_term_frequency.get(term, 1), 1)
        score = count * rarity_bonus
        scored_terms.append((score, term))

    scored_terms.sort(key=lambda item: (-item[0], item[1]))
    return [_display_token(term) for _, term in scored_terms[:3]]


def _group_member_names(
    members: list[str],
    row_by_id: dict[str, dict[str, Any]],
    adjacency: dict[str, list[tuple[str, float]]],
) -> list[str]:
    member_set = set(members)
    scored: list[tuple[float, str]] = []
    for node_id in members:
        internal_weight = sum(weight for neighbor_id, weight in adjacency.get(node_id, []) if neighbor_id in member_set)
        row = row_by_id[node_id]
        score = internal_weight + float(row.get("confidence") or 0.0) + (0.12 * len(row.get("_support_sources", set())))
        scored.append((score, node_id))
    scored.sort(key=lambda item: (-item[0], row_by_id[item[1]]["display_name"].lower()))
    return [row_by_id[node_id]["display_name"] for _, node_id in scored[:3]]


def _build_group_rationale(
    themes: list[str],
    representative_nodes: list[str],
    explicit_edge_count: int,
    inferred_edge_count: int,
    source_count: int,
) -> str:
    focus = ", ".join(themes[:3]) if themes else ", ".join(representative_nodes[:2])
    parts = []
    if focus:
        parts.append(f"Grouped around {focus}")
    if explicit_edge_count > 0:
        parts.append(
            f"{explicit_edge_count} explicit link{'s' if explicit_edge_count != 1 else ''}"
        )
    if inferred_edge_count > 0:
        parts.append(
            f"{inferred_edge_count} inferred overlap link{'s' if inferred_edge_count != 1 else ''}"
        )
    if source_count > 0:
        parts.append(
            f"support spread across {source_count} source{'s' if source_count != 1 else ''}"
        )

    if not parts:
        return "This cluster is currently based on the strongest available graph connections."

    lead = parts[0]
    tail = parts[1:]
    if not tail:
        return f"{lead}."
    return f"{lead}, with " + ", ".join(tail) + "."


def _edge_provenance_kind(edge: dict[str, Any]) -> str:
    if edge["kind"] == "explicit" and edge.get("shared_sources", 0) > 0:
        return "hybrid"
    if edge["kind"] == "explicit":
        return "explicit_relation"
    return "shared_support"


def _edge_provenance_summary(
    edge: dict[str, Any],
    source_titles: list[str],
) -> str:
    if edge["kind"] == "explicit":
        relation = str(edge["relation_type"]).replace("_", " ")
        relation_count = int(edge.get("evidence_count") or 0)
        page_titles = sorted(edge.get("_evidence_page_titles", set()))
        if edge["relation_type"] == "wiki_link":
            base = "Explicit wiki link"
        elif edge["relation_type"] == "page_reference":
            base = "Explicit concept-page reference"
        else:
            base = f"Explicit relation '{relation}'"
            if relation_count > 1:
                base += f" seen {relation_count} times"
        if page_titles:
            base += f" from {', '.join(page_titles[:2])}"
        if edge.get("shared_sources", 0) > 0:
            shared = int(edge["shared_sources"])
            base += f", reinforced by {shared} shared source{'s' if shared != 1 else ''}"
    else:
        shared = int(edge.get("shared_sources") or 0)
        lexical = int(edge.get("lexical_overlap") or 0)
        base = f"Inferred from {shared} shared source{'s' if shared != 1 else ''}"
        if lexical > 0:
            base += f" and {lexical} overlapping topical term{'s' if lexical != 1 else ''}"
    if source_titles:
        base += f" ({', '.join(source_titles)})"
    return base + "."


def _serialize_edge(
    edge: dict[str, Any],
    source_title_by_id: dict[str, str],
) -> dict[str, Any]:
    provenance_kind = _edge_provenance_kind(edge)
    source_ids = edge.get("_evidence_source_ids", set()) | edge.get("_shared_source_ids", set())
    source_titles = _source_title_list(source_ids, source_title_by_id)
    return {
        "id": edge["id"],
        "source": edge["source"],
        "target": edge["target"],
        "relation_type": edge["relation_type"],
        "kind": edge["kind"],
        "weight": round(float(edge["weight"]), 3),
        "shared_sources": int(edge.get("shared_sources") or 0),
        "evidence_count": int(edge.get("evidence_count") or 0),
        "lexical_overlap": int(edge.get("lexical_overlap") or 0),
        "provenance_kind": provenance_kind,
        "source_titles": source_titles,
        "provenance_summary": _edge_provenance_summary(edge, source_titles),
    }


async def build_knowledge_map(db: Any, project_id: str) -> dict[str, Any]:
    entity_rows = await db.fetchall(
        """
        SELECT
          e.id,
          e.entity_type,
          e.display_name,
          e.description,
          e.confidence,
          e.created_from_source_id,
          p.id AS page_id,
          p.title AS page_title,
          p.file_path AS page_file_path
        FROM entities e
        LEFT JOIN relations about_rel
          ON about_rel.project_id = e.project_id
         AND about_rel.from_kind = 'page'
         AND about_rel.relation_type = 'about'
         AND about_rel.to_kind = 'entity'
         AND about_rel.to_id = e.id
        LEFT JOIN pages p
          ON p.id = about_rel.from_id
         AND p.status = 'active'
         AND p.page_type = 'concept'
        WHERE e.project_id=?
        ORDER BY e.confidence DESC, e.display_name
        """,
        (project_id,),
    )

    if not entity_rows:
        return {
            "project_id": project_id,
            "generated_at": _now_iso(),
            "nodes": [],
            "edges": [],
            "groups": [],
            "stats": {
                "node_count": 0,
                "edge_count": 0,
                "group_count": 0,
                "explicit_edge_count": 0,
                "inferred_edge_count": 0,
            },
        }

    deduped_entities: dict[str, dict[str, Any]] = {}
    for raw_row in entity_rows:
        row = dict(raw_row)
        existing = deduped_entities.get(row["id"])
        if existing is None:
            deduped_entities[row["id"]] = row
            continue
        if row.get("page_id") and not existing.get("page_id"):
            deduped_entities[row["id"]] = row
    entity_rows = list(deduped_entities.values())

    source_rows = await db.fetchall(
        """
        SELECT id, title
        FROM sources
        WHERE project_id=? AND deleted_at IS NULL
        """,
        (project_id,),
    )
    source_title_by_id = {row["id"]: row["title"] or row["id"] for row in source_rows}

    support_rows = await db.fetchall(
        """
        SELECT entity_id, source_id FROM (
          SELECT id AS entity_id, created_from_source_id AS source_id
          FROM entities
          WHERE project_id=? AND created_from_source_id IS NOT NULL
          UNION
          SELECT to_id AS entity_id, evidence_source_id AS source_id
          FROM relations
          WHERE project_id=? AND to_kind='entity' AND evidence_source_id IS NOT NULL
          UNION
          SELECT to_id AS entity_id, from_id AS source_id
          FROM relations
          WHERE project_id=? AND from_kind='source' AND to_kind='entity'
          UNION
          SELECT from_id AS entity_id, to_id AS source_id
          FROM relations
          WHERE project_id=? AND from_kind='entity' AND to_kind='source'
        )
        WHERE source_id IS NOT NULL
        """,
        (project_id, project_id, project_id, project_id),
    )

    support_map: dict[str, set[str]] = defaultdict(set)
    for row in support_rows:
        support_map[row["entity_id"]].add(row["source_id"])

    scored_entities = []
    for row in entity_rows:
        source_count = len(support_map.get(row["id"], set()))
        page_bonus = 0.75 if row.get("page_id") else 0.0
        score = float(row.get("confidence") or 0.0) + (source_count * 0.35) + page_bonus
        if source_count == 0 and not row.get("page_id") and not row.get("description"):
            score -= 0.5
        scored_entities.append((score, row))

    scored_entities.sort(key=lambda item: (-item[0], item[1]["display_name"].lower()))
    selected_rows = [row for _, row in scored_entities[:_MAX_NODES]]
    selected_ids = {row["id"] for row in selected_rows}
    row_by_id = {row["id"]: row for row in selected_rows}

    summary_by_id: dict[str, str] = {}
    page_content_by_id: dict[str, str] = {}
    term_map: dict[str, list[str]] = {}
    global_term_frequency: Counter[str] = Counter()
    for row in selected_rows:
        page_content = _page_content(row.get("page_file_path"))
        page_content_by_id[row["id"]] = page_content
        summary = _page_summary(
            row.get("page_file_path"),
            fallback=row.get("description") or f"{row['display_name']} is a {row['entity_type']} in this project.",
        )
        summary_by_id[row["id"]] = summary
        row["_support_sources"] = support_map.get(row["id"], set())
        terms = _extract_terms(row["display_name"], row.get("description") or "", summary, page_content)
        term_map[row["id"]] = terms
        global_term_frequency.update(set(terms))

    explicit_rows = await db.fetchall(
        """
        SELECT from_id, to_id, relation_type, confidence, evidence_source_id
        FROM relations
        WHERE project_id=? AND from_kind='entity' AND to_kind='entity'
        """,
        (project_id,),
    )

    edge_map: dict[tuple[str, str], dict[str, Any]] = {}
    adjacency: dict[str, list[tuple[str, float]]] = defaultdict(list)

    for row in explicit_rows:
        source = row["from_id"]
        target = row["to_id"]
        if source not in selected_ids or target not in selected_ids or source == target:
            continue
        pair = _edge_pair(source, target)
        weight = max(0.62, 0.55 + float(row["confidence"] or 0.6) * 0.55)
        edge = edge_map.setdefault(
            pair,
            {
                "id": f"edge_{pair[0]}_{pair[1]}",
                "source": pair[0],
                "target": pair[1],
                "relation_type": row["relation_type"],
                "kind": "explicit",
                "weight": 0.0,
                "shared_sources": 0,
                "evidence_count": 0,
                "lexical_overlap": 0,
                "_relation_types": Counter(),
                "_evidence_source_ids": set(),
                "_shared_source_ids": set(),
            },
        )
        edge["weight"] = max(float(edge["weight"]), weight)
        edge["evidence_count"] = int(edge.get("evidence_count") or 0) + 1
        edge["_relation_types"][row["relation_type"]] += 1
        evidence_source_id = row["evidence_source_id"]
        if evidence_source_id:
            edge["_evidence_source_ids"].add(evidence_source_id)
        if edge["relation_type"] == "related_to" and row["relation_type"] != "related_to":
            edge["relation_type"] = row["relation_type"]

    page_title_to_ids: dict[str, set[str]] = defaultdict(set)
    name_variants_by_id: dict[str, set[str]] = {}
    for row in selected_rows:
        name_variants = _name_reference_variants(row["display_name"])
        if row.get("page_title"):
            name_variants.update(_name_reference_variants(row["page_title"]))
            page_title_to_ids[row["page_title"].lower()].add(row["id"])
        page_title_to_ids[row["display_name"].lower()].add(row["id"])
        name_variants_by_id[row["id"]] = name_variants

    for row in selected_rows:
        node_id = row["id"]
        page_content = page_content_by_id.get(node_id, "")
        if not page_content:
            continue
        compact_page_content = re.sub(r"[^a-z0-9]+", "", page_content.lower())
        wikilinks = _extract_wikilinks(page_content)
        mention_candidates: list[tuple[str, str, float]] = []

        for wikilink in wikilinks:
            for target_id in page_title_to_ids.get(wikilink, set()):
                if target_id != node_id:
                    mention_candidates.append((target_id, "wiki_link", 0.86))

        for other in selected_rows:
            target_id = other["id"]
            if target_id == node_id:
                continue
            if _page_mentions_name(page_content, compact_page_content, name_variants_by_id[target_id]):
                mention_candidates.append((target_id, "page_reference", 0.72))

        best_by_target: dict[str, tuple[str, float]] = {}
        for target_id, relation_type, weight in mention_candidates:
            current = best_by_target.get(target_id)
            if current is None or weight > current[1]:
                best_by_target[target_id] = (relation_type, weight)

        for target_id, (relation_type, weight) in best_by_target.items():
            pair = _edge_pair(node_id, target_id)
            edge = edge_map.setdefault(
                pair,
                {
                    "id": f"edge_{pair[0]}_{pair[1]}",
                    "source": pair[0],
                    "target": pair[1],
                    "relation_type": relation_type,
                    "kind": "explicit",
                    "weight": 0.0,
                    "shared_sources": 0,
                    "evidence_count": 0,
                    "lexical_overlap": 0,
                    "_relation_types": Counter(),
                    "_evidence_source_ids": set(),
                    "_shared_source_ids": set(),
                    "_evidence_page_titles": set(),
                },
            )
            edge["kind"] = "explicit"
            edge["weight"] = max(float(edge["weight"]), weight)
            edge["evidence_count"] = int(edge.get("evidence_count") or 0) + 1
            edge["_relation_types"][relation_type] += 1
            edge.setdefault("_evidence_page_titles", set()).add(row.get("page_title") or row["display_name"])
            if edge["relation_type"] in {"related_to", "shared_source"} or relation_type == "wiki_link":
                edge["relation_type"] = relation_type

    selected_list = list(selected_rows)
    for index, row in enumerate(selected_list):
        candidates: list[tuple[float, str, set[str]]] = []
        sources_a = support_map.get(row["id"], set())
        if not sources_a:
            continue
        terms_a = set(term_map.get(row["id"], [])[:8])
        for other in selected_list[index + 1 :]:
            sources_b = support_map.get(other["id"], set())
            if not sources_b:
                continue
            shared_source_ids = sources_a & sources_b
            shared = len(shared_source_ids)
            if shared <= 0:
                continue

            lexical_overlap = len(terms_a & set(term_map.get(other["id"], [])[:8]))
            if shared == 1 and lexical_overlap == 0:
                continue

            pair = _edge_pair(row["id"], other["id"])
            existing = edge_map.get(pair)
            if existing:
                existing["shared_sources"] = max(int(existing.get("shared_sources") or 0), shared)
                existing["lexical_overlap"] = max(int(existing.get("lexical_overlap") or 0), lexical_overlap)
                existing["_shared_source_ids"].update(shared_source_ids)
                continue

            inferred_weight = 0.14 + min(0.34, (shared * 0.08) + (lexical_overlap * 0.05))
            candidates.append((inferred_weight, other["id"], shared_source_ids))

        candidates.sort(key=lambda item: (-item[0], item[1]))
        for inferred_weight, other_id, shared_source_ids in candidates[:_MAX_SHARED_EDGES_PER_NODE]:
            pair = _edge_pair(row["id"], other_id)
            if pair in edge_map:
                continue
            lexical_overlap = len(set(term_map.get(row["id"], [])[:8]) & set(term_map.get(other_id, [])[:8]))
            edge_map[pair] = {
                "id": f"edge_{pair[0]}_{pair[1]}",
                "source": pair[0],
                "target": pair[1],
                "relation_type": "shared_source",
                "kind": "inferred",
                "weight": round(float(inferred_weight), 4),
                "shared_sources": len(shared_source_ids),
                "evidence_count": len(shared_source_ids),
                "lexical_overlap": lexical_overlap,
                "_relation_types": Counter(),
                "_evidence_source_ids": set(),
                "_shared_source_ids": set(shared_source_ids),
            }

    for edge in edge_map.values():
        adjacency[edge["source"]].append((edge["target"], float(edge["weight"])))
        adjacency[edge["target"]].append((edge["source"], float(edge["weight"])))

    labels = _cluster_groups(
        [row["id"] for row in selected_rows],
        adjacency,
        edge_map,
        support_map,
        row_by_id,
        term_map,
    )
    grouped_ids: dict[str, list[str]] = defaultdict(list)
    for node_id, label in labels.items():
        grouped_ids[label].append(node_id)

    sorted_groups = sorted(grouped_ids.items(), key=lambda item: (-len(item[1]), item[0]))
    group_id_by_label = {label: f"group_{index + 1}" for index, (label, _) in enumerate(sorted_groups)}
    degree_map = {node_id: len(adjacency.get(node_id, [])) for node_id in row_by_id}

    groups = []
    group_meta_by_id: dict[str, dict[str, Any]] = {}
    for label, members in sorted_groups:
        group_id = group_id_by_label[label]
        member_set = set(members)
        explicit_edge_count = 0
        inferred_edge_count = 0
        group_source_ids: set[str] = set()
        for node_id in members:
            group_source_ids.update(support_map.get(node_id, set()))

        for edge in edge_map.values():
            if edge["source"] in member_set and edge["target"] in member_set:
                if edge["kind"] == "explicit":
                    explicit_edge_count += 1
                else:
                    inferred_edge_count += 1

        representative_nodes = _group_member_names(members, row_by_id, adjacency)
        themes = _group_theme_terms(members, term_map, global_term_frequency)
        if len(representative_nodes) >= 2:
            group_label = f"{representative_nodes[0]} + {representative_nodes[1]}"
        else:
            group_label = representative_nodes[0] if representative_nodes else row_by_id[members[0]]["display_name"]
        rationale = _build_group_rationale(
            themes=themes,
            representative_nodes=representative_nodes,
            explicit_edge_count=explicit_edge_count,
            inferred_edge_count=inferred_edge_count,
            source_count=len(group_source_ids),
        )
        group_payload = {
            "id": group_id,
            "label": group_label,
            "size": len(members),
            "themes": themes,
            "rationale": rationale,
            "representative_nodes": representative_nodes,
            "explicit_edge_count": explicit_edge_count,
            "inferred_edge_count": inferred_edge_count,
            "source_count": len(group_source_ids),
        }
        groups.append(group_payload)
        group_meta_by_id[group_id] = group_payload

    nodes = []
    for row in selected_rows:
        node_id = row["id"]
        label = labels.get(node_id, node_id)
        group_id = group_id_by_label.get(label, "group_1")
        summary = summary_by_id[node_id]
        related_neighbors = []
        for neighbor_id, _weight in sorted(
            adjacency.get(node_id, []),
            key=lambda item: (
                -float(edge_map[_edge_pair(node_id, item[0])]["weight"]),
                row_by_id.get(item[0], {"display_name": item[0]})["display_name"].lower(),
            ),
        )[:8]:
            edge = _serialize_edge(edge_map[_edge_pair(node_id, neighbor_id)], source_title_by_id)
            related_neighbors.append(
                {
                    "id": neighbor_id,
                    "label": row_by_id[neighbor_id]["display_name"],
                    "relation_type": edge["relation_type"],
                    "kind": edge["kind"],
                    "weight": edge["weight"],
                    "shared_sources": edge["shared_sources"],
                    "evidence_count": edge["evidence_count"],
                    "lexical_overlap": edge["lexical_overlap"],
                    "provenance_kind": edge["provenance_kind"],
                    "source_titles": edge["source_titles"],
                    "provenance_summary": edge["provenance_summary"],
                }
            )

        group_meta = group_meta_by_id.get(group_id, {})
        nodes.append(
            {
                "id": node_id,
                "label": row["display_name"],
                "entity_type": row["entity_type"],
                "summary": summary,
                "group": group_id,
                "group_label": group_meta.get("label", row["entity_type"]),
                "group_rationale": group_meta.get("rationale", ""),
                "supporting_sources": _source_title_list(support_map.get(node_id, set()), source_title_by_id),
                "page_id": row.get("page_id"),
                "page_title": row.get("page_title"),
                "source_count": len(support_map.get(node_id, set())),
                "degree": degree_map.get(node_id, 0),
                "confidence": round(float(row.get("confidence") or 0.0), 3),
                "lead_ideas": related_neighbors,
                "seed": round(_stable_noise(node_id), 6),
            }
        )

    edges = sorted(
        (_serialize_edge(edge, source_title_by_id) for edge in edge_map.values()),
        key=lambda edge: (-float(edge["weight"]), edge["source"], edge["target"]),
    )
    explicit_edge_count = sum(1 for edge in edges if edge["kind"] == "explicit")
    inferred_edge_count = sum(1 for edge in edges if edge["kind"] == "inferred")
    return {
        "project_id": project_id,
        "generated_at": _now_iso(),
        "nodes": nodes,
        "edges": edges,
        "groups": groups,
        "stats": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "group_count": len(groups),
            "explicit_edge_count": explicit_edge_count,
            "inferred_edge_count": inferred_edge_count,
        },
    }


def _render_graph_report(graph: dict[str, Any]) -> str:
    groups = graph.get("groups", [])
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])
    stats = graph.get("stats", {})

    lines = [
        "# Knowledge Graph Report",
        "",
        f"Generated: {graph.get('generated_at', 'unknown')}",
        "",
        "## Overview",
        "",
        f"- Nodes: {stats.get('node_count', 0)}",
        f"- Links: {stats.get('edge_count', 0)}",
        f"- Groups: {stats.get('group_count', 0)}",
        f"- Explicit links: {stats.get('explicit_edge_count', 0)}",
        f"- Inferred links: {stats.get('inferred_edge_count', 0)}",
        "",
        "## Group Overview",
        "",
    ]

    if not groups:
        lines.append("_No graph groups were generated yet._")
    else:
        for group in groups:
            lines.extend(
                [
                    f"### {group['label']}",
                    f"- Size: {group['size']} nodes",
                    f"- Themes: {', '.join(group.get('themes') or []) or 'n/a'}",
                    f"- Representative concepts: {', '.join(f'[[{name}]]' for name in group.get('representative_nodes') or []) or 'n/a'}",
                    f"- Rationale: {group.get('rationale') or 'n/a'}",
                    "",
                ]
            )

    def _edge_line(edge: dict[str, Any]) -> str:
        source = next((node["label"] for node in nodes if node["id"] == edge["source"]), edge["source"])
        target = next((node["label"] for node in nodes if node["id"] == edge["target"]), edge["target"])
        return f"- [[{source}]] -> [[{target}]] — {edge['provenance_summary']}"

    explicit_edges = [edge for edge in edges if edge["kind"] == "explicit"][:8]
    inferred_edges = [edge for edge in edges if edge["kind"] == "inferred"][:8]

    lines.extend(["## Strongest Explicit Links", ""])
    if explicit_edges:
        lines.extend(_edge_line(edge) for edge in explicit_edges)
    else:
        lines.append("_No explicit entity-to-entity links were extracted yet._")

    lines.extend(["", "## Strongest Inferred Links", ""])
    if inferred_edges:
        lines.extend(_edge_line(edge) for edge in inferred_edges)
    else:
        lines.append("_No inferred overlap links were generated yet._")

    lines.extend(["", "## Hub Concepts", ""])
    if nodes:
        for node in sorted(
            nodes,
            key=lambda item: (-int(item["degree"]), -int(item["source_count"]), item["label"].lower()),
        )[:10]:
            lines.append(
                f"- [[{node['label']}]] — {node['degree']} links, {node['source_count']} supporting source{'s' if node['source_count'] != 1 else ''}."
            )
    else:
        lines.append("_No graph nodes were generated yet._")

    return "\n".join(lines).rstrip() + "\n"


async def export_knowledge_graph(db: Any, project_id: str, vault: Any) -> dict[str, Any]:
    graph = await build_knowledge_map(db, project_id)
    graph_path = vault.root / "graph.json"
    report_path = vault.root / "GRAPH_REPORT.md"
    write_atomic(graph_path, json.dumps(graph, indent=2, sort_keys=False), tmp_dir=vault.tmp_dir)
    write_atomic(report_path, _render_graph_report(graph), tmp_dir=vault.tmp_dir)
    return {
        "graph": graph,
        "graph_path": str(graph_path),
        "report_path": str(report_path),
    }
