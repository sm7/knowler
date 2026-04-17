-- Migration 0001: Initial schema
-- Creates all core tables for the Knowler knowledge engine.
-- This file must never be edited after release.

BEGIN;

-- -----------------------------------------------------------------------
-- Schema migration tracking
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_migrations (
  version    INTEGER PRIMARY KEY,
  name       TEXT NOT NULL,
  applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- -----------------------------------------------------------------------
-- Projects
-- Stored here for foreign key integrity; global project list lives in
-- the global app DB (~/Library/Application Support/Knowler/knowler.db).
-- -----------------------------------------------------------------------
CREATE TABLE projects (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  slug        TEXT NOT NULL UNIQUE,
  root_path   TEXT NOT NULL UNIQUE,
  created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  status      TEXT NOT NULL DEFAULT 'active',  -- active, archived, deleted
  config_json TEXT NOT NULL DEFAULT '{}'
);

-- -----------------------------------------------------------------------
-- Sources
-- -----------------------------------------------------------------------
CREATE TABLE sources (
  id              TEXT PRIMARY KEY,
  project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  source_type     TEXT NOT NULL,  -- bookmark, url_article, pdf, markdown_note, repo_doc, image
  origin_type     TEXT NOT NULL,  -- bookmark_import, manual_url, file_upload, repo_sync
  title           TEXT,
  canonical_url   TEXT,
  display_url     TEXT,
  domain          TEXT,
  raw_path        TEXT NOT NULL,
  mime_type       TEXT,
  checksum_sha256 TEXT,
  byte_size       INTEGER,
  trust_level     TEXT NOT NULL DEFAULT 'unknown',  -- unknown, low, medium, high
  status          TEXT NOT NULL DEFAULT 'pending',   -- pending, normalized, compiled, rejected, failed
  ingest_state    TEXT NOT NULL DEFAULT 'new',       -- new, needs_review, approved, rejected, promoted
  language_code   TEXT,
  source_date     TEXT,
  created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  deleted_at      TEXT,
  metadata_json   TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_sources_project_status   ON sources(project_id, status);
CREATE INDEX idx_sources_project_type     ON sources(project_id, source_type);
CREATE INDEX idx_sources_project_url      ON sources(project_id, canonical_url);
CREATE INDEX idx_sources_project_checksum ON sources(project_id, checksum_sha256);
CREATE INDEX idx_sources_project_state    ON sources(project_id, ingest_state);

-- -----------------------------------------------------------------------
-- Source versions
-- Stores parsed/normalized snapshots for deterministic recompilation.
-- -----------------------------------------------------------------------
CREATE TABLE source_versions (
  id                   TEXT PRIMARY KEY,
  source_id            TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  version_no           INTEGER NOT NULL,
  parse_state          TEXT NOT NULL DEFAULT 'parsed',  -- parsed, normalized, failed
  extracted_text_path  TEXT,
  normalized_json_path TEXT,
  text_sha256          TEXT,
  token_count          INTEGER,
  parser_name          TEXT,
  parser_version       TEXT,
  created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  UNIQUE(source_id, version_no)
);

CREATE INDEX idx_source_versions_source ON source_versions(source_id, version_no DESC);

-- -----------------------------------------------------------------------
-- Entities (concepts, people, orgs, tools, etc.)
-- -----------------------------------------------------------------------
CREATE TABLE entities (
  id                    TEXT PRIMARY KEY,
  project_id            TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  entity_type           TEXT NOT NULL,  -- concept, person, org, method, dataset, tool, topic
  canonical_name        TEXT NOT NULL,
  display_name          TEXT NOT NULL,
  aliases_json          TEXT NOT NULL DEFAULT '[]',
  description           TEXT,
  confidence            REAL NOT NULL DEFAULT 1.0,
  created_from_source_id TEXT REFERENCES sources(id) ON DELETE SET NULL,
  created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  updated_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  metadata_json         TEXT NOT NULL DEFAULT '{}'
);

CREATE UNIQUE INDEX uq_entities_project_type_name ON entities(project_id, entity_type, canonical_name);
CREATE INDEX idx_entities_project_type ON entities(project_id, entity_type);

-- -----------------------------------------------------------------------
-- Claims
-- -----------------------------------------------------------------------
CREATE TABLE claims (
  id                    TEXT PRIMARY KEY,
  project_id            TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  claim_text            TEXT NOT NULL,
  claim_kind            TEXT NOT NULL DEFAULT 'fact',  -- fact, comparison, definition, open_question, recommendation
  confidence            REAL NOT NULL DEFAULT 0.5,
  status                TEXT NOT NULL DEFAULT 'active', -- active, weak, contradicted, stale
  created_from_source_id TEXT REFERENCES sources(id) ON DELETE SET NULL,
  created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  updated_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  metadata_json         TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_claims_project_status ON claims(project_id, status);

-- -----------------------------------------------------------------------
-- Relations (generic edge table — the graph store)
-- -----------------------------------------------------------------------
CREATE TABLE relations (
  id                TEXT PRIMARY KEY,
  project_id        TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  from_kind         TEXT NOT NULL,  -- source, entity, claim, page, artifact, content_unit
  from_id           TEXT NOT NULL,
  relation_type     TEXT NOT NULL,  -- mentions, supports, links_to, about, related_to, derived_from, contradicts, aliases
  to_kind           TEXT NOT NULL,
  to_id             TEXT NOT NULL,
  confidence        REAL NOT NULL DEFAULT 1.0,
  evidence_source_id TEXT REFERENCES sources(id) ON DELETE SET NULL,
  created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  metadata_json     TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_relations_from         ON relations(project_id, from_kind, from_id, relation_type);
CREATE INDEX idx_relations_to           ON relations(project_id, to_kind, to_id, relation_type);
CREATE INDEX idx_relations_project_type ON relations(project_id, relation_type);

-- -----------------------------------------------------------------------
-- Pages (compiled wiki pages)
-- -----------------------------------------------------------------------
CREATE TABLE pages (
  id               TEXT PRIMARY KEY,
  project_id       TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  page_type        TEXT NOT NULL,  -- source_summary, concept, comparison, timeline, glossary, question, index
  title            TEXT NOT NULL,
  slug             TEXT NOT NULL,
  file_path        TEXT NOT NULL,
  frontmatter_json TEXT NOT NULL DEFAULT '{}',
  body_sha256      TEXT,
  status           TEXT NOT NULL DEFAULT 'active',  -- active, stale, draft, deleted
  created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  updated_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  UNIQUE(project_id, file_path)
);

CREATE INDEX idx_pages_project_type ON pages(project_id, page_type);
CREATE INDEX idx_pages_project_slug ON pages(project_id, slug);

-- -----------------------------------------------------------------------
-- Query runs
-- -----------------------------------------------------------------------
CREATE TABLE query_runs (
  id                      TEXT PRIMARY KEY,
  project_id              TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  prompt_text             TEXT NOT NULL,
  task_type               TEXT NOT NULL,
  output_format           TEXT NOT NULL,
  model_tier              TEXT NOT NULL,  -- fast, balanced, best
  status                  TEXT NOT NULL,  -- queued, planning, gathering, synthesizing, succeeded, failed, cancelled
  plan_json               TEXT NOT NULL DEFAULT '{}',
  retrieval_json          TEXT NOT NULL DEFAULT '{}',
  result_artifact_id      TEXT,           -- filled in when done (FK added after artifacts table)
  estimated_input_tokens  INTEGER,
  estimated_output_tokens INTEGER,
  actual_input_tokens     INTEGER,
  actual_output_tokens    INTEGER,
  started_at              TEXT,
  finished_at             TEXT,
  created_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX idx_query_runs_project_created ON query_runs(project_id, created_at DESC);

-- -----------------------------------------------------------------------
-- Artifacts (generated output files outside the canonical wiki)
-- -----------------------------------------------------------------------
CREATE TABLE artifacts (
  id                  TEXT PRIMARY KEY,
  project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  artifact_type       TEXT NOT NULL,  -- answer, report, slides, checklist, study_guide, memo
  title               TEXT NOT NULL,
  file_path           TEXT NOT NULL,
  source_query_run_id TEXT REFERENCES query_runs(id) ON DELETE SET NULL,
  created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  updated_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  status              TEXT NOT NULL DEFAULT 'active',
  metadata_json       TEXT NOT NULL DEFAULT '{}',
  UNIQUE(project_id, file_path)
);

CREATE INDEX idx_artifacts_project_type ON artifacts(project_id, artifact_type);

-- Add FK from query_runs to artifacts (done after artifacts table creation)
-- SQLite doesn't support ADD CONSTRAINT, so we rely on the app layer for this check.

-- -----------------------------------------------------------------------
-- Content units (retrieval units for FTS and ranking)
-- -----------------------------------------------------------------------
CREATE TABLE content_units (
  id          TEXT PRIMARY KEY,
  project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  unit_type   TEXT NOT NULL,   -- source_chunk, source_summary, page_body, page_section, claim, artifact_body
  parent_kind TEXT NOT NULL,   -- source, page, artifact, claim
  parent_id   TEXT NOT NULL,
  ordinal     INTEGER NOT NULL DEFAULT 0,
  title       TEXT,
  body        TEXT NOT NULL,
  token_count INTEGER,
  source_id   TEXT REFERENCES sources(id) ON DELETE SET NULL,
  page_id     TEXT REFERENCES pages(id) ON DELETE SET NULL,
  artifact_id TEXT REFERENCES artifacts(id) ON DELETE SET NULL,
  claim_id    TEXT REFERENCES claims(id) ON DELETE SET NULL,
  created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_content_units_project_parent ON content_units(project_id, parent_kind, parent_id);
CREATE INDEX idx_content_units_project_type   ON content_units(project_id, unit_type);

-- -----------------------------------------------------------------------
-- Jobs
-- -----------------------------------------------------------------------
CREATE TABLE jobs (
  id           TEXT PRIMARY KEY,
  project_id   TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  job_type     TEXT NOT NULL,  -- ingest, normalize, compile, query, maintenance
  status       TEXT NOT NULL,  -- queued, running, succeeded, failed, cancelled
  priority     INTEGER NOT NULL DEFAULT 100,
  requested_by TEXT NOT NULL DEFAULT 'user',  -- user, system, maintenance
  payload_json TEXT NOT NULL DEFAULT '{}',
  result_json  TEXT NOT NULL DEFAULT '{}',
  progress     REAL NOT NULL DEFAULT 0.0,
  started_at   TEXT,
  finished_at  TEXT,
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX idx_jobs_project_status ON jobs(project_id, status, created_at DESC);
CREATE INDEX idx_jobs_project_type   ON jobs(project_id, job_type, created_at DESC);

-- -----------------------------------------------------------------------
-- Job events (structured log entries per job)
-- -----------------------------------------------------------------------
CREATE TABLE job_events (
  id           TEXT PRIMARY KEY,
  job_id       TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  seq_no       INTEGER NOT NULL,
  level        TEXT NOT NULL,       -- info, warn, error, debug
  event_type   TEXT NOT NULL,       -- progress, step_started, step_finished, log
  message      TEXT,
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  UNIQUE(job_id, seq_no)
);

CREATE INDEX idx_job_events_job ON job_events(job_id, seq_no);

-- -----------------------------------------------------------------------
-- Maintenance findings
-- -----------------------------------------------------------------------
CREATE TABLE maintenance_findings (
  id              TEXT PRIMARY KEY,
  project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  finding_type    TEXT NOT NULL,   -- orphan_page, duplicate_entity, weak_claim, stale_page, missing_comparison
  severity        TEXT NOT NULL,   -- low, medium, high
  subject_kind    TEXT NOT NULL,
  subject_id      TEXT NOT NULL,
  title           TEXT NOT NULL,
  description     TEXT NOT NULL,
  suggestion_json TEXT NOT NULL DEFAULT '{}',
  status          TEXT NOT NULL DEFAULT 'open',  -- open, ignored, accepted, resolved
  created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX idx_maintenance_project_status ON maintenance_findings(project_id, status, created_at DESC);

-- -----------------------------------------------------------------------
-- Record this migration
-- -----------------------------------------------------------------------
INSERT INTO schema_migrations(version, name)
VALUES (1, '0001_initial');

COMMIT;
