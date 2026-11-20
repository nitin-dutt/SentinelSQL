import { useEffect, useState } from "react";
import { AuthError, fetchSchemaOverview } from "../api/sentinel";
import type { SchemaTable } from "../api/sentinel";
import Icon from "./Icon";

type Props = {
  onAuthError: () => void;
  /** Set on a visitor's own database: the sample company's description does not apply there. */
  workspaceLabel?: string;
};

/** Collapsible list of the tables the user can ask about: description, sensitivity and columns. */
export default function SchemaPanel({ onAuthError, workspaceLabel }: Props) {
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
    <details className="card" open>
      <summary className="schema-head">
        What is in this database{tables ? <span className="meta"> · {tables.length} tables</span> : null}
      </summary>
      {error && <p className="muted" style={{ marginTop: 12 }}>The table list could not be loaded.</p>}
      {tables && (
        <>
          <p className="muted small" style={{ margin: "12px 0 16px" }}>
            {workspaceLabel ? `${workspaceLabel}.` : "A synthetic company: customers, orders, products, staff and finance."}{" "}
            {sensitiveCount} {sensitiveCount === 1 ? "table is" : "tables are"} marked{" "}
            <span className="tag tag-bad">sensitive</span>: questions about them are held for a human reviewer and
            personal columns are masked. Select a table to see its columns.
          </p>
          <div className="schema-list">
            {tables.map((t) => {
              const isOpen = open === t.name;
              return (
                <div
                  key={t.name}
                  className="schema-item"
                  role="button"
                  aria-expanded={isOpen}
                  tabIndex={0}
                  onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setOpen(isOpen ? null : t.name); } }}
                  onClick={() => setOpen(isOpen ? null : t.name)}
                >
                  <div className="schema-row">
                    <code className="schema-name">{t.name}</code>
                    <span className="schema-row-end" title={isOpen ? "Hide columns" : "Show columns"}>
                      {t.sensitive && <span className="tag tag-bad">sensitive</span>}
                      <Icon name="chevron-down" />
                    </span>
                  </div>
                  <div className="schema-desc">{t.description}</div>
                  {isOpen && (
                    <>
                      <div className="schema-cols">
                        {t.columns.map((c) => (
                          <span key={c.name} className="schema-col" title={c.primary_key ? "Primary key" : undefined}>
                            {c.primary_key && <Icon name="key" size={12} />}
                            {c.name} <span>{c.type}</span>
                          </span>
                        ))}
                      </div>
                      {t.foreign_keys.length > 0 && (
                        <div className="schema-desc" style={{ marginTop: 8 }}>
                          Links: {t.foreign_keys.join("; ")}
                        </div>
                      )}
                    </>
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
