import { useEffect, useState } from "react";
import {
  getApiKeyStatus,
  getAppSettings,
  setAppSettings,
  storeApiKey,
  type ApiKeyStatus,
} from "../lib/ipc";

type PrivacyMode = "local_first" | "restricted_cloud" | "local_only";
type Provider = "anthropic" | "openai";
type ModelTier = "fast" | "balanced" | "best";

function apiKeyStorageCopy(status: ApiKeyStatus | null): string {
  if (!status?.configured) {
    return "Not configured yet.";
  }
  if (status.storage === "keychain") {
    return "Stored in macOS Keychain and restored automatically on restart.";
  }
  if (status.storage === "app_db") {
    return "Stored in Knowler's local app database as a development fallback.";
  }
  if (status.storage === "env") {
    return "Loaded from your shell environment.";
  }
  if (status.storage === "session") {
    return "Active for this session only.";
  }
  return "Stored locally.";
}

export function SettingsScreen() {
  const [provider, setProvider] = useState<Provider>("anthropic");
  const [apiKey, setApiKey] = useState("");
  const [apiKeyMasked, setApiKeyMasked] = useState(true);
  const [keyStatus, setKeyStatus] = useState<ApiKeyStatus | null>(null);
  const [settingsReady, setSettingsReady] = useState(false);
  const [privacyMode, setPrivacyMode] = useState<PrivacyMode>("local_first");
  const [obsidianPath, setObsidianPath] = useState("");
  const [defaultTier, setDefaultTier] = useState<ModelTier>("balanced");

  useEffect(() => {
    let cancelled = false;

    const loadSettings = async () => {
      try {
        const settings = await getAppSettings();
        if (cancelled) return;
        setProvider(settings.llm_provider);
        setPrivacyMode(settings.privacy_mode);
        setObsidianPath(settings.obsidian_vault_path);
        setDefaultTier(settings.default_model_tier);
      } finally {
        if (!cancelled) {
          setSettingsReady(true);
        }
      }
    };

    void loadSettings();

    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!settingsReady) return;

    let cancelled = false;
    setKeyStatus(null);

    getApiKeyStatus("Knowler", provider)
      .then((status) => {
        if (!cancelled) {
          setKeyStatus(status);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setKeyStatus({
            configured: false,
            provider,
            storage: "none",
          });
        }
      });

    return () => {
      cancelled = true;
    };
  }, [provider, settingsReady]);

  const persistSettings = async (values: {
    llm_provider?: Provider;
    default_model_tier?: ModelTier;
    privacy_mode?: PrivacyMode;
    obsidian_vault_path?: string;
  }) => {
    await setAppSettings(values);
  };

  const handleSaveKey = async () => {
    if (!apiKey.trim()) return;
    const status = await storeApiKey("Knowler", provider, apiKey.trim());
    setKeyStatus(status);
    setApiKey("");
    setApiKeyMasked(true);
  };

  return (
    <div className="screen">
      <div className="screen-header">
        <h1>Settings</h1>
      </div>
      <div className="screen-body" style={{ maxWidth: 600 }}>

        {/* Provider & API Key */}
        <section style={{ marginBottom: 32 }}>
          <h2 style={{ marginBottom: 16 }}>LLM Provider</h2>

          {/* Key status banner */}
          {keyStatus?.configured === true && (
            <div style={{
              marginBottom: 16,
              padding: "10px 14px",
              borderRadius: "var(--radius-sm)",
              background: "color-mix(in srgb, var(--color-success) 12%, transparent)",
              border: "1px solid color-mix(in srgb, var(--color-success) 30%, transparent)",
              color: "var(--color-success)",
              fontSize: 13,
              fontWeight: 500,
            }}>
              ✓ API key saved — AI features are active
            </div>
          )}
          {keyStatus?.configured === false && (
            <div style={{
              marginBottom: 16,
              padding: "10px 14px",
              borderRadius: "var(--radius-sm)",
              background: "color-mix(in srgb, var(--color-warning, #f59e0b) 12%, transparent)",
              border: "1px solid color-mix(in srgb, var(--color-warning, #f59e0b) 30%, transparent)",
              color: "var(--color-warning, #b45309)",
              fontSize: 13,
              fontWeight: 500,
            }}>
              ⚠ No API key saved — required for AI features (normalize, compile, query)
            </div>
          )}

          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            <div>
              <label style={{ display: "block", fontSize: 12, color: "var(--color-text-secondary)", marginBottom: 4 }}>
                Provider
              </label>
              <select
                value={provider}
                onChange={(e) => {
                  const nextProvider = e.target.value as Provider;
                  setProvider(nextProvider);
                  void persistSettings({ llm_provider: nextProvider });
                }}
                style={{ width: "auto" }}
              >
                <option value="anthropic">Anthropic (Claude)</option>
                <option value="openai">OpenAI (GPT)</option>
              </select>
            </div>

            <div>
              <label style={{ display: "block", fontSize: 12, color: "var(--color-text-secondary)", marginBottom: 4 }}>
                API Key
              </label>
              <div style={{ display: "flex", gap: 8 }}>
                <input
                  type={apiKeyMasked ? "password" : "text"}
                  placeholder={
                    keyStatus?.configured
                      ? "Key already stored — enter new key to replace"
                      : provider === "openai"
                        ? "sk-..."
                        : "sk-ant-..."
                  }
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                  style={{ flex: 1, fontFamily: "var(--font-mono)", fontSize: 12 }}
                />
                <button
                  className="btn btn-ghost"
                  style={{ fontSize: 12 }}
                  onClick={() => setApiKeyMasked(!apiKeyMasked)}
                >
                  {apiKeyMasked ? "Show" : "Hide"}
                </button>
                <button
                  className="btn btn-primary"
                  style={{ fontSize: 12 }}
                  onClick={handleSaveKey}
                  disabled={!apiKey.trim()}
                >
                  Save Key
                </button>
              </div>
              <p style={{ marginTop: 4, fontSize: 11, color: "var(--color-text-muted)" }}>
                {apiKeyStorageCopy(keyStatus)}
              </p>
            </div>

            <div>
              <label style={{ display: "block", fontSize: 12, color: "var(--color-text-secondary)", marginBottom: 4 }}>
                Default model tier
              </label>
              <select
                value={defaultTier}
                onChange={(e) => {
                  const nextTier = e.target.value as ModelTier;
                  setDefaultTier(nextTier);
                  void persistSettings({ default_model_tier: nextTier });
                }}
                style={{ width: "auto" }}
              >
                <option value="fast">Fast (cheap, good for maintenance)</option>
                <option value="balanced">Balanced (default)</option>
                <option value="best">Best (high quality)</option>
              </select>
            </div>
          </div>
        </section>

        {/* Privacy */}
        <section style={{ marginBottom: 32 }}>
          <h2 style={{ marginBottom: 16 }}>Privacy</h2>
          <div>
            <label style={{ display: "block", fontSize: 12, color: "var(--color-text-secondary)", marginBottom: 4 }}>
              Privacy mode
            </label>
            <select
              value={privacyMode}
              onChange={(e) => {
                const nextMode = e.target.value as PrivacyMode;
                setPrivacyMode(nextMode);
                void persistSettings({ privacy_mode: nextMode });
              }}
              style={{ width: "auto" }}
            >
              <option value="local_first">Local-first — cloud LLM calls allowed for AI tasks only</option>
              <option value="restricted_cloud">Restricted cloud — pre-process locally before sending</option>
              <option value="local_only">Local-only — no remote calls</option>
            </select>

            <div style={{ marginTop: 12, background: "var(--color-badge-bg)", borderRadius: "var(--radius-sm)", padding: 12, fontSize: 12 }}>
              {privacyMode === "local_first" && (
                <p>Your files stay on disk. Only selected text is sent to the LLM provider for AI tasks. No background telemetry.</p>
              )}
              {privacyMode === "restricted_cloud" && (
                <p>Raw source text is summarized locally before being sent to the provider. Images are not sent unless required.</p>
              )}
              {privacyMode === "local_only" && (
                <p>No remote model calls. Only local parsing, search, and indexing. AI features require a local model setup.</p>
              )}
            </div>
          </div>
        </section>

        {/* Obsidian integration */}
        <section style={{ marginBottom: 32 }}>
          <h2 style={{ marginBottom: 16 }}>Obsidian Integration</h2>
          <div>
            <label style={{ display: "block", fontSize: 12, color: "var(--color-text-secondary)", marginBottom: 4 }}>
              Obsidian vault name (optional)
            </label>
            <input
              type="text"
              placeholder="My Vault"
              value={obsidianPath}
              onChange={(e) => setObsidianPath(e.target.value)}
              onBlur={() => void persistSettings({ obsidian_vault_path: obsidianPath })}
            />
            <p style={{ marginTop: 4, fontSize: 11, color: "var(--color-text-muted)" }}>
              If set, "Open in Obsidian" links will use this vault name.
              Leave blank to open files with the system default app.
            </p>
          </div>
        </section>

        <p style={{ fontSize: 11, color: "var(--color-text-muted)" }}>
          Knowler v0.1 — local-first knowledge compiler
        </p>
      </div>
    </div>
  );
}
