import { NavLink } from "react-router-dom";
import type { Project } from "../types";
import "./Sidebar.css";

interface Props {
  activeProject: Project | null;
  projectSelectionReady: boolean;
}

const NAV_ITEMS = [
  { path: "/projects", label: "Projects", icon: "⊞", requiresProject: false },
  { path: "/inbox", label: "Inbox", icon: "↓", requiresProject: true },
  { path: "/build", label: "Build", icon: "⚙", requiresProject: true },
  { path: "/ask", label: "Ask", icon: "?", requiresProject: true },
  { path: "/visualization", label: "Map", icon: "◈", requiresProject: true },
  { path: "/artifacts", label: "Wiki", icon: "◎", requiresProject: true },
  { path: "/health", label: "Health", icon: "♥", requiresProject: true },
  { path: "/settings", label: "Settings", icon: "⋯", requiresProject: false },
];

export function Sidebar({ activeProject, projectSelectionReady }: Props) {
  const projectActionsEnabled = projectSelectionReady && Boolean(activeProject);

  return (
    <nav className="sidebar">
      <div className="sidebar-header">
        <span className="sidebar-logo">Knowler</span>
        {activeProject && (
          <span className="sidebar-project-name truncate" title={activeProject.name}>
            {activeProject.name}
          </span>
        )}
        {!activeProject && projectSelectionReady && (
          <span className="sidebar-project-hint">Select a project to unlock the workspace.</span>
        )}
      </div>

      <ul className="sidebar-nav">
        {NAV_ITEMS.map(({ path, label, icon, requiresProject }) => (
          <li key={path}>
            {requiresProject && !projectActionsEnabled ? (
              <button
                className="sidebar-nav-item disabled"
                disabled
                title="Select a project first"
                aria-label={`${label} — select a project first`}
              >
                <span className="sidebar-nav-icon" aria-hidden="true">
                  {icon}
                </span>
                {label}
              </button>
            ) : (
              <NavLink
                to={path}
                className={({ isActive }) =>
                  `sidebar-nav-item${isActive ? " active" : ""}`
                }
              >
                <span className="sidebar-nav-icon" aria-hidden="true">
                  {icon}
                </span>
                {label}
              </NavLink>
            )}
          </li>
        ))}
      </ul>

      <div className="sidebar-footer">
        <span className="sidebar-version">v0.1</span>
      </div>
    </nav>
  );
}
