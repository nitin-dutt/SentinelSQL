import { useState } from "react";

type Props = {
  sql: string;
  reason: string;
  runId?: string;
  onApprove: (saveAsTemplate: boolean) => void;
  onEdit: (editedSql: string, saveAsTemplate: boolean) => void;
  onReject: () => void;
  loading: boolean;
};

export default function SQLReview({
  sql,
  reason,
  runId,
  onApprove,
  onEdit,
  onReject,
  loading,
}: Props) {
  const [editedSql, setEditedSql] = useState(sql);
  const [mode, setMode] = useState<"view" | "edit">("view");
  const [saveAsTemplate, setSaveAsTemplate] = useState(false);

  const isEditMode = mode === "edit";

  return (
    <div
      style={{
        marginBottom: 32,
        padding: 32,
        border: "1px solid var(--warning)",
        borderRadius: 12,
        background: "rgba(245, 158, 11, 0.1)",
        boxShadow: "0 4px 6px rgba(0,0,0,0.1)",
      }}
    >
      <h2 style={{ marginTop: 0, color: "var(--warning)", fontSize: 24, display: "flex", alignItems: "center", gap: 8 }}>
        <svg width="24" height="24" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"></path></svg>
        Human Approval Required
      </h2>

      <p style={{ color: "var(--text-main)", marginBottom: 8, lineHeight: 1.6 }}>
        This query was flagged as potentially risky and requires explicit human
        approval before execution.
      </p>

      <p style={{ fontSize: 14, color: "var(--text-muted)", marginBottom: 8 }}>
        <strong style={{ color: "var(--text-main)" }}>Reason:</strong> {reason}
      </p>

      {runId && (
        <p style={{ fontSize: 13, color: "var(--text-muted)", marginBottom: 0 }}>
          <strong style={{ color: "var(--text-main)" }}>Run ID:</strong> <code style={{ background: "rgba(255,255,255,0.1)", padding: "2px 6px", borderRadius: 3 }}>{runId}</code>
        </p>
      )}

      <hr style={{ margin: 16, border: "none", borderTop: "1px solid rgba(245, 158, 11, 0.2)" }} />

      <label
        style={{
          display: "block",
          fontWeight: 600,
          marginBottom: 6,
          color: "var(--text-main)"
        }}
      >
        SQL to be executed
      </label>

      <textarea
        rows={8}
        value={isEditMode ? editedSql : sql}
        onChange={(e) => setEditedSql(e.target.value)}
        readOnly={!isEditMode}
        style={{
          width: "100%",
          fontFamily: "monospace",
          fontSize: "14px",
          background: isEditMode ? "rgba(0,0,0,0.5)" : "rgba(0,0,0,0.2)",
          border: isEditMode ? "1px solid var(--accent)" : "1px solid var(--border)",
          color: isEditMode ? "#fff" : "var(--accent-hover)",
          padding: "12px",
          borderRadius: "8px",
          outline: "none",
          resize: "vertical"
        }}
      />

      {/* ⭐ Approach 1: Explicit Business Template Opt-In Checkbox */}
      <div
        style={{
          marginTop: 14,
          padding: "10px 14px",
          background: "rgba(255, 255, 255, 0.04)",
          borderRadius: 8,
          border: "1px solid rgba(255, 255, 255, 0.08)",
          display: "flex",
          alignItems: "center",
          gap: 10,
        }}
      >
        <input
          type="checkbox"
          id={`save-template-${runId || "default"}`}
          checked={saveAsTemplate}
          onChange={(e) => setSaveAsTemplate(e.target.checked)}
          style={{ cursor: "pointer", width: 16, height: 16, accentColor: "var(--accent)" }}
        />
        <label
          htmlFor={`save-template-${runId || "default"}`}
          style={{
            cursor: "pointer",
            fontSize: 13,
            color: "var(--text-main)",
            userSelect: "none",
            display: "flex",
            alignItems: "center",
            gap: 6,
          }}
        >
          <span>⭐</span>
          <span>Save as verified business template for future reference (Few-Shot Memory)</span>
        </label>
      </div>

      <div
        style={{
          marginTop: 20,
          display: "flex",
          gap: 12,
          flexWrap: "wrap",
          alignItems: "center",
        }}
      >
        {!isEditMode && (
          <button className="primary" onClick={() => onApprove(saveAsTemplate)} disabled={loading} style={{ background: "var(--success)", borderColor: "var(--success)" }}>
            Approve & Execute
          </button>
        )}

        {!isEditMode && (
          <button onClick={() => setMode("edit")} disabled={loading}>
            Edit SQL
          </button>
        )}

        {isEditMode && (
          <button className="primary" onClick={() => onEdit(editedSql, saveAsTemplate)} disabled={loading || !editedSql.trim()}>
            Execute Edited SQL
          </button>
        )}

        {isEditMode && (
          <button onClick={() => { setEditedSql(sql); setMode("view"); }} disabled={loading}>
            Cancel
          </button>
        )}

        <button onClick={onReject} disabled={loading} style={{ marginLeft: "auto", background: "transparent", color: "var(--error)", borderColor: "var(--error)" }}>
          Reject
        </button>
      </div>

      {loading && (
        <p style={{ marginTop: 12, fontSize: 14, color: "var(--accent)" }}>
          ⏳ Awaiting execution…
        </p>
      )}
    </div>
  );
}
