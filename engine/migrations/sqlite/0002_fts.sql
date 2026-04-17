-- Migration 0002: Full-text search virtual table
-- content_units_fts is kept in sync from application code.

BEGIN;

CREATE VIRTUAL TABLE content_units_fts USING fts5(
  title,
  body,
  project_id UNINDEXED,
  content='',
  tokenize='porter unicode61'
);

INSERT INTO schema_migrations(version, name)
VALUES (2, '0002_fts');

COMMIT;
