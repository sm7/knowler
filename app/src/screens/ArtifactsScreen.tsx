import { useEffect, useState } from "react";
import { engineCall, revealInFinder, openInObsidian } from "../lib/ipc";
import type { Project } from "../types";

interface Page {
  id: string;
  page_type: string;
  title: string;
  slug: string;
  file_path: string;
  status: string;
  created_at: string;
  content?: string;
}

interface Props {
  project: Project | null;
}

export function ArtifactsScreen({ project }: Props) {
  const [pages, setPages] = useState<Page[]>([]);
  const [selected, setSelected] = useState<Page | null>(null);
  const [loading, setLoading] = useState(false);
  const [filter, setFilter] = useState<"all" | "source_summary" | "concept" | "question">("all");

  const load = async () => {
    if (!project) return;
    setLoading(true);
    try {
      const params: Record<string, string> = { project_id: project.project_id };
      if (filter !== "all") params.page_type = filter;
      const r = await engineCall<{ pages: Page[] }>("pages.list", params);
      setPages(r.pages ?? []);
    } catch (e) {
      console.error(e);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, [project, filter]);

  const handleSelect = async (page: Page) => {
    setSelected(page);
    if (page.content !== undefined) return;
    try {
      const full = await engineCall<{ content?: string }>("pages.get", {
        project_id: project!.project_id,
        page_id: page.id,
      });
      setSelected({ ...page, content: full.content ?? "(No content)" });
    } catch {
      setSelected({ ...page, content: "(Could not load page content)" });
    }
  };

  if (!project) {
    return (
      <div className="screen">
        <div className="screen-header"><h1>Wiki</h1></div>
        <div className="empty-state"><h3>No project open</h3></div>
      </div>
    );
  }

  const sourceSummaries = pages.filter(p => p.page_type === "source_summary");
  const concepts = pages.filter(p => p.page_type === "concept");

  return (
    <div className="screen">
      <div className="screen-header">
        <h1>Wiki</h1>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          {pages.length > 0 && (
            <span style={{ fontSize: 12, color: "var(--color-text-muted)" }}>
              {sourceSummaries.length} sources · {concepts.length} concepts
            </span>
          )}
          <button className="btn" onClick={load} disabled={loading}>
            {loading ? "Loading…" : "Refresh"}
          </button>
        </div>
      </div>

      {pages.length > 0 && (
        <div className="screen-toolbar">
          {(["all", "source_summary", "concept", "question"] as const).map((f) => (
            <button
              key={f}
              className={`btn${filter === f ? " btn-primary" : ""}`}
              style={{ fontSize: 12, padding: "4px 10px" }}
              onClick={() => setFilter(f)}
            >
              {f === "all" ? "All" : f === "source_summary" ? "Sources" : f === "concept" ? "Concepts" : "Questions"}
            </button>
          ))}
        </div>
      )}

      <div style={{ display: "flex", flex: 1, overflow: "hidden" }}>
        {/* List */}
        <div style={{ width: 280, borderRight: "1px solid var(--color-border)", overflowY: "auto", flexShrink: 0 }}>
          {loading ? (
            <p style={{ padding: 20, fontSize: 13, color: "var(--color-text-muted)" }}>Loading…</p>
          ) : pages.length === 0 ? (
            <div className="empty-state" style={{ padding: 24 }}>
              <h3>No pages yet</h3>
              <p>Import files and run a build to generate source summaries and concept pages.</p>
              <p style={{ marginTop: 8, fontSize: 11, color: "var(--color-text-muted)" }}>
                Save an API key in Settings for richer AI-generated content.
              </p>
            </div>
          ) : (
            <div className="source-list">
              {pages.map((page) => (
                <div
                  key={page.id}
                  className={`source-row${selected?.id === page.id ? " selected" : ""}`}
                  onClick={() => handleSelect(page)}
                >
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div className="truncate" style={{ fontSize: 13, fontWeight: 500 }}>
                      {page.title}
                    </div>
                    <div style={{ fontSize: 11, color: "var(--color-text-muted)", marginTop: 2 }}>
                      {page.page_type === "source_summary"
                        ? "Source Summary"
                        : page.page_type === "concept"
                        ? "Concept"
                        : page.page_type === "question"
                        ? "Question"
                        : page.page_type} · {new Date(page.created_at).toLocaleDateString()}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Preview */}
        <div style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" }}>
          {selected ? (
            <>
              <div style={{
                padding: "10px 16px",
                borderBottom: "1px solid var(--color-border)",
                display: "flex",
                gap: 8,
                alignItems: "center",
                background: "var(--color-surface)",
              }}>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontWeight: 600, fontSize: 13 }}>{selected.title}</div>
                  <div style={{ fontSize: 11, color: "var(--color-text-muted)" }}>
                    {selected.page_type === "source_summary"
                      ? "Source Summary"
                      : selected.page_type === "concept"
                      ? "Concept"
                      : selected.page_type === "question"
                      ? "Question"
                      : selected.page_type} · {selected.file_path}
                  </div>
                </div>
                <button
                  className="btn btn-ghost"
                  style={{ fontSize: 12 }}
                  onClick={() => revealInFinder(selected.file_path)}
                >
                  Show in Finder
                </button>
                <button
                  className="btn btn-ghost"
                  style={{ fontSize: 12 }}
                  onClick={() => openInObsidian(selected.file_path, project.config?.obsidian_vault_path)}
                >
                  Obsidian
                </button>
              </div>
              <div style={{ flex: 1, overflowY: "auto" }}>
                {selected.content !== undefined ? (
                  <pre className="artifact-preview" style={{ whiteSpace: "pre-wrap", padding: 20, fontSize: 13, lineHeight: 1.6, fontFamily: "var(--font-mono)" }}>
                    {selected.content}
                  </pre>
                ) : (
                  <div style={{ padding: 20, color: "var(--color-text-muted)", fontSize: 13 }}>Loading…</div>
                )}
              </div>
            </>
          ) : (
            <div className="empty-state">
              <h3>Select a page</h3>
              <p>Click a page to preview its content.</p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
