import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import { engineCall, onArtifactCreated, onJobCompleted, onJobFailed, onQueryPhaseChanged } from "../lib/ipc";
import type { Project, TaskType, ModelTier, OutputFormat } from "../types";

interface Props {
  project: Project | null;
}

type QueryPhase = "idle" | "planning" | "retrieving" | "synthesizing" | "writing" | "done" | "error";

const PHASE_ORDER: QueryPhase[] = ["planning", "retrieving", "synthesizing", "writing"];

function stripFrontmatter(md: string): string {
  return md.replace(/^---[\s\S]*?---\n?/, "");
}

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
  const isSubmittingRef = useRef(false);
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
    if (!project || !prompt.trim() || isSubmittingRef.current) return;
    isSubmittingRef.current = true;
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
    } finally {
      isSubmittingRef.current = false;
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
                {PHASE_ORDER.map((p) => (
                  <span key={p} style={{ display: "flex", alignItems: "center", gap: 4 }}>
                    <span
                      className={`phase-dot ${phase === p ? "active" : (PHASE_ORDER.indexOf(phase as QueryPhase) > PHASE_ORDER.indexOf(p) ? "done" : "")}`}
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
            >
              <ReactMarkdown>{stripFrontmatter(artifactContent)}</ReactMarkdown>
            </div>
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
