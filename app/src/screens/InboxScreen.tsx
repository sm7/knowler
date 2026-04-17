import { useEffect, useRef, useState } from "react";
import { engineCall } from "../lib/ipc";
import type { Project, Source, IngestState } from "../types";

interface Props {
  project: Project | null;
}

interface BrowserBookmarkSource {
  browser_id: string;
  browser_name: string;
  profile: string | null;
  label: string;
  detail: string;
  is_default?: boolean;
}

const FILTER_TABS: { label: string; value: IngestState | "all" }[] = [
  { label: "All", value: "all" },
  { label: "Needs Review", value: "needs_review" },
  { label: "Approved", value: "approved" },
  { label: "Rejected", value: "rejected" },
];

/** Upload files to the engine server and return the saved file paths. */
async function uploadFiles(files: File[], projectId: string): Promise<string[]> {
  const BASE = import.meta.env.DEV ? "http://localhost:7842" : "";
  const form = new FormData();
  files.forEach((f) => form.append("files", f));
  form.append("project_id", projectId);
  const res = await fetch(`${BASE}/api/upload`, { method: "POST", body: form });
  if (!res.ok) throw new Error(`Upload failed: ${res.statusText}`);
  const data = await res.json() as { paths: string[] };
  return data.paths;
}

export function InboxScreen({ project }: Props) {
  const [sources, setSources] = useState<Source[]>([]);
  const [filter, setFilter] = useState<IngestState | "all">("all");
  const [selected, setSelected] = useState<Source | null>(null);
  const [loading, setLoading] = useState(false);
  const [importing, setImporting] = useState(false);
  const [importError, setImportError] = useState<string | null>(null);
  const [addUrlInput, setAddUrlInput] = useState("");
  const [addUrlOpen, setAddUrlOpen] = useState(false);
  const [browserBookmarkSources, setBrowserBookmarkSources] = useState<BrowserBookmarkSource[]>([]);

  const pdfInputRef = useRef<HTMLInputElement>(null);
  const bookmarkInputRef = useRef<HTMLInputElement>(null);

  const loadSources = async () => {
    if (!project) return;
    setLoading(true);
    try {
      const params: Record<string, unknown> = { project_id: project.project_id };
      if (filter !== "all") params.ingest_state = filter;
      const r = await engineCall<{ sources: Source[] }>("sources.list", params);
      setSources(r.sources ?? []);
    } catch (e) {
      console.error(e);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { loadSources(); }, [project, filter]);

  useEffect(() => {
    if (!project) {
      setBrowserBookmarkSources([]);
      return;
    }
    let cancelled = false;
    engineCall<{ sources: BrowserBookmarkSource[] }>("sources.listBrowserBookmarkSources")
      .then((result) => {
        if (!cancelled) setBrowserBookmarkSources(result.sources ?? []);
      })
      .catch((err) => {
        console.error(err);
        if (!cancelled) setBrowserBookmarkSources([]);
      });
    return () => { cancelled = true; };
  }, [project?.project_id]);

  const handlePDFsSelected = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files ?? []);
    e.target.value = "";
    if (!files.length || !project) return;
    setImporting(true);
    setImportError(null);
    try {
      const paths = await uploadFiles(files, project.project_id);
      await engineCall("sources.importFiles", {
        project_id: project.project_id,
        file_paths: paths,
      });
      setTimeout(() => { loadSources(); setImporting(false); }, 1500);
    } catch (err: unknown) {
      setImportError(err instanceof Error ? err.message : String(err));
      setImporting(false);
    }
  };

  const handleBookmarkSelected = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file || !project) return;
    setImporting(true);
    setImportError(null);
    try {
      const paths = await uploadFiles([file], project.project_id);
      await engineCall("sources.importBookmarks", {
        project_id: project.project_id,
        bookmark_file_path: paths[0],
      });
      setTimeout(() => { loadSources(); setImporting(false); }, 1500);
    } catch (err: unknown) {
      setImportError(err instanceof Error ? err.message : String(err));
      setImporting(false);
    }
  };

  const handleAddUrl = async () => {
    if (!project || !addUrlInput.trim()) return;
    setImporting(true);
    setImportError(null);
    try {
      await engineCall("sources.importUrls", {
        project_id: project.project_id,
        urls: [addUrlInput.trim()],
      });
      setAddUrlInput("");
      setAddUrlOpen(false);
      setTimeout(() => { loadSources(); setImporting(false); }, 1000);
    } catch (err: unknown) {
      setImportError(err instanceof Error ? err.message : String(err));
      setImporting(false);
    }
  };

  const handleBrowserBookmarkImport = async (source: BrowserBookmarkSource) => {
    if (!project) return;
    setImporting(true);
    setImportError(null);
    try {
      await engineCall("sources.importBrowserBookmarks", {
        project_id: project.project_id,
        browser_id: source.browser_id,
        browser_profile: source.profile,
      });
      setTimeout(() => { loadSources(); setImporting(false); }, 1500);
    } catch (err: unknown) {
      setImportError(err instanceof Error ? err.message : String(err));
      setImporting(false);
    }
  };

  const handlePromote = async (source: Source) => {
    if (!project) return;
    await engineCall("sources.promote", {
      project_id: project.project_id,
      source_id: source.id,
    });
    setSources((prev) =>
      prev.map((s) => s.id === source.id ? { ...s, ingest_state: "approved" } : s)
    );
  };

  const handleReject = async (source: Source) => {
    if (!project) return;
    await engineCall("sources.reject", {
      project_id: project.project_id,
      source_id: source.id,
    });
    setSources((prev) =>
      prev.map((s) => s.id === source.id ? { ...s, ingest_state: "rejected" } : s)
    );
  };

  if (!project) {
    return (
      <div className="screen">
        <div className="screen-header"><h1>Inbox</h1></div>
        <div className="empty-state">
          <h3>No project open</h3>
          <p>Go to Projects and open or create a project first.</p>
        </div>
      </div>
    );
  }

  const filteredSources =
    filter === "all" ? sources : sources.filter((s) => s.ingest_state === filter);
  const defaultBrowserSource =
    browserBookmarkSources.find((source) => source.is_default) ?? null;
  const chromeBrowserSource =
    browserBookmarkSources.find((source) => source.browser_id === "chrome") ?? null;
  const showChromeButton =
    chromeBrowserSource !== null &&
    chromeBrowserSource.browser_id !== defaultBrowserSource?.browser_id;
  const defaultBrowserLabel = defaultBrowserSource?.browser_id === "chrome"
    ? "Import Chrome"
    : defaultBrowserSource
      ? `Import Default (${defaultBrowserSource.browser_name})`
      : null;

  return (
    <div className="screen">
      {/* Hidden file inputs */}
      <input
        ref={pdfInputRef}
        type="file"
        multiple
        accept=".pdf,.txt,.md"
        style={{ display: "none" }}
        onChange={handlePDFsSelected}
      />
      <input
        ref={bookmarkInputRef}
        type="file"
        accept=".html,.htm"
        style={{ display: "none" }}
        onChange={handleBookmarkSelected}
      />

      <div className="screen-header">
        <h1>Inbox — {project.name}</h1>
        <div style={{ display: "flex", gap: 8 }}>
          <button
            className="btn btn-primary"
            onClick={() => pdfInputRef.current?.click()}
            disabled={importing}
          >
            {importing ? "Importing…" : "+ Import Files"}
          </button>
          <button className="btn" onClick={() => setAddUrlOpen((v) => !v)} disabled={importing}>
            + URL
          </button>
          {defaultBrowserSource && defaultBrowserLabel && (
            <button
              className="btn"
              onClick={() => void handleBrowserBookmarkImport(defaultBrowserSource)}
              disabled={importing}
              title={defaultBrowserSource.detail}
            >
              {defaultBrowserLabel}
            </button>
          )}
          {showChromeButton && chromeBrowserSource && (
            <button
              className="btn"
              onClick={() => void handleBrowserBookmarkImport(chromeBrowserSource)}
              disabled={importing}
              title={chromeBrowserSource.detail}
            >
              Import Chrome
            </button>
          )}
          <button
            className="btn"
            onClick={() => bookmarkInputRef.current?.click()}
            disabled={importing}
          >
            Bookmark HTML
          </button>
        </div>
      </div>

      {/* URL input bar */}
      {addUrlOpen && (
        <div className="screen-toolbar" style={{ gap: 8 }}>
          <input
            type="url"
            placeholder="https://..."
            value={addUrlInput}
            onChange={(e) => setAddUrlInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleAddUrl()}
            style={{ flex: 1 }}
            autoFocus
          />
          <button className="btn btn-primary" onClick={handleAddUrl} disabled={!addUrlInput.trim()}>
            Add
          </button>
          <button className="btn" onClick={() => setAddUrlOpen(false)}>Cancel</button>
        </div>
      )}

      {importError && (
        <div style={{ padding: "8px 16px", background: "var(--color-danger-bg, #fee)", color: "var(--color-danger)", fontSize: 12 }}>
          {importError}
        </div>
      )}

      {/* Empty state with big CTA */}
      {!loading && sources.length === 0 && (
        <div className="empty-state" style={{ flex: "none", padding: "32px 16px" }}>
          <h3>No sources yet</h3>
          <p>Import PDFs, URLs, or browser bookmarks to start building the wiki.</p>
          <button
            className="btn btn-primary"
            style={{ marginTop: 12 }}
            onClick={() => pdfInputRef.current?.click()}
          >
            + Import Files
          </button>
        </div>
      )}

      {/* Filter tabs */}
      {sources.length > 0 && (
        <div className="screen-toolbar">
          {FILTER_TABS.map((tab) => (
            <button
              key={tab.value}
              className={`btn${filter === tab.value ? " btn-primary" : ""}`}
              style={{ fontSize: 12, padding: "4px 10px" }}
              onClick={() => setFilter(tab.value)}
            >
              {tab.label}
            </button>
          ))}
          <span style={{ marginLeft: "auto", fontSize: 12, color: "var(--color-text-muted)" }}>
            {filteredSources.length} sources
          </span>
        </div>
      )}

      <div style={{ display: "flex", flex: 1, overflow: "hidden" }}>
        {/* Source list */}
        <div style={{ flex: 1, overflowY: "auto", borderRight: "1px solid var(--color-border)" }}>
          {loading ? (
            <p style={{ padding: 20, fontSize: 13, color: "var(--color-text-muted)" }}>Loading…</p>
          ) : (
            <div className="source-list">
              {filteredSources.map((src) => (
                <div
                  key={src.id}
                  className={`source-row${selected?.id === src.id ? " selected" : ""}`}
                  onClick={() => setSelected(src)}
                >
                  <div className="source-row-title truncate">
                    {src.title || src.canonical_url || src.id}
                  </div>
                  <div className="source-row-meta">
                    <span className="badge badge-gray">{src.source_type}</span>
                    <IngestStateBadge state={src.ingest_state} />
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Detail / action panel */}
        {selected && (
          <div style={{ width: 320, overflowY: "auto", padding: 16, flexShrink: 0 }}>
            <h2 style={{ marginBottom: 8, fontSize: 14 }}>
              {selected.title || selected.canonical_url || "Untitled"}
            </h2>

            <dl style={{ fontSize: 12, display: "grid", gridTemplateColumns: "80px 1fr", gap: "4px 8px", marginBottom: 16 }}>
              <dt style={{ color: "var(--color-text-muted)" }}>Type</dt>
              <dd>{selected.source_type}</dd>
              <dt style={{ color: "var(--color-text-muted)" }}>State</dt>
              <dd><IngestStateBadge state={selected.ingest_state} /></dd>
              <dt style={{ color: "var(--color-text-muted)" }}>Trust</dt>
              <dd>{selected.trust_level}</dd>
              {selected.domain && (
                <>
                  <dt style={{ color: "var(--color-text-muted)" }}>Domain</dt>
                  <dd>{selected.domain}</dd>
                </>
              )}
            </dl>

            {selected.summary && (
              <p style={{ fontSize: 12, lineHeight: 1.5, color: "var(--color-text-secondary)", marginBottom: 16 }}>
                {selected.summary}
              </p>
            )}

            {selected.ingest_state === "needs_review" && (
              <div style={{ display: "flex", gap: 8 }}>
                <button
                  className="btn btn-primary"
                  style={{ flex: 1 }}
                  onClick={() => handlePromote(selected)}
                >
                  Approve
                </button>
                <button
                  className="btn"
                  style={{ flex: 1, color: "var(--color-danger)" }}
                  onClick={() => handleReject(selected)}
                >
                  Reject
                </button>
              </div>
            )}

            {selected.ingest_state === "approved" && (
              <p style={{ fontSize: 12, color: "var(--color-text-muted)" }}>
                Approved — will be included in the next Build.
              </p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function IngestStateBadge({ state }: { state: IngestState }) {
  const map: Record<IngestState, string> = {
    new: "badge-gray",
    needs_review: "badge-yellow",
    approved: "badge-green",
    rejected: "badge-red",
    promoted: "badge",
  };
  return <span className={`badge ${map[state] ?? "badge-gray"}`}>{state.replace("_", " ")}</span>;
}
