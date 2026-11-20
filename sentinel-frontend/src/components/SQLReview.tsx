import { useState } from "react";
import Icon from "./Icon";
import Verdict from "./Verdict";

type Props = {
  sql: string;
  reason: string;
  runId?: string;
  onApprove: (saveAsTemplate: boolean) => void;
  onEdit: (editedSql: string, saveAsTemplate: boolean) => void;
  onReject: () => void;
  loading: boolean;
  /** When set, approving and editing are disabled and this explains why (e.g. the reviewer submitted the request). */
  blockedReason?: string;
};

export default function SQLReview({
  sql,
  reason,
  runId,
  onApprove,
  onEdit,
  onReject,
  loading,
  blockedReason,
}: Props) {
  const [editedSql, setEditedSql] = useState(sql);
  const [mode, setMode] = useState<"view" | "edit">("view");
  const [saveAsTemplate, setSaveAsTemplate] = useState(false);

  const isEditMode = mode === "edit";
  const checkboxId = `save-template-${runId || "default"}`;

  return (
    <div>
      <div className="field">
        <span className="label">Why it was held</span>
        <p style={{ display: "flex", alignItems: "flex-start", gap: 8 }}>
          <span className="text-warn" style={{ paddingTop: 2 }}><Icon name="alert" /></span>
          <span>{reason}</span>
        </p>
        <p className="muted small" style={{ marginTop: 4 }}>
          This query was flagged as potentially risky and runs only after a reviewer approves it.
          {runId && <> Run ID <code>{runId}</code></>}
        </p>
      </div>

      <div className="field">
        <label className="label" htmlFor={`sql-${runId || "default"}`}>SQL to be executed</label>
        <textarea
          id={`sql-${runId || "default"}`}
          className="input input-mono"
          rows={8}
          value={isEditMode ? editedSql : sql}
          onChange={(e) => setEditedSql(e.target.value)}
          readOnly={!isEditMode}
          style={{
            background: "var(--bg-deep)",
            color: isEditMode ? "var(--text)" : "var(--accent-ink)",
            borderColor: isEditMode ? "var(--accent-ink)" : undefined,
          }}
        />
      </div>

      <label className="check field-gap" htmlFor={checkboxId}>
        <input
          type="checkbox"
          id={checkboxId}
          checked={saveAsTemplate}
          onChange={(e) => setSaveAsTemplate(e.target.checked)}
        />
        <span>Save as a verified template, used as a few-shot example for future questions</span>
      </label>

      <div className="actions" style={{ marginTop: 24 }}>
        {!isEditMode && (
          <button className="btn btn-primary" onClick={() => onApprove(saveAsTemplate)} disabled={loading || !!blockedReason}>
            Approve and run
          </button>
        )}

        {!isEditMode && (
          <button className="btn" onClick={() => setMode("edit")} disabled={loading || !!blockedReason}>
            Edit SQL
          </button>
        )}

        {isEditMode && (
          <button className="btn btn-primary" onClick={() => onEdit(editedSql, saveAsTemplate)} disabled={loading || !editedSql.trim()}>
            Run edited SQL
          </button>
        )}

        {isEditMode && (
          <button className="btn" onClick={() => { setEditedSql(sql); setMode("view"); }} disabled={loading}>
            Cancel
          </button>
        )}

        <button className="btn btn-danger push-right" onClick={onReject} disabled={loading}>
          Reject
        </button>
      </div>

      {blockedReason && (
        <p className="text-warn" style={{ marginTop: 12 }}>{blockedReason}</p>
      )}

      {loading && (
        <p style={{ marginTop: 12 }}>
          <Verdict tone="info">running</Verdict>
        </p>
      )}
    </div>
  );
}
