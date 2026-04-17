import { useEffect, useRef, useState } from "react";
import { engineCall, onArtifactCreated, onJobCompleted, onJobFailed, onQueryPhaseChanged } from "../lib/ipc";
import type { Project, TaskType, ModelTier, OutputFormat } from "../types";

interface Props {
  project: Project | null;
}

type QueryPhase = "idle" | "planning" | "retrieving" | "synthesizing" | "writing" | "done" | "error";

export function AskScreen({ project }: Props) {
  const [prompt, setPrompt] = useState("");
  const [taskType, setTaskType] = useState<TaskType>("auto");
  const [modelTier, setModelTier] = useState<ModelTier>("balanced");
  const [outputFormat, setOutputFormat] = useState<OutputFormat>("markdown_report");
  const [fileBack, setFileBack] = useState(true);
  const [phase, setPhase] = useState<QueryPhase>("idle");
  const [error, setError] = useState<string | null>(null);
  const [lastArtifactId, setLastArtifactId] = useState<string | null>(null);
  const [artifactContent, setArtifactContent] = useState<string | null>(null);
  const [currentJobId, setCurrentJobId] = useState<string | null>(null);
  const [currentQueryRunId, setCurrentQueryRunId] = useState<string | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    textareaRef.current?.focus();
  }, []);

  useEffect(() => {
    const listeners: Array<() => void> = [];

    const loadArtifact = async (artifactId: string) => {
      if (!project) return;
      const art = await engineCall<{ content: string; file_path: string }>(
        "artifacts.get",
        { project_id: project.project_id, artifact_id: artifactId }
      );
      setArtifactContent(art.content ?? null);
      setLastArtifactId(artifactId);
      setPhase("done");
      setError(null);
    };

    onQueryPhaseChanged((e) => {
      if (e.query_run_id !== currentQueryRunId) return;
      setPhase(e.phase as QueryPhase);
    }).then((fn) => listeners.push(fn));

    onArtifactCreated(async (e) => {
      if (e.query_run_id !== currentQueryRunId) return;
      try {
        await loadArtifact(e.artifact_id);
      } catch (err: unknown) {
        setPhase("error");
        setError(err instanceof Error ? err.message : String(err));
      }
    }).then((fn) => listeners.push(fn));

    onJobCompleted(async (e) => {
      if (e.job_id !== currentJobId || !project || !currentQueryRunId) return;
      try {
        const run = await engineCall<{ result_artifact_id: string | null; status: string }>(
          "query.get",
          { project_id: project.project_id, query_run_id: currentQueryRunId }
        );
        if (run.result_artifact_id) {
          await loadArtifact(run.result_artifact_id);
        } else {
          setPhase("error");
          setError("The query finished but no answer artifact was produced.");
        }
      } catch (err: unknown) {
        setPhase("error");
        setError(err instanceof Error ? err.message : String(err));
      }
    }).then((fn) => listeners.push(fn));

    onJobFailed((e) => {
      if (e.job_id !== currentJobId) return;
      setPhase("error");
      setError(e.error ?? "The query failed before producing an answer.");
    }).then((fn) => listeners.push(fn));

    return () => listeners.forEach((fn) => fn());
  }, [currentJobId, currentQueryRunId, project]);

  const handleGenerate = async () => {
    if (!project || !prompt.trim()) return;
    setPhase("planning");
    setError(null);
    setArtifactContent(null);
    setLastArtifactId(null);
    setCurrentJobId(null);
    setCurrentQueryRunId(null);

    try {
      const result = await engineCall<{ job_id: string; query_run_id: string }>("query.run", {
        project_id: project.project_id,
        prompt: prompt.trim(),
        task_type: taskType,
        output_format: outputFormat,
        model_tier: modelTier,
        file_back: fileBack,
      });
      setCurrentJobId(result.job_id);
      setCurrentQueryRunId(result.query_run_id);
    } catch (e: unknown) {
      setPhase("error");
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  if (!project) {
    return (
      <div className="screen">
        <div className="screen-header"><h1>Ask</h1></div>
        <div className="empty-state">
          <h3>No project open</h3>
          <p>Open a project to ask questions and generate artifacts.</p>
        </div>
      </div>
    );
  }

  const isRunning = phase !== "idle" && phase !== "done" && phase !== "error";

  return (
    <div className="screen">
      <div className="screen-header">
        <h1>Ask</h1>
      </div>

      <div className="screen-body">
        {/* Prompt area */}
        <div className="card" style={{ marginBottom: 16 }}>
          <textarea
            ref={textareaRef}
            placeholder="Ask a question, request a comparison, generate a study guide, create a report…"
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            rows={5}
            style={{ marginBottom: 12, resize: "vertical" }}
            disabled={isRunning}
          />

          <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center", marginBottom: 12 }}>
            <label style={{ fontSize: 12, color: "var(--color-text-secondary)" }}>Task:</label>
            <select value={taskType} onChange={(e) => setTaskType(e.target.value as TaskType)} style={{ width: "auto" }}>
              <option value="auto">Auto-detect</option>
              <option value="answer">Direct Answer</option>
              <option value="comparison">Comparison</option>
              <option value="study_guide">Study Guide</option>
              <option value="report">Report</option>
              <option value="reading_plan">Reading Plan</option>
              <option value="slides">Slides (Marp)</option>
              <option value="checklist">Checklist</option>
              <option value="open_questions">Open Questions</option>
            </select>

            <label style={{ fontSize: 12, color: "var(--color-text-secondary)" }}>Model:</label>
            <select value={modelTier} onChange={(e) => setModelTier(e.target.value as ModelTier)} style={{ width: "auto" }}>
              <option value="fast">Fast (cheap)</option>
              <option value="balanced">Balanced</option>
              <option value="best">Best quality</option>
            </select>

            <label style={{ fontSize: 12, color: "var(--color-text-secondary)" }}>Format:</label>
            <select value={outputFormat} onChange={(e) => setOutputFormat(e.target.value as OutputFormat)} style={{ width: "auto" }}>
              <option value="markdown_report">Markdown Report</option>
              <option value="markdown_note">Markdown Note</option>
              <option value="checklist">Checklist</option>
            </select>

            <label style={{ fontSize: 12, display: "flex", alignItems: "center", gap: 4 }}>
              <input
                type="checkbox"
                checked={fileBack}
                onChange={(e) => setFileBack(e.target.checked)}
              />
              File into wiki/questions
            </label>
          </div>

          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <button
              className="btn btn-primary"
              onClick={handleGenerate}
              disabled={isRunning || !prompt.trim()}
            >
              {isRunning ? "Generating…" : "Generate"}
            </button>

            {isRunning && (
              <div className="phase-indicator">
                {(["planning", "retrieving", "synthesizing", "writing"] as const).map((p) => (
                  <span key={p} style={{ display: "flex", alignItems: "center", gap: 4 }}>
                    <span
                      className={`phase-dot ${phase === p ? "active" : (["planning", "retrieving", "synthesizing", "writing"].indexOf(phase) > ["planning", "retrieving", "synthesizing", "writing"].indexOf(p) ? "done" : "")}`}
                    />
                    <span style={{ fontSize: 11 }}>{p}</span>
                  </span>
                ))}
              </div>
            )}
          </div>
        </div>

        {error && (
          <div style={{ background: "#fef2f2", border: "1px solid #fca5a5", borderRadius: "var(--radius-sm)", padding: "10px 14px", marginBottom: 16, color: "var(--color-danger)", fontSize: 13 }}>
            {error}
          </div>
        )}

        {/* Artifact preview */}
        {artifactContent && (
          <div>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
              <h2>Generated Artifact</h2>
              <button className="btn" style={{ fontSize: 12 }} onClick={() => setArtifactContent(null)}>
                Clear
              </button>
            </div>
            <div
              className="artifact-preview card"
              style={{ background: "var(--color-surface)" }}
              dangerouslySetInnerHTML={{ __html: markdownToHtmlSimple(artifactContent) }}
            />
          </div>
        )}

        {phase === "idle" && !artifactContent && (
          <div className="empty-state">
            <span style={{ fontSize: 36 }}>?</span>
            <h3>No artifact yet</h3>
            <p>
              Type a question or task above and click Generate. Artifacts are saved
              to <code>outputs/</code>, and you can optionally file answers into
              <code>wiki/questions</code>.
            </p>
          </div>
        )}
      </div>
    </div>
  );
}

/** Very simple markdown→HTML for preview. For production use react-markdown. */
function markdownToHtmlSimple(md: string): string {
  return md
    .replace(/^---[\s\S]*?---\n?/, "") // strip frontmatter
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/^### (.+)$/gm, "<h3>$1</h3>")
    .replace(/^## (.+)$/gm, "<h2>$1</h2>")
    .replace(/^# (.+)$/gm, "<h1>$1</h1>")
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/\*(.+?)\*/g, "<em>$1</em>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/^- (.+)$/gm, "<li>$1</li>")
    .replace(/(<li>.*<\/li>\n?)+/g, (m) => `<ul>${m}</ul>`)
    .replace(/^---$/gm, "<hr>")
    .replace(/\n\n/g, "</p><p>")
    .replace(/^(?!<[h|u|o|l|p|h])/gm, "")
    .replace(/^([^<].+)$/gm, "<p>$1</p>");
}
