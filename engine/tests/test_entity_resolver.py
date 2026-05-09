"""
Tests for entity resolution and deduplication.

Covers:
  - normalize_for_comparison canonicalization
  - find_matching_entity (all four matching rules)
  - Type boundary: same name, different entity_type → separate entities
  - add_alias_to_entity (alias absorption + confidence bump)
  - merge_entities (alias merge + FK reassignment)
  - resolve_project_entities (batch Union-Find clustering)
"""
import json
import pytest
from pathlib import Path

from knowler_engine.storage.db import open_project_db
from knowler_engine.normalize.entity_resolver import (
    normalize_for_comparison,
    find_matching_entity,
    add_alias_to_entity,
    merge_entities,
    resolve_project_entities,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _setup_project(db, project_id: str, tmp_path: Path) -> None:
    await db.execute(
        "INSERT INTO projects (id, name, slug, root_path) VALUES (?, ?, ?, ?)",
        (project_id, "Test", "test", str(tmp_path)),
    )
    await db.commit()


async def _insert_entity(
    db,
    project_id: str,
    eid: str,
    display_name: str,
    entity_type: str = "concept",
    aliases: list[str] | None = None,
    confidence: float = 0.8,
) -> None:
    canonical = display_name.lower().strip()
    await db.execute(
        """
        INSERT INTO entities(
          id, project_id, entity_type, canonical_name, display_name,
          aliases_json, confidence
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (
            eid, project_id, entity_type, canonical, display_name,
            json.dumps(aliases or []), confidence,
        ),
    )
    await db.commit()



async def _insert_relation(
    db, project_id: str, rel_id: str,
    from_id: str, to_id: str,
    relation_type: str = "related_to",
) -> None:
    await db.execute(
        """
        INSERT INTO relations(
          id, project_id, from_kind, from_id, relation_type,
          to_kind, to_id, confidence
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        (rel_id, project_id, "entity", from_id, relation_type, "entity", to_id, 0.8),
    )
    await db.commit()


@pytest.fixture
async def db_with_project(tmp_path: Path):
    database = await open_project_db(tmp_path / "resolver_proj")
    await _setup_project(database, "proj-1", tmp_path)
    yield database
    await database.close()


# ---------------------------------------------------------------------------
# normalize_for_comparison
# ---------------------------------------------------------------------------

class TestNormalizeForComparison:
    def test_hyphen_separator(self):
        assert normalize_for_comparison("GPT-4") == "gpt 4"

    def test_space_separator(self):
        assert normalize_for_comparison("GPT 4") == "gpt 4"

    def test_no_separator(self):
        assert normalize_for_comparison("GPT4") == "gpt 4"

    def test_underscore_separator(self):
        assert normalize_for_comparison("GPT_4") == "gpt 4"

    def test_all_three_forms_equal(self):
        assert (
            normalize_for_comparison("GPT-4")
            == normalize_for_comparison("GPT 4")
            == normalize_for_comparison("GPT4")
        )

    def test_dot_separator(self):
        assert normalize_for_comparison("v1.0") == "v 1 0"

    def test_slash_separator(self):
        assert normalize_for_comparison("A/B") == "a b"

    def test_mixed_case_preserved_as_lower(self):
        assert normalize_for_comparison("Transformer") == "transformer"

    def test_trailing_whitespace_stripped(self):
        assert normalize_for_comparison("  BERT  ") == "bert"

    def test_punctuation_dropped(self):
        # Parens are stripped; LLaMA stays "llama" (no letter-digit boundary within it)
        assert normalize_for_comparison("LLaMA (2023)") == "llama 2023"

    def test_multiple_spaces_collapsed(self):
        assert normalize_for_comparison("a   b") == "a b"

    def test_pure_digits(self):
        assert normalize_for_comparison("2024") == "2024"

    def test_short_word(self):
        assert normalize_for_comparison("AI") == "ai"


# ---------------------------------------------------------------------------
# find_matching_entity
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_find_match_rule1_normalized_key(db_with_project):
    """Rule 1: normalized canonical names match."""
    await _insert_entity(db_with_project, "proj-1", "ent-1", "GPT-4", aliases=[])

    match = await find_matching_entity(db_with_project, "proj-1", "GPT 4", "concept", [])
    assert match == "ent-1"

    match2 = await find_matching_entity(db_with_project, "proj-1", "GPT4", "concept", [])
    assert match2 == "ent-1"


@pytest.mark.asyncio
async def test_find_match_rule2_name_in_existing_alias(db_with_project):
    """Rule 2: incoming name matches an existing entity's alias."""
    await _insert_entity(db_with_project, "proj-1", "ent-2", "BERT", aliases=["Bidirectional Encoder"])

    match = await find_matching_entity(
        db_with_project, "proj-1", "Bidirectional Encoder", "concept", []
    )
    assert match == "ent-2"


@pytest.mark.asyncio
async def test_find_match_rule3_existing_canonical_in_new_aliases(db_with_project):
    """Rule 3: existing canonical name appears in incoming aliases."""
    await _insert_entity(db_with_project, "proj-1", "ent-3", "Transformer", aliases=[])

    match = await find_matching_entity(
        db_with_project, "proj-1", "Attention Mechanism",
        "concept", ["transformer", "attention is all you need"]
    )
    assert match == "ent-3"


@pytest.mark.asyncio
async def test_find_match_rule4_shared_aliases(db_with_project):
    """Rule 4: incoming and existing entities share an alias."""
    await _insert_entity(db_with_project, "proj-1", "ent-4", "LLM", aliases=["Large Language Model"])

    match = await find_matching_entity(
        db_with_project, "proj-1", "Foundation Model",
        "concept", ["large language model", "llm"]
    )
    assert match == "ent-4"


@pytest.mark.asyncio
async def test_find_no_match_returns_none(db_with_project):
    await _insert_entity(db_with_project, "proj-1", "ent-5", "Python", "tool")

    match = await find_matching_entity(db_with_project, "proj-1", "Rust", "tool", [])
    assert match is None


@pytest.mark.asyncio
async def test_find_type_boundary_respected(db_with_project):
    """Same name under different entity_type must NOT match."""
    await _insert_entity(db_with_project, "proj-1", "ent-6", "Python", "tool")

    # "Python" the concept is a different entity_type
    match = await find_matching_entity(db_with_project, "proj-1", "Python", "concept", [])
    assert match is None


@pytest.mark.asyncio
async def test_find_no_match_empty_project(db_with_project):
    match = await find_matching_entity(db_with_project, "proj-1", "Anything", "concept", [])
    assert match is None


# ---------------------------------------------------------------------------
# add_alias_to_entity
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_add_alias_extends_alias_list(db_with_project):
    await _insert_entity(db_with_project, "proj-1", "ent-7", "BERT", aliases=["bert base"])

    await add_alias_to_entity(db_with_project, "ent-7", "bert-base-uncased", ["base bert"], 0.9)

    row = await db_with_project.fetchone("SELECT aliases_json, confidence FROM entities WHERE id=?", ("ent-7",))
    aliases = json.loads(row["aliases_json"])
    assert "bert-base-uncased" in aliases
    assert "base bert" in aliases
    assert "bert base" in aliases


@pytest.mark.asyncio
async def test_add_alias_bumps_confidence(db_with_project):
    await _insert_entity(db_with_project, "proj-1", "ent-8", "RoBERTa", aliases=[], confidence=0.5)

    await add_alias_to_entity(db_with_project, "ent-8", "roberta", [], 0.95)

    row = await db_with_project.fetchone("SELECT confidence FROM entities WHERE id=?", ("ent-8",))
    assert float(row["confidence"]) == pytest.approx(0.95)


@pytest.mark.asyncio
async def test_add_alias_does_not_lower_confidence(db_with_project):
    await _insert_entity(db_with_project, "proj-1", "ent-9", "T5", aliases=[], confidence=0.9)

    await add_alias_to_entity(db_with_project, "ent-9", "t5-model", [], 0.4)

    row = await db_with_project.fetchone("SELECT confidence FROM entities WHERE id=?", ("ent-9",))
    assert float(row["confidence"]) == pytest.approx(0.9)


@pytest.mark.asyncio
async def test_add_alias_noop_on_missing_entity(db_with_project):
    # Should not raise
    await add_alias_to_entity(db_with_project, "nonexistent", "whatever", [], 0.8)


# ---------------------------------------------------------------------------
# merge_entities
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_merge_absorbs_loser_aliases(db_with_project):
    await _insert_entity(db_with_project, "proj-1", "ent-w", "GPT-4", aliases=["gpt4"])
    await _insert_entity(db_with_project, "proj-1", "ent-l", "GPT 4", aliases=["gpt-4-turbo"])

    await merge_entities(db_with_project, "proj-1", "ent-w", "ent-l")

    winner = await db_with_project.fetchone("SELECT aliases_json FROM entities WHERE id=?", ("ent-w",))
    aliases = json.loads(winner["aliases_json"])
    # Loser's display_name and aliases absorbed into winner
    assert "GPT 4" in aliases
    assert "gpt-4-turbo" in aliases


@pytest.mark.asyncio
async def test_merge_deletes_loser(db_with_project):
    await _insert_entity(db_with_project, "proj-1", "ent-w2", "BERT", aliases=[])
    await _insert_entity(db_with_project, "proj-1", "ent-l2", "bert-base", aliases=[])

    await merge_entities(db_with_project, "proj-1", "ent-w2", "ent-l2")

    loser = await db_with_project.fetchone("SELECT id FROM entities WHERE id=?", ("ent-l2",))
    assert loser is None


@pytest.mark.asyncio
async def test_merge_reassigns_from_relations(db_with_project):
    """Relations where loser is source must point to winner after merge."""
    await _insert_entity(db_with_project, "proj-1", "ent-a", "EntityA", aliases=[])
    await _insert_entity(db_with_project, "proj-1", "ent-b", "EntityB", aliases=[])
    await _insert_entity(db_with_project, "proj-1", "ent-c", "EntityC", aliases=[])
    await _insert_relation(db_with_project, "proj-1", "rel-1", "ent-b", "ent-c")

    await merge_entities(db_with_project, "proj-1", "ent-a", "ent-b")

    rel = await db_with_project.fetchone("SELECT from_id FROM relations WHERE id=?", ("rel-1",))
    assert rel["from_id"] == "ent-a"


@pytest.mark.asyncio
async def test_merge_reassigns_to_relations(db_with_project):
    """Relations where loser is target must point to winner after merge."""
    await _insert_entity(db_with_project, "proj-1", "ent-d", "EntityD", aliases=[])
    await _insert_entity(db_with_project, "proj-1", "ent-e", "EntityE", aliases=[])
    await _insert_entity(db_with_project, "proj-1", "ent-f", "EntityF", aliases=[])
    await _insert_relation(db_with_project, "proj-1", "rel-2", "ent-f", "ent-e")

    await merge_entities(db_with_project, "proj-1", "ent-d", "ent-e")

    rel = await db_with_project.fetchone("SELECT to_id FROM relations WHERE id=?", ("rel-2",))
    assert rel["to_id"] == "ent-d"


@pytest.mark.asyncio
async def test_merge_idempotent_missing_entities(db_with_project):
    """If winner or loser no longer exist, merge must be a no-op (not raise)."""
    await merge_entities(db_with_project, "proj-1", "ghost-w", "ghost-l")


@pytest.mark.asyncio
async def test_merge_duplicate_relations_handled(db_with_project):
    """Merging two nodes that already have a relation to the same target shouldn't crash."""
    await _insert_entity(db_with_project, "proj-1", "ent-m1", "EntityM1", aliases=[])
    await _insert_entity(db_with_project, "proj-1", "ent-m2", "EntityM2", aliases=[])
    await _insert_entity(db_with_project, "proj-1", "ent-m3", "EntityM3", aliases=[])
    # Both m1 and m2 point to m3
    await _insert_relation(db_with_project, "proj-1", "rel-dup1", "ent-m1", "ent-m3")
    await _insert_relation(db_with_project, "proj-1", "rel-dup2", "ent-m2", "ent-m3")

    # Merging m2 into m1: rel-dup2 reassign will conflict with rel-dup1 (same from/to after reassign)
    # UPDATE OR IGNORE must handle this; the duplicate should be DELETEd
    await merge_entities(db_with_project, "proj-1", "ent-m1", "ent-m2")

    # m2 should be gone
    assert await db_with_project.fetchone("SELECT id FROM entities WHERE id=?", ("ent-m2",)) is None
    # m1 should survive
    assert await db_with_project.fetchone("SELECT id FROM entities WHERE id=?", ("ent-m1",)) is not None


# ---------------------------------------------------------------------------
# resolve_project_entities (batch)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_merges_duplicate_cluster(db_with_project):
    """Three surface forms of the same entity should collapse into one."""
    await _insert_entity(db_with_project, "proj-1", "ent-r1", "GPT-4", confidence=0.9)
    await _insert_entity(db_with_project, "proj-1", "ent-r2", "GPT 4", confidence=0.7)
    await _insert_entity(db_with_project, "proj-1", "ent-r3", "GPT4",  confidence=0.6)

    stats = await resolve_project_entities(db_with_project, "proj-1")

    assert stats["merged"] == 2
    assert stats["clusters"] == 1

    remaining = await db_with_project.fetchall(
        "SELECT id FROM entities WHERE project_id='proj-1'", ()
    )
    assert len(remaining) == 1
    # Winner should be highest confidence: ent-r1
    assert remaining[0]["id"] == "ent-r1"


@pytest.mark.asyncio
async def test_resolve_distinct_entities_untouched(db_with_project):
    """Entities that don't match must not be merged."""
    await _insert_entity(db_with_project, "proj-1", "ent-x1", "Python", "tool")
    await _insert_entity(db_with_project, "proj-1", "ent-x2", "Rust", "tool")
    await _insert_entity(db_with_project, "proj-1", "ent-x3", "Go", "tool")

    stats = await resolve_project_entities(db_with_project, "proj-1")

    assert stats["merged"] == 0
    assert stats["clusters"] == 0
    remaining = await db_with_project.fetchall(
        "SELECT id FROM entities WHERE project_id='proj-1'", ()
    )
    assert len(remaining) == 3


@pytest.mark.asyncio
async def test_resolve_type_boundary_prevents_merge(db_with_project):
    """Python-tool and Python-concept must not be merged despite identical normalized key."""
    await _insert_entity(db_with_project, "proj-1", "ent-py-tool", "Python", "tool")
    await _insert_entity(db_with_project, "proj-1", "ent-py-concept", "Python", "concept")

    stats = await resolve_project_entities(db_with_project, "proj-1")

    assert stats["merged"] == 0
    remaining = await db_with_project.fetchall(
        "SELECT id FROM entities WHERE project_id='proj-1'", ()
    )
    assert len(remaining) == 2


@pytest.mark.asyncio
async def test_resolve_empty_project_noop(db_with_project):
    stats = await resolve_project_entities(db_with_project, "proj-1")
    assert stats == {"merged": 0, "clusters": 0}


@pytest.mark.asyncio
async def test_resolve_idempotent(db_with_project):
    """Running batch resolution twice must produce the same result."""
    # "GPT-4" (canonical "gpt-4") and "GPT4" (canonical "gpt4") normalize to "gpt 4"
    await _insert_entity(db_with_project, "proj-1", "ent-i1", "GPT-4", confidence=0.9)
    await _insert_entity(db_with_project, "proj-1", "ent-i2", "GPT4", confidence=0.7)

    stats1 = await resolve_project_entities(db_with_project, "proj-1")
    assert stats1["merged"] == 1

    stats2 = await resolve_project_entities(db_with_project, "proj-1")
    assert stats2["merged"] == 0  # Nothing left to merge


@pytest.mark.asyncio
async def test_resolve_winner_is_highest_confidence(db_with_project):
    """The entity with highest confidence must survive as the winner."""
    # "gpt-4" and "gpt4" have different canonical names but normalize to "gpt 4"
    await _insert_entity(db_with_project, "proj-1", "ent-lo", "gpt-4", confidence=0.4)
    await _insert_entity(db_with_project, "proj-1", "ent-hi", "gpt4",  confidence=0.95)

    await resolve_project_entities(db_with_project, "proj-1")

    remaining = await db_with_project.fetchall(
        "SELECT id FROM entities WHERE project_id='proj-1'", ()
    )
    assert len(remaining) == 1
    assert remaining[0]["id"] == "ent-hi"


@pytest.mark.asyncio
async def test_resolve_scoped_to_project(db_with_project):
    """Entities in a different project must never be merged with proj-1 entities."""
    await db_with_project.execute(
        "INSERT INTO projects (id, name, slug, root_path) VALUES (?, ?, ?, ?)",
        ("proj-2", "Test2", "test2", "/tmp/p2"),
    )
    await db_with_project.commit()
    await _insert_entity(db_with_project, "proj-1", "ent-p1", "GPT-4", confidence=0.9)
    await _insert_entity(db_with_project, "proj-2", "ent-p2", "GPT-4", confidence=0.9)

    stats = await resolve_project_entities(db_with_project, "proj-1")

    assert stats["merged"] == 0
    # Both must still exist
    assert await db_with_project.fetchone("SELECT id FROM entities WHERE id=?", ("ent-p1",))
    assert await db_with_project.fetchone("SELECT id FROM entities WHERE id=?", ("ent-p2",))
