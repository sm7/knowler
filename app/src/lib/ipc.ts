/**
 * IPC client — fetch + WebSocket transport.
 *
 * All RPC calls go to POST /api/rpc
 * Engine events arrive over a WebSocket at ws://localhost:PORT/ws
 *
 * Browser-first client for the local HTTP/WebSocket engine.
 */

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

// In dev the Vite server (5173) is separate from the engine (7842), so point
// directly at the engine. In production both are served from the same origin.
const BASE_URL =
  import.meta.env.VITE_ENGINE_URL ??
  (import.meta.env.DEV ? "http://localhost:7842" : `http://${window.location.host}`);
const WS_URL = BASE_URL.replace(/^http/, "ws") + "/ws";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface SendResult {
  ok: boolean;
  result?: Record<string, unknown>;
  error?: {
    code: string;
    message: string;
    details?: Record<string, unknown>;
  };
}

export class IPCError extends Error {
  constructor(
    public readonly code: string,
    message: string,
    public readonly details?: Record<string, unknown>
  ) {
    super(message);
    this.name = "IPCError";
  }
}

// ---------------------------------------------------------------------------
// RPC over HTTP POST /api/rpc
// ---------------------------------------------------------------------------

export async function engineCall<T = Record<string, unknown>>(
  method: string,
  params: Record<string, unknown> = {}
): Promise<T> {
  const res = await fetch(`${BASE_URL}/api/rpc`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ method, params }),
  });

  const data: SendResult = await res.json();

  if (!data.ok) {
    throw new IPCError(
      data.error?.code ?? "UNKNOWN_ERROR",
      data.error?.message ?? "Unknown engine error",
      data.error?.details
    );
  }
  return data.result as T;
}

// ---------------------------------------------------------------------------
// WebSocket event bus
// ---------------------------------------------------------------------------

type EventHandler = (payload: unknown) => void;

class EngineEventBus {
  private _ws: WebSocket | null = null;
  private _handlers: Map<string, Set<EventHandler>> = new Map();
  // Track whether engine.ready was received so late subscribers still fire.
  private _engineReadyReceived = false;

  constructor() {
    this._connect();
  }

  private _connect() {
    const ws = new WebSocket(WS_URL);
    this._ws = ws;

    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data as string);
        if (msg.type === "event" && msg.event) {
          if (msg.event === "engine.ready") {
            this._engineReadyReceived = true;
          }
          this._dispatch(msg.event as string, msg.payload ?? {});
        }
      } catch {
        // ignore malformed messages
      }
    };

    ws.onclose = () => {
      setTimeout(() => this._connect(), 1500);
    };

    ws.onerror = () => {
      ws.close();
    };
  }

  private _dispatch(event: string, payload: unknown) {
    this._handlers.get(event)?.forEach((h) => h(payload));
  }

  on(event: string, handler: EventHandler): () => void {
    if (!this._handlers.has(event)) {
      this._handlers.set(event, new Set());
    }
    this._handlers.get(event)!.add(handler);
    // If engine.ready already fired before this listener was registered,
    // call the handler asynchronously so callers always get it.
    if (event === "engine.ready" && this._engineReadyReceived) {
      setTimeout(() => handler({}), 0);
    }
    return () => {
      this._handlers.get(event)?.delete(handler);
    };
  }
}

const _bus = new EngineEventBus();

function listen<T>(event: string, handler: (payload: T) => void): Promise<() => void> {
  return Promise.resolve(_bus.on(event, handler as EventHandler));
}

// ---------------------------------------------------------------------------
// Event listeners
// ---------------------------------------------------------------------------

export function onJobProgress(
  handler: (payload: JobProgressEvent) => void
): Promise<() => void> {
  return listen<JobProgressEvent>("job.progress", handler);
}

export function onJobCompleted(
  handler: (payload: JobDoneEvent) => void
): Promise<() => void> {
  return listen<JobDoneEvent>("job.completed", handler);
}

export function onJobFailed(
  handler: (payload: JobDoneEvent) => void
): Promise<() => void> {
  return listen<JobDoneEvent>("job.failed", handler);
}

