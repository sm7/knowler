import { useEffect, useState, Component, type ReactElement, type ReactNode, type ErrorInfo } from "react";
import { Routes, Route, Navigate } from "react-router-dom";

class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  constructor(props: { children: ReactNode }) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("Unhandled component error", error, info);
  }

  render() {
    if (this.state.error) {
      return (
        <div style={{ padding: 32, display: "flex", flexDirection: "column", gap: 12 }}>
          <h2 style={{ color: "var(--color-danger)" }}>Something went wrong</h2>
          <p style={{ fontSize: 13, color: "var(--color-text-secondary)" }}>
            {this.state.error.message}
          </p>
          <button className="btn" onClick={() => this.setState({ error: null })}>
            Try again
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
import { Sidebar } from "./components/Sidebar";
import { JobStatusPanel } from "./components/JobStatusPanel";
import { ProjectsScreen } from "./screens/ProjectsScreen";
import { InboxScreen } from "./screens/InboxScreen";
import { BuildScreen } from "./screens/BuildScreen";
import { AskScreen } from "./screens/AskScreen";
import { ArtifactsScreen } from "./screens/ArtifactsScreen";
import { VisualizationScreen } from "./screens/VisualizationScreen";
import { HealthScreen } from "./screens/HealthScreen";
import { SettingsScreen } from "./screens/SettingsScreen";
import { EngineStatusBanner } from "./components/EngineStatusBanner";
import { engineCall, getAppSettings, onEngineReady, onEngineError, setAppSettings } from "./lib/ipc";
import type { Project } from "./types";
import "./styles/app.css";

export type EngineStatus = "starting" | "ready" | "error";
const ACTIVE_PROJECT_STORAGE_KEY = "knowler:activeProjectRootPath";

function ProjectRoute({
  activeProject,
  projectSelectionReady,
  children,
}: {
  activeProject: Project | null;
  projectSelectionReady: boolean;
  children: ReactElement;
}) {
  if (!projectSelectionReady) {
    return (
      <div className="screen">
        <div className="empty-state">
          <h3>Loading project…</h3>
          <p>Restoring your last selected project.</p>
        </div>
      </div>
    );
  }

  if (!activeProject) {
    return <Navigate to="/projects" replace />;
  }

  return children;
}

export default function App() {
  const [engineStatus, setEngineStatus] = useState<EngineStatus>("starting");
  const [engineError, setEngineError] = useState<string | null>(null);
  const [activeProject, setActiveProject] = useState<Project | null>(null);
  const [projectSelectionReady, setProjectSelectionReady] = useState(false);

  useEffect(() => {
    let unlistenReady: (() => void) | undefined;
    let unlistenError: (() => void) | undefined;

    onEngineReady(() => setEngineStatus("ready")).then((fn) => {
      unlistenReady = fn;
    });
    onEngineError((msg) => {
      setEngineStatus("error");
      setEngineError(msg);
    }).then((fn) => {
      unlistenError = fn;
    });

    return () => {
      unlistenReady?.();
      unlistenError?.();
    };
  }, []);

  useEffect(() => {
    if (engineStatus !== "ready") return;

    let cancelled = false;

    const restoreProject = async () => {
      try {
        const settings = await getAppSettings();
        const savedRoot =
          settings.active_project_root_path ??
          localStorage.getItem(ACTIVE_PROJECT_STORAGE_KEY);

        if (!savedRoot) return;

        const project = await engineCall<Project>("project.open", {
          root_path: savedRoot,
        });
        if (!cancelled) {
          setActiveProject(project);
        }
      } catch {
        localStorage.removeItem(ACTIVE_PROJECT_STORAGE_KEY);
        void setAppSettings({ active_project_root_path: null }).catch(() => {});
      } finally {
        if (!cancelled) {
          setProjectSelectionReady(true);
        }
      }
    };

    void restoreProject();

    return () => {
      cancelled = true;
    };
  }, [engineStatus]);

  useEffect(() => {
    if (engineStatus !== "ready" || !projectSelectionReady) return;

    const rootPath = activeProject?.root_path ?? null;
    if (rootPath) {
      localStorage.setItem(ACTIVE_PROJECT_STORAGE_KEY, rootPath);
    } else {
      localStorage.removeItem(ACTIVE_PROJECT_STORAGE_KEY);
    }

    void setAppSettings({ active_project_root_path: rootPath }).catch(() => {});
  }, [activeProject, engineStatus, projectSelectionReady]);

  return (
    <div className="app-layout">
      <Sidebar
        activeProject={activeProject}
        projectSelectionReady={projectSelectionReady}
      />
      <main className="app-main">
        <EngineStatusBanner status={engineStatus} error={engineError} />
        <div className="app-content">
          <ErrorBoundary>
          <Routes>
            <Route path="/" element={<Navigate to="/projects" replace />} />
            <Route
              path="/projects"
              element={
                <ProjectsScreen
                  onProjectOpen={(p) => setActiveProject(p)}
                  onProjectDelete={(projectId) =>
                    setActiveProject((current) =>
                      current?.project_id === projectId ? null : current
                    )
                  }
                  activeProject={activeProject}
                />
              }
            />
            <Route
              path="/inbox"
              element={
                <ProjectRoute
                  activeProject={activeProject}
                  projectSelectionReady={projectSelectionReady}
                >
                  <InboxScreen project={activeProject} />
                </ProjectRoute>
              }
            />
            <Route
              path="/build"
              element={
                <ProjectRoute
                  activeProject={activeProject}
                  projectSelectionReady={projectSelectionReady}
                >
                  <BuildScreen project={activeProject} />
                </ProjectRoute>
              }
            />
            <Route
              path="/ask"
              element={
                <ProjectRoute
                  activeProject={activeProject}
                  projectSelectionReady={projectSelectionReady}
                >
                  <AskScreen project={activeProject} />
                </ProjectRoute>
              }
            />
            <Route
              path="/visualization"
              element={
                <ProjectRoute
                  activeProject={activeProject}
                  projectSelectionReady={projectSelectionReady}
                >
                  <VisualizationScreen project={activeProject} />
                </ProjectRoute>
              }
            />
            <Route
              path="/artifacts"
              element={
                <ProjectRoute
                  activeProject={activeProject}
                  projectSelectionReady={projectSelectionReady}
                >
                  <ArtifactsScreen project={activeProject} />
                </ProjectRoute>
              }
            />
            <Route
              path="/health"
              element={
                <ProjectRoute
                  activeProject={activeProject}
                  projectSelectionReady={projectSelectionReady}
                >
                  <HealthScreen project={activeProject} />
                </ProjectRoute>
              }
            />
            <Route path="/settings" element={<SettingsScreen />} />
          </Routes>
          </ErrorBoundary>
        </div>
      </main>
      <aside className="app-context-panel">
        <JobStatusPanel project={activeProject} />
      </aside>
    </div>
  );
}
