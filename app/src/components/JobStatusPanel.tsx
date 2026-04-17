import { useEffect, useState } from "react";
import { onJobCompleted, onJobFailed, onJobProgress } from "../lib/ipc";
import { engineCall } from "../lib/ipc";
import type { Job, Project } from "../types";
import type { JobProgressEvent } from "../lib/ipc";

interface Props {
  project: Project | null;
}

interface LogEntry {
  id: string;
  job_id: string;
  level: string;
  message: string;
  ts: number;
}

export function JobStatusPanel({ project }: Props) {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [loading, setLoading] = useState(false);

  // Listen for job events
  useEffect(() => {
    const listeners: Array<() => void> = [];

    onJobProgress((e: JobProgressEvent) => {
      if (e.message) {
        setLogs((prev) => [
          {
            id: `${Date.now()}-${Math.random()}`,
            job_id: e.job_id,
            level: e.level ?? "info",
            message: e.message!,
            ts: Date.now(),
          },
          ...prev.slice(0, 99),
        ]);
      }
      // Update job progress
      if (e.status || e.progress !== undefined) {
        setJobs((prev) =>
          prev.map((j) =>
            j.id === e.job_id
              ? { ...j, progress: e.progress ?? j.progress, status: (e.status as Job["status"]) ?? j.status }
              : j
          )
        );
      }
    }).then((fn) => listeners.push(fn));

    onJobCompleted((e) => {
      setJobs((prev) =>
        prev.map((j) =>
          j.id === e.job_id ? { ...j, status: "succeeded", progress: 1.0 } : j
        )
      );
    }).then((fn) => listeners.push(fn));

    onJobFailed((e) => {
      setJobs((prev) =>
        prev.map((j) =>
          j.id === e.job_id ? { ...j, status: "failed" } : j
        )
      );
    }).then((fn) => listeners.push(fn));

    return () => listeners.forEach((fn) => fn());
  }, []);

  // Load jobs when project changes
  useEffect(() => {
    if (!project) return;
    setLoading(true);
    engineCall<{ jobs: Job[] }>("jobs.list", { project_id: project.project_id })
      .then((r) => setJobs(r.jobs ?? []))
      .catch(console.error)
      .finally(() => setLoading(false));
  }, [project]);

  if (!project) {
    return (
      <div style={{ padding: 16 }}>
        <p style={{ fontSize: 12, color: "var(--color-text-muted)" }}>
          Open a project to see job status.
        </p>
      </div>
    );
  }

  const activeJobs = jobs.filter((j) => j.status === "queued" || j.status === "running");
  const recentJobs = jobs.filter((j) => j.status !== "queued" && j.status !== "running").slice(0, 5);

  return (
    <div style={{ padding: "12px 14px", display: "flex", flexDirection: "column", gap: 16 }}>
      <section>
        <h3 style={{ marginBottom: 8, fontSize: 12, textTransform: "uppercase", letterSpacing: "0.05em", color: "var(--color-text-muted)" }}>
          Active Jobs
        </h3>
        {activeJobs.length === 0 ? (
          <p style={{ fontSize: 12, color: "var(--color-text-muted)" }}>No active jobs</p>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {activeJobs.map((job) => (
              <div key={job.id} className="card" style={{ padding: "10px 12px" }}>
                <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
                  <span style={{ fontSize: 12, fontWeight: 600 }}>{job.job_type}</span>
                  <span className={`badge badge-${job.status === "running" ? "blue" : "gray"}`} style={{ fontSize: 10 }}>
                    {job.status}
                  </span>
                </div>
                <div className="progress-bar">
                  <div
                    className="progress-bar-fill"
                    style={{ width: `${(job.progress ?? 0) * 100}%` }}
                  />
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      {logs.length > 0 && (
        <section>
          <h3 style={{ marginBottom: 8, fontSize: 12, textTransform: "uppercase", letterSpacing: "0.05em", color: "var(--color-text-muted)" }}>
            Recent Logs
          </h3>
          <div className="job-log" style={{ maxHeight: 200, overflowY: "auto" }}>
            {logs.map((entry) => (
              <div key={entry.id} className="job-log-entry">
                <span className={`job-log-level ${entry.level}`}>{entry.level.toUpperCase()}</span>
                <span style={{ color: "var(--color-text-secondary)" }}>{entry.message}</span>
              </div>
            ))}
          </div>
        </section>
      )}

      {recentJobs.length > 0 && (
        <section>
          <h3 style={{ marginBottom: 8, fontSize: 12, textTransform: "uppercase", letterSpacing: "0.05em", color: "var(--color-text-muted)" }}>
            Recent
          </h3>
          {recentJobs.map((job) => (
            <div
              key={job.id}
              style={{
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                padding: "5px 0",
                borderBottom: "1px solid var(--color-border-subtle)",
                fontSize: 12,
              }}
            >
              <span style={{ color: "var(--color-text-secondary)" }}>{job.job_type}</span>
              <span
                style={{
                  color:
                    job.status === "succeeded"
                      ? "var(--color-success)"
                      : job.status === "failed"
                      ? "var(--color-danger)"
                      : "var(--color-text-muted)",
                  fontWeight: 500,
                }}
              >
                {job.status === "succeeded" ? "✓" : job.status === "failed" ? "✗" : job.status}
              </span>
            </div>
          ))}
        </section>
      )}
    </div>
  );
}
