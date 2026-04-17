import type { EngineStatus } from "../App";

interface Props {
  status: EngineStatus;
  error: string | null;
}

export function EngineStatusBanner({ status, error }: Props) {
  if (status === "ready") return null;

  return (
    <div
      style={{
        padding: "8px 16px",
        fontSize: 12,
        background: status === "error" ? "#fef2f2" : "#eff6ff",
        borderBottom: "1px solid",
        borderColor: status === "error" ? "#fca5a5" : "#bfdbfe",
        color: status === "error" ? "#b91c1c" : "#1d4ed8",
        display: "flex",
        alignItems: "center",
        gap: 8,
      }}
    >
      {status === "starting" ? (
        <>
          <span>⏳</span> Starting knowledge engine…
        </>
      ) : (
        <>
          <span>⚠</span> Engine error: {error || "Unknown error"}. Try restarting the app.
        </>
      )}
    </div>
  );
}
