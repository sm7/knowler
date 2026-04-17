import { useEffect, useState } from "react";
import { engineCall } from "../lib/ipc";
import type { Project, MaintenanceFinding, FindingType } from "../types";

interface Props {
  project: Project | null;
}

const FINDING_SECTIONS: { type: FindingType; label: string; icon: string }[] = [
  { type: "orphan_page", label: "Orphan Pages", icon: "◌" },
  { type: "duplicate_entity", label: "Duplicate Concepts", icon: "⟺" },
  { type: "weak_claim", label: "Weak Claims", icon: "?" },
  { type: "stale_page", label: "Stale Pages", icon: "⏳" },
  { type: "missing_comparison", label: "Missing Comparisons", icon: "≠" },
];

export function HealthScreen({ project }: Props) {
  const [findings, setFindings] = useState<MaintenanceFinding[]>([]);
  const [loading, setLoading] = useState(false);
  const [running, setRunning] = useState(false);

  const load = async () => {
    if (!project) return;
    setLoading(true);
    try {
      const r = await engineCall<{ findings: MaintenanceFinding[] }>(
        "maintenance.listFindings",
        { project_id: project.project_id }
      );
      setFindings(r.findings ?? []);
    } catch (e) {
      console.error(e);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, [project]);

  const runMaintenance = async () => {
    if (!project) return;
    setRunning(true);
    try {
      await engineCall("maintenance.run", { project_id: project.project_id });
      setTimeout(() => { load(); setRunning(false); }, 3000);
    } catch (e) {
      console.error(e);
      setRunning(false);
    }
  };

  const ignoreFinding = async (finding: MaintenanceFinding) => {
    if (!project) return;
    await engineCall("maintenance.ignoreFinding", {
      project_id: project.project_id,
      finding_id: finding.id,
    });
    setFindings((prev) => prev.filter((f) => f.id !== finding.id));
  };

  if (!project) {
    return (
      <div className="screen">
        <div className="screen-header"><h1>Health</h1></div>
        <div className="empty-state"><h3>No project open</h3></div>
      </div>
    );
  }

  return (
    <div className="screen">
      <div className="screen-header">
        <h1>Health</h1>
        <div style={{ display: "flex", gap: 8 }}>
          <button className="btn" onClick={load} disabled={loading || running}>
            Refresh
          </button>
          <button
            className="btn btn-primary"
            onClick={runMaintenance}
            disabled={running}
          >
            {running ? "Running…" : "♥ Run Maintenance"}
          </button>
        </div>
      </div>

      <div className="screen-body">
        {findings.length === 0 && !loading ? (
          <div className="empty-state">
            <span style={{ fontSize: 36 }}>♥</span>
            <h3>No findings</h3>
            <p>
              {running
                ? "Maintenance is running…"
                : "No open findings right now. Run a maintenance pass to check for issues."}
            </p>
          </div>
        ) : (
          FINDING_SECTIONS.map(({ type, label, icon }) => {
            const sectionFindings = findings.filter((f) => f.finding_type === type);
            if (sectionFindings.length === 0) return null;
            return (
              <section key={type} style={{ marginBottom: 24 }}>
                <h2 style={{ marginBottom: 12, display: "flex", alignItems: "center", gap: 8 }}>
                  <span>{icon}</span>
                  {label}
                  <span className="badge badge-gray" style={{ fontSize: 11 }}>
                    {sectionFindings.length}
                  </span>
                </h2>
                <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                  {sectionFindings.map((finding) => (
                    <FindingCard
                      key={finding.id}
                      finding={finding}
                      onIgnore={() => ignoreFinding(finding)}
                    />
                  ))}
                </div>
              </section>
            );
          })
        )}
      </div>
    </div>
  );
}

function FindingCard({
  finding,
  onIgnore,
}: {
  finding: MaintenanceFinding;
  onIgnore: () => void;
}) {
  let suggestion: Record<string, unknown> = {};
  try {
    suggestion = JSON.parse(finding.suggestion_json);
  } catch {}

  return (
    <div className="finding-card">
      <span className={`finding-severity-dot ${finding.severity}`} />
      <div className="finding-body">
        <div className="finding-title">{finding.title}</div>
        <div className="finding-desc">{finding.description}</div>
        {suggestion.action_type != null && (
          <div style={{ marginTop: 4, fontSize: 11, color: "var(--color-accent)" }}>
            Suggested: {String(suggestion.action_type).replace(/_/g, " ")}
            {suggestion.details != null ? ` — ${String(suggestion.details)}` : ""}
          </div>
        )}
        <div className="finding-actions">
          <button className="btn btn-ghost" style={{ fontSize: 11, padding: "3px 8px" }} onClick={onIgnore}>
            Ignore
          </button>
        </div>
      </div>
    </div>
  );
}
