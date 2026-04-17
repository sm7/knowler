import { useEffect, useRef, useState } from "react";
import { engineCall, onJobCompleted, onJobFailed, onJobProgress } from "../lib/ipc";
import type { Project, Job } from "../types";

interface Props {
  project: Project | null;
}

interface BuildLog {
  id: string;
  level: string;
  message: string;
}

export function BuildScreen({ project }: Props) {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [logs, setLogs] = useState<BuildLog[]>([]);
  const [running, setRunning] = useState(false);
  const [currentJobId, setCurrentJobId] = useState<string | null>(null);
  const currentJobIdRef = useRef<string | null>(null);

  useEffect(() => {
    currentJobIdRef.current = currentJobId;
  }, [currentJobId]);

  const refreshJobs = async () => {
    if (!project) return;
    const r = await engineCall<{ jobs: Job[] }>("jobs.list", { project_id: project.project_id });
    setJobs(r.jobs ?? []);
  };

  useEffect(() => {
    if (!project) return;
    refreshJobs().catch(console.error);
  }, [project]);

  useEffect(() => {
    const listeners: Array<() => void> = [];

    onJobProgress((e) => {
      if (e.message) {
        setLogs((prev) => [
          { id: `${Date.now()}-${Math.random()}`, level: e.level ?? "info", message: e.message! },
          ...prev.slice(0, 199),
        ]);
      }
      if (e.status || e.progress !== undefined) {
        setJobs((prev) =>
          prev.map((j) =>
            j.id === e.job_id
              ? {
                  ...j,
                  progress: e.progress ?? j.progress,
                  status: (e.status as Job["status"]) ?? j.status,
                }
              : j
          )
        );
      }
      if (e.job_id === currentJobIdRef.current && (e.status === "running" || e.progress !== undefined)) {
        setRunning(true);
      }
    }).then((fn) => listeners.push(fn));

    onJobCompleted((e) => {
      if (e.job_id === currentJobIdRef.current) setRunning(false);
      setJobs((prev) =>
        prev.map((j) => j.id === e.job_id ? { ...j, status: "succeeded", progress: 1.0 } : j)
      );
    }).then((fn) => listeners.push(fn));

    onJobFailed((e) => {
      if (e.job_id === currentJobIdRef.current) setRunning(false);
      setJobs((prev) =>
        prev.map((j) => j.id === e.job_id ? { ...j, status: "failed" } : j)
      );
    }).then((fn) => listeners.push(fn));

    return () => listeners.forEach((fn) => fn());
  }, []);

  useEffect(() => {
    if (!project || !currentJobId || !running) return;
    let cancelled = false;
    const tick = async () => {
      if (cancelled) return;
      try {
        const r = await engineCall<{ jobs: Job[] }>("jobs.list", { project_id: project.project_id });
        if (cancelled) return;
        setJobs(r.jobs ?? []);
        const current = (r.jobs ?? []).find((job) => job.id === currentJobId);
        if (!current || !["queued", "running"].includes(current.status)) {
          setRunning(false);
          return;
        }
      } catch (error) {
        console.error(error);
      }
      window.setTimeout(() => {
        void tick();
      }, 1200);
    };
    void tick();
    return () => {
      cancelled = true;
    };
  }, [project, currentJobId, running]);

  const startCompile = async (scope: "incremental" | "full") => {
    if (!project) return;
    if (scope === "full") {
      if (!confirm("Full rebuild will recompile all pages. This may take a while. Continue?")) return;
    }
    setRunning(true);
    setLogs([]);
    try {
      const r = await engineCall<{ job_id: string }>("compile.runProject", {
        project_id: project.project_id,
        scope,
        reason: "user_manual",
      });
      setCurrentJobId(r.job_id);
      await refreshJobs();
    } catch (e) {
      console.error(e);
      setRunning(false);
    }
  };

  const runMaintenance = async () => {
    if (!project) return;
    setRunning(true);
    setLogs([]);
    try {
      const r = await engineCall<{ job_id: string }>("maintenance.run", {
        project_id: project.project_id,
      });
      setCurrentJobId(r.job_id);
      await refreshJobs();
    } catch (e) {
      console.error(e);
      setRunning(false);
    }
  };

  if (!project) {
    return (
      <div className="screen">
        <div className="screen-header"><h1>Build</h1></div>
        <div className="empty-state">
          <h3>No project open</h3>
          <p>Open a project to run the knowledge compiler.</p>
        </div>
      </div>
    );
  }

  const activeJob = jobs.find((j) => j.id === currentJobId);

  return (
    <div className="screen">
      <div className="screen-header">
        <h1>Build</h1>
      </div>

      <div className="screen-body">
        <div style={{ display: "flex", gap: 10, marginBottom: 24, flexWrap: "wrap" }}>
          <button
            className="btn btn-primary"
            onClick={() => startCompile("incremental")}
            disabled={running}
          >
            ⚙ Compile Project
          </button>
          <button
            className="btn"
            onClick={() => startCompile("full")}
            disabled={running}
          >
            Rebuild All
          </button>
          <button
            className="btn"
            onClick={runMaintenance}
            disabled={running}
          >
            ♥ Run Maintenance
          </button>
        </div>

        {activeJob && (
          <div className="card" style={{ marginBottom: 20 }}>
            <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
              <span style={{ fontWeight: 600, fontSize: 13 }}>
                {activeJob.job_type}
              </span>
              <span style={{ fontSize: 12, color: "var(--color-text-secondary)" }}>
                {activeJob.status}
              </span>
            </div>
            <div className="progress-bar">
              <div
                className="progress-bar-fill"
                style={{ width: `${Math.round((activeJob.progress ?? 0) * 100)}%` }}
              />
            </div>
            <p style={{ fontSize: 11, marginTop: 4, color: "var(--color-text-muted)" }}>
              {Math.round((activeJob.progress ?? 0) * 100)}% complete
            </p>
          </div>
        )}

        {logs.length > 0 && (
          <div>
            <h3 style={{ marginBottom: 8 }}>Build Log</h3>
            <div
              className="job-log"
              style={{
                background: "var(--color-border-subtle)",
                padding: 12,
                borderRadius: "var(--radius-sm)",
                maxHeight: 400,
                overflowY: "auto",
                direction: "ltr",
              }}
            >
              {logs.map((entry) => (
                <div key={entry.id} className="job-log-entry">
                  <span className={`job-log-level ${entry.level}`}>
                    {entry.level.toUpperCase().slice(0, 4)}
                  </span>
                  <span style={{ color: "var(--color-text)" }}>{entry.message}</span>
                </div>
              ))}
            </div>
          </div>
        )}

        {logs.length === 0 && !running && (
          <div className="empty-state">
            <span style={{ fontSize: 36 }}>⚙</span>
            <h3>No recent builds</h3>
            <p>
              Import and approve sources in the Inbox, then compile to generate
              wiki pages and concept summaries.
            </p>
          </div>
        )}

        {jobs.filter((j) => j.status !== "queued" && j.status !== "running").length > 0 && (
          <div style={{ marginTop: 24 }}>
            <h3 style={{ marginBottom: 12 }}>Recent Jobs</h3>
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              {jobs.filter((j) => j.status !== "queued" && j.status !== "running").slice(0, 8).map((job) => (
                <div
                  key={job.id}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 12,
                    padding: "8px 12px",
                    background: "var(--color-surface)",
                    border: "1px solid var(--color-border)",
                    borderRadius: "var(--radius-sm)",
                    fontSize: 13,
                  }}
                >
                  <span
                    style={{
                      color: job.status === "succeeded" ? "var(--color-success)" : "var(--color-danger)",
                      fontSize: 16,
                    }}
                  >
                    {job.status === "succeeded" ? "✓" : "✗"}
                  </span>
                  <span style={{ flex: 1 }}>{job.job_type}</span>
                  <span style={{ color: "var(--color-text-muted)", fontSize: 12 }}>
                    {job.created_at ? new Date(job.created_at).toLocaleString() : ""}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
