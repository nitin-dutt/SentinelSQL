import { useEffect, useState } from "react";
import { AuthError, fetchSchemaOverview } from "../api/sentinel";
import type { SchemaTable } from "../api/sentinel";

type Props = {
  onAuthError: () => void;
};

const SENSITIVE_BADGE = { fontSize: "11px", padding: "1px 8px", borderRadius: "10px", color: "#f87171", border: "1px solid #f87171" };

/** Collapsible list of the tables the user can ask about: description, sensitivity and columns. */
export default function SchemaPanel({ onAuthError }: Props) {
  const [tables, setTables] = useState<SchemaTable[] | null>(null);
  const [error, setError] = useState(false);
  const [open, setOpen] = useState<string | null>(null);

  useEffect(() => {
    fetchSchemaOverview()
      .then(setTables)
      .catch((e) => {
        if (e instanceof AuthError) onAuthError();
        else setError(true);
      });
  }, [onAuthError]);

  const sensitiveCount = tables?.filter((t) => t.sensitive).length ?? 0;

  return (
    <details className="glass-panel" style={{ padding: "20px 24px" }} open>
      <summary style={{ cursor: "pointer", fontWeight: 600, fontSize: "16px" }}>
        What is in this database?{tables ? ` ${tables.length} tables` : ""}
      </summary>
      {error && <p style={{ color: "var(--text-muted)" }}>The table list could not be loaded.</p>}
      {tables && (
        <>
          <p style={{ color: "var(--text-muted)", fontSize: "13px", margin: "12px 0" }}>
            A synthetic company: customers, orders, products, staff and finance. {sensitiveCount} tables are marked{" "}
            <span style={SENSITIVE_BADGE}>sensitive</span>: questions about them are held for a human reviewer and
            personal columns are masked. Click a table to see its columns.
          </p>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(230px, 1fr))", gap: "10px" }}>
            {tables.map((t) => {
              const isOpen = open === t.name;
              return (
                <div
                  key={t.name}
                  role="button"
                  aria-expanded={isOpen}
                  tabIndex={0}
                  onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setOpen(isOpen ? null : t.name); } }}
                  onClick={() => setOpen(isOpen ? null : t.name)}
                  style={{ padding: "10px 12px", borderRadius: "8px", border: "1px solid var(--border)", background: "rgba(255,255,255,0.03)", cursor: "pointer", gridColumn: isOpen ? "1 / -1" : undefined }}
                >
                  <div style={{ display: "flex", justifyContent: "space-between", gap: "8px", alignItems: "center" }}>
                    <code style={{ fontWeight: 700 }}>{t.name}</code>
                    <span style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                      {t.sensitive && <span style={SENSITIVE_BADGE}>sensitive</span>}
                      <span
                        aria-hidden="true"
                        title={isOpen ? "Hide columns" : "Show columns"}
                        style={{ color: "var(--text-muted)", fontSize: "12px", display: "inline-block", transition: "transform 0.2s", transform: isOpen ? "rotate(180deg)" : "none" }}
                      >
                        ▼
                      </span>
                    </span>
                  </div>
                  <div style={{ color: "var(--text-muted)", fontSize: "12px", marginTop: "4px" }}>{t.description}</div>
                  {isOpen && (
                    <div style={{ marginTop: "10px", fontSize: "13px" }}>
                      <div style={{ display: "flex", flexWrap: "wrap", gap: "6px" }}>
                        {t.columns.map((c) => (
                          <span key={c.name} style={{ padding: "2px 8px", borderRadius: "12px", border: "1px solid var(--border)" }}>
                            {c.primary_key ? "🔑 " : ""}
                            {c.name} <span style={{ color: "var(--text-muted)" }}>{c.type}</span>
                          </span>
                        ))}
                      </div>
                      {t.foreign_keys.length > 0 && (
                        <div style={{ color: "var(--text-muted)", marginTop: "8px" }}>
                          Links: {t.foreign_keys.join("; ")}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </>
      )}
    </details>
  );
}
