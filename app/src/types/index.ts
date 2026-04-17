/**
 * Shared TypeScript types matching the engine's data model.
 * Architecture: §14, §40.3
 */

// ---------------------------------------------------------------------------
// Project
// ---------------------------------------------------------------------------

export interface Project {
  project_id: string;
  name: string;
  slug: string;
  root_path: string;
  status: "active" | "archived" | "deleted";
  config?: ProjectConfig;
  created_at?: string;
  updated_at?: string;
}

export interface ProjectConfig {
  default_model_tier?: ModelTier;
  trusted_domains?: string[];
  privacy_mode?: "local_first" | "restricted_cloud" | "local_only";
  obsidian_vault_path?: string;
}

// ---------------------------------------------------------------------------
// Sources
// ---------------------------------------------------------------------------

export type SourceType =
  | "bookmark"
  | "url_article"
  | "pdf"
  | "markdown_note"
  | "repo_doc"
  | "image"
  | "dataset";

export type IngestState =
  | "new"
  | "needs_review"
  | "approved"
  | "rejected"
  | "promoted";

export type SourceStatus =
  | "pending"
  | "normalized"
  | "compiled"
  | "rejected"
  | "failed";

export type TrustLevel = "unknown" | "low" | "medium" | "high";

export interface Source {
  id: string;
  source_type: SourceType;
  origin_type: string;
  title: string | null;
  canonical_url: string | null;
  display_url: string | null;
  domain: string | null;
  raw_path: string;
  trust_level: TrustLevel;
  status: SourceStatus;
  ingest_state: IngestState;
  language_code: string | null;
  source_date: string | null;
  created_at: string;
  updated_at: string;
  summary?: string | null;
}

// ---------------------------------------------------------------------------
// Pages
// ---------------------------------------------------------------------------

export type PageType =
  | "source_summary"
  | "concept"
  | "comparison"
  | "timeline"
  | "question"
  | "index"
  | "glossary";

export interface WikiPage {
  id: string;
  page_type: PageType;
  title: string;
  slug: string;
  file_path: string;
  status: "active" | "stale" | "draft" | "deleted";
  created_at: string;
  updated_at: string;
}

// ---------------------------------------------------------------------------
// Artifacts
// ---------------------------------------------------------------------------

export type ArtifactType =
  | "answer"
  | "report"
  | "slides"
  | "checklist"
  | "study_guide"
  | "memo"
  | "comparison";

export interface Artifact {
  id: string;
  artifact_type: ArtifactType;
  title: string;
  file_path: string;
  status: "active" | "archived";
  created_at: string;
  updated_at: string;
  content?: string;
}

// ---------------------------------------------------------------------------
// Jobs
// ---------------------------------------------------------------------------

export type JobStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled";

export interface Job {
  id: string;
  job_type: string;
  status: JobStatus;
  progress: number;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
}

// ---------------------------------------------------------------------------
// Maintenance findings
// ---------------------------------------------------------------------------

export type FindingType =
  | "orphan_page"
  | "duplicate_entity"
  | "weak_claim"
  | "stale_page"
  | "missing_comparison";

export type FindingSeverity = "low" | "medium" | "high";

export interface MaintenanceFinding {
  id: string;
  finding_type: FindingType;
  severity: FindingSeverity;
  subject_kind: string;
  subject_id: string;
  title: string;
  description: string;
  suggestion_json: string;
  status: "open" | "ignored" | "accepted" | "resolved";
  created_at: string;
}

// ---------------------------------------------------------------------------
// Query
// ---------------------------------------------------------------------------

export type TaskType =
  | "auto"
  | "answer"
  | "comparison"
  | "study_guide"
  | "reading_plan"
  | "report"
  | "slides"
  | "checklist"
  | "open_questions";

export type ModelTier = "fast" | "balanced" | "best";

export type OutputFormat =
  | "markdown_report"
  | "markdown_note"
  | "slides"
  | "checklist";

export interface QueryRun {
  id: string;
  prompt_text: string;
  task_type: TaskType;
  status: string;
  result_artifact_id: string | null;
  created_at: string;
}

// ---------------------------------------------------------------------------
// Search
// ---------------------------------------------------------------------------

export interface SearchResult {
  content_unit_id: string;
  unit_type: string;
  title: string | null;
  body_excerpt: string;
  parent_kind: string;
  parent_id: string;
  source_id: string | null;
  page_id: string | null;
  score: number;
}

// ---------------------------------------------------------------------------
// Visualization
// ---------------------------------------------------------------------------

export interface KnowledgeLeadIdea {
  id: string;
  label: string;
  relation_type: string;
  kind: "explicit" | "inferred";
  weight: number;
  shared_sources: number;
  evidence_count: number;
  lexical_overlap: number;
  provenance_kind: "explicit_relation" | "shared_support" | "hybrid";
  provenance_summary: string;
  source_titles: string[];
}

export interface KnowledgeMapNode {
  id: string;
  label: string;
  entity_type: string;
  summary: string;
  group: string;
  group_label: string;
  group_rationale: string;
  supporting_sources: string[];
  page_id?: string | null;
  page_title?: string | null;
  source_count: number;
  degree: number;
  confidence: number;
  lead_ideas: KnowledgeLeadIdea[];
  seed: number;
}

export interface KnowledgeMapEdge {
  id: string;
  source: string;
  target: string;
  relation_type: string;
  kind: "explicit" | "inferred";
  weight: number;
  shared_sources: number;
  evidence_count: number;
  lexical_overlap: number;
  provenance_kind: "explicit_relation" | "shared_support" | "hybrid";
  provenance_summary: string;
  source_titles: string[];
}

export interface KnowledgeMapGroup {
  id: string;
  label: string;
  size: number;
  themes: string[];
  rationale: string;
  representative_nodes: string[];
  explicit_edge_count: number;
  inferred_edge_count: number;
  source_count: number;
}

export interface KnowledgeMap {
  project_id: string;
  generated_at: string;
  nodes: KnowledgeMapNode[];
  edges: KnowledgeMapEdge[];
  groups: KnowledgeMapGroup[];
  stats: {
    node_count: number;
    edge_count: number;
    group_count: number;
    explicit_edge_count: number;
    inferred_edge_count: number;
  };
}
