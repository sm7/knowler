import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { engineCall, onJobProgress, onJobCompleted, pickDirectory } from "../lib/ipc";
import type { Project } from "../types";

interface Props {
  onProjectOpen: (p: Project) => void;
  onProjectDelete: (projectId: string) => void;
  activeProject: Project | null;
}

type Step = "list" | "creating" | "importing";

interface Progress {
  message: string;
  pct: number;
}

export function ProjectsScreen({ onProjectOpen, onProjectDelete, activeProject }: Props) {
  const navigate = useNavigate();
  const [projects, setProjects] = useState<Project[]>([]);
  const [step, setStep] = useState<Step>("list");
  const [newName, setNewName] = useState("");
  const [newPath, setNewPath] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [progress, setProgress] = useState<Progress | null>(null);
  const [createdProject, setCreatedProject] = useState<Project | null>(null);

  const loadProjects = async () => {
    try {
      const r = await engineCall<{ projects: Project[] }>("project.list", {});
      setProjects(r.projects ?? []);
    } catch {
      // ignore project list errors on initial load
    }
  };

  useEffect(() => {
    void loadProjects();
  }, []);

  // Listen for job progress while importing
  useEffect(() => {
    if (step !== "importing") return;
    const unsubs: Array<() => void> = [];

    onJobProgress((e) => {
      setProgress({ message: e.message ?? "Processing…", pct: (e.progress ?? 0) * 100 });
    }).then((fn) => unsubs.push(fn));

    onJobCompleted((e) => {
      if (e.status === "succeeded") {
        setProgress({ message: "Done!", pct: 100 });
        setTimeout(() => {
          if (createdProject) {
            setProjects((prev) =>
              prev.find((p) => p.project_id === createdProject.project_id)
                ? prev
                : [createdProject, ...prev]
            );
            onProjectOpen(createdProject);
            navigate("/inbox");
          }
          setStep("list");
          setProgress(null);
          setCreatedProject(null);
        }, 800);
      }
    }).then((fn) => unsubs.push(fn));

    return () => unsubs.forEach((fn) => fn());
  }, [step, createdProject]);

  const handleChooseFolder = async () => {
    const selectedPath = await pickDirectory(
      "Select the folder that should become this Knowler project"
    );
    if (!selectedPath) return;
    setNewPath(selectedPath);
    if (!newName) {
      const folderName = selectedPath.split("/").filter(Boolean).pop() ?? "";
      setNewName(folderName);
    }
  };

  const handleCreate = async () => {
    if (!newName.trim() || !newPath.trim()) return;
    setError(null);
    setNotice(null);
    setStep("creating");
    try {
      const result = await engineCall<Project>("project.create", {
        name: newName.trim(),
        root_path: newPath.trim(),
      });
      setCreatedProject(result);
      setStep("importing");
      setProgress({ message: "Scanning folder…", pct: 0 });

      const r = await engineCall<{ files_found: number }>("project.importAndBuild", {
        project_id: result.project_id,
      });
      setProgress({ message: `Importing ${r.files_found} files…`, pct: 5 });
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
      setStep("creating");
    }
  };

  const handleOpenExisting = async (folderName: string) => {
    if (!folderName) return;
    setError(null);
    setNotice(null);
    try {
      const result = await engineCall<Project>("project.open", { root_path: folderName });
      setProjects((prev) =>
        prev.find((p) => p.project_id === result.project_id) ? prev : [result, ...prev]
      );
      onProjectOpen(result);
      navigate("/inbox");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const handleOpen = async (p: Project) => {
    setError(null);
    setNotice(null);
    try {
      const result = await engineCall<Project>("project.open", { root_path: p.root_path });
      onProjectOpen(result);
      navigate("/inbox");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const handleDelete = async (project: Project) => {
    const confirmed = window.confirm(
      `Delete "${project.name}" from Knowler?\n\nThis removes it from the project list but keeps the folder and files on disk.`
    );
    if (!confirmed) return;

    setError(null);
    setNotice(null);
    try {
      await engineCall("project.delete", { project_id: project.project_id });
      setProjects((prev) => prev.filter((p) => p.project_id !== project.project_id));
      onProjectDelete(project.project_id);
      setNotice(`Deleted "${project.name}" from Knowler. Files on disk were kept.`);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  // ── Importing progress screen ──────────────────────────────────────────
  if (step === "importing") {
    return (
      <div className="screen">
        <div className="screen-header"><h1>Setting up {createdProject?.name}…</h1></div>
        <div className="empty-state">
          <div style={{ width: "100%", maxWidth: 360 }}>
            <p style={{ marginBottom: 16, color: "var(--color-text-muted)", fontSize: 14 }}>
              {progress?.message ?? "Working…"}
            </p>
            <div style={{
              height: 6, borderRadius: 3,
              background: "var(--color-border)",
              overflow: "hidden",
            }}>
              <div style={{
                height: "100%",
                width: `${progress?.pct ?? 0}%`,
                background: "var(--color-accent)",
                transition: "width 0.4s ease",
                borderRadius: 3,
              }} />
            </div>
          </div>
        </div>
      </div>
    );
  }

  // ── Create form ────────────────────────────────────────────────────────
  if (step === "creating") {
    return (
      <div className="screen">
        <div className="screen-header"><h1>New Project</h1></div>

        <div style={{ maxWidth: 480, padding: 32, display: "flex", flexDirection: "column", gap: 16 }}>
          {error && (
            <p style={{ color: "var(--color-danger)", fontSize: 13 }}>{error}</p>
          )}

          {/* Big folder picker button */}
          <div
            onClick={() => void handleChooseFolder()}
            style={{
              border: "2px dashed var(--color-border)",
              borderRadius: 8,
              padding: "32px 24px",
              textAlign: "center",
              cursor: "pointer",
              color: newPath ? "var(--color-text)" : "var(--color-text-muted)",
            }}
          >
            {newPath ? (
              <>
                <div style={{ fontSize: 32, marginBottom: 8 }}>📁</div>
                <div style={{ fontFamily: "var(--font-mono)", fontSize: 13 }}>{newPath}</div>
                <div style={{ fontSize: 12, color: "var(--color-text-muted)", marginTop: 4 }}>
                  Click to change
                </div>
              </>
            ) : (
              <>
                <div style={{ fontSize: 40, marginBottom: 8 }}>📂</div>
                <div style={{ fontWeight: 600, marginBottom: 4 }}>Choose a folder</div>
                <div style={{ fontSize: 12 }}>Select the folder containing your PDFs or notes</div>
              </>
            )}
          </div>

          {/* Or type path */}
          <input
            type="text"
            placeholder="Or type the full path  e.g. /Users/you/ai"
            value={newPath}
            onChange={(e) => {
              setNewPath(e.target.value);
              if (!newName && e.target.value) {
                setNewName(e.target.value.split("/").filter(Boolean).pop() ?? "");
              }
            }}
            style={{ fontFamily: "var(--font-mono)", fontSize: 12 }}
          />

          <input
            type="text"
            placeholder="Project name"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
          />

          <div style={{ display: "flex", gap: 8 }}>
            <button
              className="btn btn-primary"
              style={{ flex: 1 }}
              onClick={handleCreate}
              disabled={!newName.trim() || !newPath.trim()}
            >
              Import & Build →
            </button>
            <button className="btn" onClick={() => { setStep("list"); setError(null); setNewName(""); setNewPath(""); }}>
              Cancel
            </button>
          </div>
        </div>
      </div>
    );
  }

  // ── Project list ───────────────────────────────────────────────────────
  return (
      <div className="screen">
      <div className="screen-header">
        <h1>Projects</h1>
        <div style={{ display: "flex", gap: 8 }}>
          <button
            className="btn"
            onClick={async () => {
              const selectedPath = await pickDirectory("Select an existing Knowler project");
              if (selectedPath) {
                await handleOpenExisting(selectedPath);
              }
            }}
          >
            Open Existing
          </button>
          <button className="btn btn-primary" onClick={() => setStep("creating")}>
            + New Project
          </button>
        </div>
      </div>

      {error && (
        <p style={{ padding: "8px 16px", color: "var(--color-danger)", fontSize: 13 }}>{error}</p>
      )}
      {notice && (
        <p style={{ padding: "8px 16px", color: "var(--color-success)", fontSize: 13 }}>{notice}</p>
      )}

      <div className="screen-body">
        {projects.length === 0 ? (
          <div className="empty-state">
            <div style={{ fontSize: 48, marginBottom: 16 }}>📚</div>
            <h3>No projects yet</h3>
            <p>Point Knowler at a folder of PDFs or notes and it will do the rest.</p>
            <button
              className="btn btn-primary"
              style={{ marginTop: 16 }}
              onClick={() => setStep("creating")}
            >
              + New Project
            </button>
          </div>
        ) : (
          <div className="project-grid">
            {projects.map((p) => (
              <div
                key={p.project_id}
                className={`project-card${activeProject?.project_id === p.project_id ? " active" : ""}`}
                onClick={() => handleOpen(p)}
              >
                <div className="project-card-header">
                  <div className="project-card-name">{p.name}</div>
                  <button
                    className="btn btn-danger project-card-delete"
                    onClick={(e) => {
                      e.stopPropagation();
                      void handleDelete(p);
                    }}
                    title={`Delete ${p.name}`}
                  >
                    Delete
                  </button>
                </div>
                <div className="project-card-path">{p.root_path}</div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
