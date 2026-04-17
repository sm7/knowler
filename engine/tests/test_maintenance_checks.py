"""Tests for deterministic maintenance checks."""
import pytest
import pytest_asyncio
from pathlib import Path
from datetime import datetime, timedelta, timezone

from knowler_engine.storage.db import open_project_db
from knowler_engine.maintenance.checks import (
    check_orphan_pages,
    check_duplicate_entities,
    check_weak_claims,
    check_stale_pages,
    check_missing_comparisons,
)

PROJECT_ID = "proj-maint"


@pytest_asyncio.fixture
async def db(tmp_path: Path):
    _db = await open_project_db(tmp_path / "maint_proj")
    await _db.execute(
        "INSERT INTO projects (id, name, slug, root_path) VALUES (?, ?, ?, ?)",
        (PROJECT_ID, "Maint Test", "maint-test", str(tmp_path))
    )
    await _db.commit()
    yield _db
    await _db.close()


async def _insert_page(db, page_id, project_id, page_type, slug, updated_at=None, status="active"):
    if updated_at is None:
        updated_at = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "INSERT INTO pages (id, project_id, page_type, slug, file_path, title, status, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (page_id, project_id, page_type, slug, f"wiki/{slug}.md",
         slug.replace("-", " ").title(), status, updated_at)
    )
    await db.commit()


async def _insert_entity(db, entity_id, project_id, entity_type, canonical_name, display_name=None):
    await db.execute(
        "INSERT INTO entities (id, project_id, entity_type, canonical_name, display_name) "
        "VALUES (?, ?, ?, ?, ?)",
        (entity_id, project_id, entity_type, canonical_name, display_name or canonical_name)
    )
    await db.commit()


async def _insert_claim(db, claim_id, project_id, text, confidence):
    await db.execute(
        "INSERT INTO claims (id, project_id, claim_text, confidence) "
        "VALUES (?, ?, ?, ?)",
        (claim_id, project_id, text, confidence)
    )
    await db.commit()


async def _insert_relation(db, project_id, from_kind, from_id, rel_type, to_kind, to_id):
    rel_id = f"rel-{from_id[:8]}-{to_id[:8]}"
    await db.execute(
        "INSERT INTO relations (id, project_id, from_kind, from_id, relation_type, to_kind, to_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (rel_id, project_id, from_kind, from_id, rel_type, to_kind, to_id)
    )
    await db.commit()


# ---- Orphan pages ----

@pytest.mark.asyncio
async def test_check_orphan_pages_detects_orphan(db):
    await _insert_page(db, "pg-orphan", PROJECT_ID, "concept", "orphan-page")
    count = await check_orphan_pages(db, PROJECT_ID)
    assert count >= 1
    # Verify finding in DB
    findings = await db.fetchall(
        "SELECT * FROM maintenance_findings WHERE project_id=? AND finding_type='orphan_page'",
        (PROJECT_ID,)
    )
    assert len(findings) >= 1
    assert any(f["subject_id"] == "pg-orphan" for f in findings)


@pytest.mark.asyncio
async def test_check_orphan_pages_skips_non_orphan(db):
    await _insert_page(db, "pg-linked", PROJECT_ID, "concept", "linked-page")
    await _insert_relation(db, PROJECT_ID, "page", "pg-linked", "derived_from", "source", "src-1")
    count = await check_orphan_pages(db, PROJECT_ID)
    findings = await db.fetchall(
        "SELECT * FROM maintenance_findings WHERE project_id=? AND subject_id='pg-linked'",
        (PROJECT_ID,)
    )
    assert len(findings) == 0


@pytest.mark.asyncio
async def test_check_orphan_pages_returns_count(db):
    await _insert_page(db, "pg-o1", PROJECT_ID, "concept", "orphan-1")
    await _insert_page(db, "pg-o2", PROJECT_ID, "concept", "orphan-2")
    count = await check_orphan_pages(db, PROJECT_ID)
    assert count == 2


@pytest.mark.asyncio
async def test_check_orphan_pages_ignores_index_pages(db):
    await _insert_page(db, "pg-index", PROJECT_ID, "index", "project-index")
    count = await check_orphan_pages(db, PROJECT_ID)
    findings = await db.fetchall(
        "SELECT * FROM maintenance_findings WHERE project_id=? AND subject_id='pg-index'",
        (PROJECT_ID,),
    )
    assert count == 0
    assert findings == []


# ---- Duplicate entities ----

@pytest.mark.asyncio
async def test_check_duplicate_entities_detects_similar(db):
    await _insert_entity(db, "ent-1", PROJECT_ID, "concept", "Machine Learning")
    await _insert_entity(db, "ent-2", PROJECT_ID, "concept", "Machine Learnin")  # typo ≥ 0.85 sim
    count = await check_duplicate_entities(db, PROJECT_ID)
    assert count >= 1