export function onArtifactCreated(
  handler: (payload: ArtifactCreatedEvent) => void
): Promise<() => void> {
  return listen<ArtifactCreatedEvent>("artifact.created", handler);
}

export function onQueryPhaseChanged(
  handler: (payload: QueryPhaseEvent) => void
): Promise<() => void> {
  return listen<QueryPhaseEvent>("query.phase_changed", handler);
}

export function onEngineReady(handler: () => void): Promise<() => void> {
  return listen<unknown>("engine.ready", () => handler());
}

export function onEngineError(
  handler: (msg: string) => void
): Promise<() => void> {
  return listen<string>("engine.error", handler);
}

export function onMaintenanceFinding(
  handler: (payload: MaintenanceFindingEvent) => void
): Promise<() => void> {
  return listen<MaintenanceFindingEvent>("maintenance.finding_created", handler);
}

// ---------------------------------------------------------------------------
// Browser helpers
// ---------------------------------------------------------------------------

export interface AppSettings {
  active_project_root_path: string | null;
  llm_provider: "anthropic" | "openai";
  default_model_tier: "fast" | "balanced" | "best";
  privacy_mode: "local_first" | "restricted_cloud" | "local_only";
  obsidian_vault_path: string;
}

export interface ApiKeyStatus {
  configured: boolean;
  provider: "anthropic" | "openai";
  storage: "none" | "env" | "keychain" | "app_db" | "session";
}

export async function getAppSettings(): Promise<AppSettings> {
  return engineCall<AppSettings>("settings.getApp");
}

export async function setAppSettings(
  values: Partial<AppSettings>
): Promise<AppSettings> {
  return engineCall<AppSettings>("settings.setApp", { values });
}

export async function pickDirectory(
  prompt = "Select a project folder"
): Promise<string | null> {
  try {
    const result = await engineCall<{ path: string | null }>("os.pickDirectory", {
      prompt,
    });
    return result.path ?? null;
  } catch {
    const path = window.prompt("Enter the full path to your project folder:");
    return path?.trim() || null;
  }
}

/** Not available in web context — use <input type="file" multiple> in the screen. */
export async function pickFiles(): Promise<string[]> {
  return [];
}

export async function revealInFinder(path: string): Promise<void> {
  try {
    await engineCall("os.revealInFinder", { path });
  } catch {
    window.alert(`Path: ${path}`);
  }
}

export async function openInObsidian(
  path: string,
  _obsidianVault?: string
): Promise<void> {
  window.open(`obsidian://open?path=${encodeURIComponent(path)}`, "_blank");
}

export async function readFileText(_path: string): Promise<string> {
  return "";
}

export async function storeApiKey(
  service: string,
  account: string,
  key: string
): Promise<ApiKeyStatus> {
  void service;
  return engineCall<ApiKeyStatus>("settings.setApiKey", {
    provider: account,
    key,
  });
}

export async function getApiKeyStatus(
  service: string,
  account: string
): Promise<ApiKeyStatus> {
  void service;
  return engineCall<ApiKeyStatus>("settings.getApiKeyStatus", {
    provider: account,
  });
}

// ---------------------------------------------------------------------------
// Event payload types
// ---------------------------------------------------------------------------

export interface JobProgressEvent {
  job_id: string;
  project_id: string;
  level?: string;
  event_type?: string;
  message?: string;
  progress?: number;
  status?: string;
  payload?: Record<string, unknown>;
}

export interface JobDoneEvent {
  job_id: string;
  project_id: string;
  status: string;
  error?: string;
}

export interface ArtifactCreatedEvent {
  artifact_id: string;
  project_id: string;
  title: string;
  query_run_id?: string | null;
  question_page_id?: string | null;
}

export interface QueryPhaseEvent {
  query_run_id: string;
  phase: "planning" | "retrieving" | "synthesizing" | "writing";
}

export interface MaintenanceFindingEvent {
  project_id: string;
  total_findings: number;
  by_type: Record<string, number>;
}
