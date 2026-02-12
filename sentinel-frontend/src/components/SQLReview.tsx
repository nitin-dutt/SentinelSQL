import { useState } from "react";

type Props = {
  sql: string;
  reason: string;
  runId?: string;
  onApprove: () => void;
  onEdit: (editedSql: string) => void;
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

  const isEditMode = mode === "edit";

  return (
    <div
      style={{
        marginBottom: 32,
        padding: 32,
        border: "2px solid #f6ad55",
        borderRadius: 12,
        background: "#fffaf0",
        boxShadow: "0 4px 6px rgba(0,0,0,0.07)",
      }}
    >
      <h2 style={{ marginTop: 0, color: "#c05621", fontSize: 24, display: "flex", alignItems: "center", gap: 8 }}>⚠️ Human Approval Required</h2>

      <p style={{ color: "#744210", marginBottom: 8, lineHeight: 1.6 }}>
        This query was flagged as potentially risky and requires explicit human
        approval before execution.
      </p>

      <p style={{ fontSize: 14, color: "#5a3a1a", marginBottom: 8 }}>
        <strong>Reason:</strong> {reason}
      </p>

      {runId && (
        <p style={{ fontSize: 13, color: "#744210", marginBottom: 0 }}>
          <strong>Run ID:</strong> <code style={{ background: "#fff5e6", padding: "2px 6px", borderRadius: 3 }}>{runId}</code>
        </p>
      )}

      <hr style={{ margin: 16, border: "none", borderTop: "1px solid #f6d5b8" }} />

      <label
        style={{
          display: "block",
          fontWeight: 600,
          marginBottom: 6,
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
          fontSize: "13px",
          background: isEditMode ? "#fff" : "#f4f4f4",
          border: "1px solid #ccc",
          padding: "10px",
          borderRadius: "4px",
        }}
      />

      <div
        style={{
          marginTop: 20,
          display: "flex",
          gap: 10,
          flexWrap: "wrap",
          alignItems: "center",
        }}
      >
        {!isEditMode && (
          <button onClick={onApprove} disabled={loading} style={{ background: "#48bb78", color: "white", padding: "10px 20px", fontSize: 14, fontWeight: 600, border: "none", borderRadius: 6, cursor: loading ? "not-allowed" : "pointer", opacity: loading ? 0.6 : 1, transition: "background-color 0.2s" }} onMouseOver={(e) => !loading && (e.currentTarget.style.background = "#38a169")} onMouseOut={(e) => !loading && (e.currentTarget.style.background = "#48bb78")}>
            ✅ Approve & Execute
          </button>
        )}

        {!isEditMode && (
          <button onClick={() => setMode("edit")} disabled={loading} style={{ background: "#4299e1", color: "white", padding: "10px 20px", fontSize: 14, fontWeight: 600, border: "none", borderRadius: 6, cursor: loading ? "not-allowed" : "pointer", opacity: loading ? 0.6 : 1, transition: "background-color 0.2s" }} onMouseOver={(e) => !loading && (e.currentTarget.style.background = "#3182ce")} onMouseOut={(e) => !loading && (e.currentTarget.style.background = "#4299e1")}>
            ✏️ Edit SQL
          </button>
        )}

        {isEditMode && (
          <button onClick={() => onEdit(editedSql)} disabled={loading || !editedSql.trim()} style={{ background: !editedSql.trim() ? "#cbd5e0" : "#48bb78", color: "white", padding: "10px 20px", fontSize: 14, fontWeight: 600, border: "none", borderRadius: 6, cursor: loading || !editedSql.trim() ? "not-allowed" : "pointer", opacity: loading || !editedSql.trim() ? 0.6 : 1, transition: "background-color 0.2s" }}>
            ▶ Execute Edited SQL
          </button>
        )}

        {isEditMode && (
          <button onClick={() => { setEditedSql(sql); setMode("view"); }} disabled={loading} style={{ background: "#718096", color: "white", padding: "10px 20px", fontSize: 14, fontWeight: 600, border: "none", borderRadius: 6, cursor: loading ? "not-allowed" : "pointer", opacity: loading ? 0.6 : 1 }}>
            ↩ Cancel
          </button>
        )}

        <button onClick={onReject} disabled={loading} style={{ marginLeft: "auto", background: "#f56565", color: "white", padding: "10px 20px", fontSize: 14, fontWeight: 600, border: "none", borderRadius: 6, cursor: loading ? "not-allowed" : "pointer", opacity: loading ? 0.6 : 1, transition: "background-color 0.2s" }} onMouseOver={(e) => !loading && (e.currentTarget.style.background = "#e53e3e")} onMouseOut={(e) => !loading && (e.currentTarget.style.background = "#f56565")}>
          ❌ Reject
        </button>
      </div>

      {loading && (
        <p style={{ marginTop: 12, fontSize: 14, color: "#744210" }}>
          ⏳ Awaiting execution…
        </p>
      )}
    </div>
  );
}