@pytest.mark.asyncio
async def test_check_duplicate_entities_ignores_distinct(db):
    await _insert_entity(db, "ent-a", PROJECT_ID, "concept", "Quantum Physics")
    await _insert_entity(db, "ent-b", PROJECT_ID, "concept", "Bayesian Statistics")
    count = await check_duplicate_entities(db, PROJECT_ID)
    assert count == 0


@pytest.mark.asyncio
async def test_check_duplicate_entities_ignores_different_type(db):
    await _insert_entity(db, "ent-c", PROJECT_ID, "concept", "Python")
    await _insert_entity(db, "ent-d", PROJECT_ID, "tool", "Python")  # same name, different type
    count = await check_duplicate_entities(db, PROJECT_ID)
    assert count == 0  # Different types, not considered duplicates


# ---- Weak claims ----

@pytest.mark.asyncio
async def test_check_weak_claims_detects_low_confidence(db):
    await _insert_claim(db, "cl-weak", PROJECT_ID, "Some low confidence claim here", 0.3)
    count = await check_weak_claims(db, PROJECT_ID)
    assert count >= 1
    findings = await db.fetchall(
        "SELECT * FROM maintenance_findings WHERE project_id=? AND finding_type='weak_claim'",
        (PROJECT_ID,)
    )
    assert any(f["subject_id"] == "cl-weak" for f in findings)


@pytest.mark.asyncio
async def test_check_weak_claims_ignores_high_confidence(db):
    await _insert_claim(db, "cl-strong", PROJECT_ID, "High confidence claim", 0.9)
    count = await check_weak_claims(db, PROJECT_ID)
    findings = await db.fetchall(
        "SELECT * FROM maintenance_findings WHERE project_id=? AND subject_id='cl-strong'",
        (PROJECT_ID,)
    )
    assert len(findings) == 0


@pytest.mark.asyncio
async def test_check_weak_claims_boundary_at_0_4(db):
    # Exactly 0.4 should NOT be flagged (< 0.4 required)
    await _insert_claim(db, "cl-boundary", PROJECT_ID, "Boundary claim", 0.4)
    count = await check_weak_claims(db, PROJECT_ID)
    findings = await db.fetchall(
        "SELECT * FROM maintenance_findings WHERE project_id=? AND subject_id='cl-boundary'",
        (PROJECT_ID,)
    )
    assert len(findings) == 0


# ---- Stale pages ----

@pytest.mark.asyncio
async def test_check_stale_pages_detects_old_page(db):
    old_date = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    await _insert_page(db, "pg-stale", PROJECT_ID, "concept", "stale-page", updated_at=old_date)
    count = await check_stale_pages(db, PROJECT_ID)
    assert count >= 1
    findings = await db.fetchall(
        "SELECT * FROM maintenance_findings WHERE project_id=? AND subject_id='pg-stale'",
        (PROJECT_ID,)
    )
    assert len(findings) >= 1


@pytest.mark.asyncio
async def test_check_stale_pages_ignores_recent(db):
    recent = datetime.now(timezone.utc).isoformat()
    await _insert_page(db, "pg-fresh", PROJECT_ID, "concept", "fresh-page", updated_at=recent)
    count = await check_stale_pages(db, PROJECT_ID)
    findings = await db.fetchall(
        "SELECT * FROM maintenance_findings WHERE project_id=? AND subject_id='pg-fresh'",
        (PROJECT_ID,)
    )
    assert len(findings) == 0


# ---- Missing comparisons ----

@pytest.mark.asyncio
async def test_check_missing_comparisons_no_false_positives_with_sparse_data(db):
    await _insert_entity(db, "ent-x", PROJECT_ID, "concept", "Entity X")
    await _insert_entity(db, "ent-y", PROJECT_ID, "concept", "Entity Y")
    count = await check_missing_comparisons(db, PROJECT_ID)
    assert count == 0


@pytest.mark.asyncio
async def test_check_orphan_pages_idempotent(db):
    """Running the same check twice should not create duplicate findings."""
    await _insert_page(db, "pg-idem", PROJECT_ID, "concept", "idempotent-page")
    await check_orphan_pages(db, PROJECT_ID)
    await check_orphan_pages(db, PROJECT_ID)
    findings = await db.fetchall(
        "SELECT * FROM maintenance_findings WHERE project_id=? AND subject_id='pg-idem'",
        (PROJECT_ID,)
    )
    assert len(findings) == 1  # Only one finding, not duplicated
