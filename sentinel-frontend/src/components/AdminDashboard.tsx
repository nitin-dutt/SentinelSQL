import { useState, useEffect } from "react";
import {
  getAdminQueue,
  submitDecision,
  getGoldenQueries,
  deleteGoldenQuery,
  getCacheStats,
  clearCache,
  getDictionaryTerms,
  addDictionaryTerm,
  deleteDictionaryTerm,
  type GoldenQuery,
  type CacheStats,
  type DictionaryTerm
} from "../api/sentinel";
import SQLReview from "./SQLReview";
import LoadingIndicator from "./LoadingIndicator";

interface AdminDashboardProps {
  onDecisionComplete?: (runId: string, result: any) => void;
}

export default function AdminDashboard({ onDecisionComplete }: AdminDashboardProps) {
  const [queue, setQueue] = useState<any[]>([]);
  const [goldenQueries, setGoldenQueries] = useState<GoldenQuery[]>([]);
  const [cacheStats, setCacheStats] = useState<CacheStats | null>(null);
  const [dictionaryTerms, setDictionaryTerms] = useState<DictionaryTerm[]>([]);
  const [showAddTerm, setShowAddTerm] = useState(false);
  const [addingTerm, setAddingTerm] = useState(false);
  const [deletingTermId, setDeletingTermId] = useState<number | null>(null);
  const [newTerm, setNewTerm] = useState({
    term: "",
    definition: "",
    formula_or_rule: "",
    related_tables: "",
    category: "metric"
  });
  const [clearingCache, setClearingCache] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [processingId, setProcessingId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<number | null>(null);

  useEffect(() => {
    fetchData();
  }, []);

  async function fetchData() {
    setLoading(true);
    setError(null);
    try {
      const [queueData, templatesData, statsData, dictData] = await Promise.all([
        getAdminQueue(),
        getGoldenQueries().catch(() => []),
        getCacheStats().catch(() => null),
        getDictionaryTerms().catch(() => [])
      ]);
      setQueue(queueData);
      setGoldenQueries(templatesData);
      setCacheStats(statsData);
      setDictionaryTerms(dictData);
    } catch (e: any) {
      setError(e.message || "Failed to load dashboard data");
    } finally {
      setLoading(false);
    }
  }

  async function handleDecision(
    runId: string,
    decision: "approve" | "edit" | "reject",
    editedSql?: string,
    saveAsTemplate?: boolean
  ) {
    setProcessingId(runId);
    try {
      const res = await submitDecision({
        run_id: runId,
        decision,
        edited_sql: editedSql,
        save_as_template: saveAsTemplate
      });

      // Remove from queue
      setQueue(q => q.filter(item => item.run_id !== runId));

      // Refresh golden templates if saved
      if (saveAsTemplate && (decision === "approve" || decision === "edit")) {
        try {
          const updatedTemplates = await getGoldenQueries();
          setGoldenQueries(updatedTemplates);
        } catch {
          // ignore background reload error
        }
      }

      if (onDecisionComplete) {
        onDecisionComplete(runId, res);
      }
    } catch (e: any) {
      alert("Failed to process decision: " + e.message);
    } finally {
      setProcessingId(null);
    }
  }

  async function handleDeleteTemplate(id: number) {
    if (!confirm(`Are you sure you want to delete Golden Query Template #${id}?`)) {
      return;
    }
    setDeletingId(id);
    try {
      await deleteGoldenQuery(id);
      setGoldenQueries(prev => prev.filter(t => t.id !== id));
    } catch (e: any) {
      alert("Failed to delete golden query: " + e.message);
    } finally {
      setDeletingId(null);
    }
  }

  async function handleClearCache() {
    if (!confirm("Are you sure you want to invalidate all cached query entries?")) {
      return;
    }
    setClearingCache(true);
    try {
      await clearCache();
      const stats = await getCacheStats();
      setCacheStats(stats);
    } catch (e: any) {
      alert("Failed to clear cache: " + e.message);
    } finally {
      setClearingCache(false);
    }
  }

  async function handleAddTerm(e: React.FormEvent) {
    e.preventDefault();
    if (!newTerm.term.trim() || !newTerm.formula_or_rule.trim()) {
      alert("Please provide both Term name and SQL Formula / Condition.");
      return;
    }
    setAddingTerm(true);
    try {
      await addDictionaryTerm(newTerm);
      const updated = await getDictionaryTerms();
      setDictionaryTerms(updated);
      setNewTerm({ term: "", definition: "", formula_or_rule: "", related_tables: "", category: "metric" });
      setShowAddTerm(false);
    } catch (e: any) {
      alert("Failed to save dictionary term: " + e.message);
    } finally {
      setAddingTerm(false);
    }
  }

  async function handleDeleteTerm(id: number, termName: string) {
    if (!confirm(`Are you sure you want to delete '${termName}' from the Data Dictionary?`)) {
      return;
    }
    setDeletingTermId(id);
    try {
      await deleteDictionaryTerm(id);
      setDictionaryTerms(prev => prev.filter(t => t.id !== id));
    } catch (e: any) {
      alert("Failed to delete term: " + e.message);
    } finally {
      setDeletingTermId(null);
    }
  }

  if (loading) return <LoadingIndicator />;
  if (error) return <div style={{ color: "var(--error)", padding: 24 }}>{error}</div>;

  return (
    <div className="admin-dashboard animate-fade-in" style={{ padding: 24, maxWidth: 1000, margin: "0 auto", display: "flex", flexDirection: "column", gap: 32 }}>
      <div>
        <h2 style={{ marginTop: 0, marginBottom: 8, fontSize: 28 }}>Admin Queue</h2>
        <p style={{ color: "var(--text-muted)", margin: 0 }}>Review and approve queries flagged by the LLM Validator.</p>
      </div>

      {queue.length === 0 ? (
        <div className="glass-panel" style={{ padding: 48, textAlign: "center", color: "var(--text-muted)" }}>
          <h3 style={{ margin: 0 }}>Queue is Empty</h3>
          <p>No queries require human intervention right now.</p>
        </div>
      ) : (
        queue.map(item => (
          <div key={item.run_id} className="glass-panel" style={{ padding: 32 }}>
            <div style={{ marginBottom: 24 }}>
              <h3 style={{ margin: "0 0 16px 0", color: "var(--accent)" }}>Query Intent</h3>
              <div style={{ background: "rgba(255,255,255,0.05)", padding: 16, borderRadius: 8, border: "1px solid var(--border)" }}>
                <strong>User Question:</strong> {item.state.user_query}
              </div>
            </div>

            <SQLReview
              runId={item.run_id}
              sql={item.state.sql || ""}
              reason={item.state.validation?.reason || "Flagged for review"}
              onApprove={(saveAsTemplate) => handleDecision(item.run_id, "approve", undefined, saveAsTemplate)}
              onEdit={(edited, saveAsTemplate) => handleDecision(item.run_id, "edit", edited, saveAsTemplate)}
              onReject={() => handleDecision(item.run_id, "reject")}
              loading={processingId === item.run_id}
            />
          </div>
        ))
      )}

      {/* ⭐ Golden Query Templates Section */}
      <div style={{ marginTop: 16 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
          <div>
            <h3 style={{ margin: 0, fontSize: 22, display: "flex", alignItems: "center", gap: 8 }}>
              <span>⭐</span> Verified Business Templates
              <span className="badge" style={{ fontSize: 13, background: "rgba(99, 102, 241, 0.2)", color: "var(--accent)" }}>
                {goldenQueries.length} Saved
              </span>
            </h3>
            <p style={{ color: "var(--text-muted)", margin: "4px 0 0 0", fontSize: 14 }}>
              Human-approved golden queries injected as few-shot in-context examples to guarantee generation accuracy.
            </p>
          </div>
        </div>

        {goldenQueries.length === 0 ? (
          <div className="glass-panel" style={{ padding: 32, textAlign: "center", color: "var(--text-muted)" }}>
            <p style={{ margin: 0 }}>
              No golden query templates saved yet. When approving a query in the Admin Queue, check
              <strong> "Save as verified business template"</strong> to add it to SentinelSQL's memory.
            </p>
          </div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
            {goldenQueries.map(template => (
              <div
                key={template.id}
                className="glass-panel"
                style={{
                  padding: 20,
                  display: "flex",
                  flexDirection: "column",
                  gap: 12,
                  border: "1px solid rgba(255, 255, 255, 0.08)"
                }}
              >
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 16 }}>
                  <div>
                    <span style={{ fontSize: 12, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: 0.5 }}>
                      Template #{template.id} &bull; Added by <strong style={{ color: "var(--text-main)" }}>{template.created_by}</strong>
                    </span>
                    <h4 style={{ margin: "4px 0 0 0", fontSize: 16, color: "var(--text-main)" }}>
                      "{template.natural_language_query}"
                    </h4>
                  </div>
                  <button
                    onClick={() => handleDeleteTemplate(template.id)}
                    disabled={deletingId === template.id}
                    style={{
                      background: "rgba(239, 68, 68, 0.1)",
                      border: "1px solid rgba(239, 68, 68, 0.3)",
                      color: "var(--error)",
                      fontSize: 12,
                      padding: "6px 12px",
                      cursor: "pointer"
                    }}
                  >
                    {deletingId === template.id ? "Deleting…" : "🗑️ Delete"}
                  </button>
                </div>

                <div
                  style={{
                    background: "rgba(0, 0, 0, 0.3)",
                    padding: 12,
                    borderRadius: 6,
                    fontFamily: "monospace",
                    fontSize: 13,
                    color: "var(--accent-hover)",
                    overflowX: "auto"
                  }}
                >
                  {template.verified_sql}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* ⚡ Semantic Cache Telemetry & Invalidation Section */}
      <div style={{ marginTop: 8 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
          <div>
            <h3 style={{ margin: 0, fontSize: 22, display: "flex", alignItems: "center", gap: 8 }}>
              <span>⚡</span> Enterprise Semantic Cache
              <span className="badge" style={{ fontSize: 13, background: "rgba(56, 189, 248, 0.2)", color: "#38bdf8" }}>
                {cacheStats?.total_entries ?? 0} Cached
              </span>
            </h3>
            <p style={{ color: "var(--text-muted)", margin: "4px 0 0 0", fontSize: 14 }}>
              Two-tier L1 exact hash + L2 vector cosine similarity cache bypassing LLM & DB latency (under 3ms).
            </p>
          </div>

          <button
            onClick={handleClearCache}
            disabled={clearingCache || (cacheStats?.total_entries ?? 0) === 0}
            style={{
              padding: "8px 16px",
              cursor: clearingCache || (cacheStats?.total_entries ?? 0) === 0 ? "not-allowed" : "pointer",
              background: "rgba(239, 68, 68, 0.15)",
              color: "#f87171",
              border: "1px solid rgba(239, 68, 68, 0.4)",
              opacity: clearingCache || (cacheStats?.total_entries ?? 0) === 0 ? 0.5 : 1
            }}
          >
            {clearingCache ? "⏳ Clearing..." : "🗑️ Invalidate Cache"}
          </button>
        </div>

        {cacheStats && (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 16 }}>
            <div className="glass-panel" style={{ padding: 16, textAlign: "center" }}>
              <div style={{ fontSize: 12, color: "var(--text-muted)", textTransform: "uppercase" }}>Hit Ratio</div>
              <div style={{ fontSize: 24, fontWeight: 700, color: "#34d399", marginTop: 4 }}>
                {cacheStats.hit_ratio_percent}%
              </div>
            </div>

            <div className="glass-panel" style={{ padding: 16, textAlign: "center" }}>
              <div style={{ fontSize: 12, color: "var(--text-muted)", textTransform: "uppercase" }}>Total Hits</div>
              <div style={{ fontSize: 24, fontWeight: 700, color: "#38bdf8", marginTop: 4 }}>
                {cacheStats.hits}
              </div>
            </div>

            <div className="glass-panel" style={{ padding: 16, textAlign: "center" }}>
              <div style={{ fontSize: 12, color: "var(--text-muted)", textTransform: "uppercase" }}>L1 Exact Hits</div>
              <div style={{ fontSize: 24, fontWeight: 700, color: "var(--accent)", marginTop: 4 }}>
                {cacheStats.exact_hits}
              </div>
            </div>

            <div className="glass-panel" style={{ padding: 16, textAlign: "center" }}>
              <div style={{ fontSize: 12, color: "var(--text-muted)", textTransform: "uppercase" }}>L2 Vector Hits</div>
              <div style={{ fontSize: 24, fontWeight: 700, color: "#c084fc", marginTop: 4 }}>
                {cacheStats.semantic_hits}
              </div>
            </div>

            <div className="glass-panel" style={{ padding: 16, textAlign: "center" }}>
              <div style={{ fontSize: 12, color: "var(--text-muted)", textTransform: "uppercase" }}>Cache Misses</div>
              <div style={{ fontSize: 24, fontWeight: 700, color: "var(--text-muted)", marginTop: 4 }}>
                {cacheStats.misses}
              </div>
            </div>
          </div>
        )}
      </div>

      {/* 📖 Business Glossary & Data Dictionary Section */}
      <div style={{ marginTop: 8 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16, flexWrap: "wrap", gap: 12 }}>
          <div>
            <h3 style={{ margin: 0, fontSize: 22, display: "flex", alignItems: "center", gap: 8 }}>
              <span>📖</span> Business Glossary & Data Dictionary
              <span className="badge" style={{ fontSize: 13, background: "rgba(168, 85, 247, 0.2)", color: "#c084fc" }}>
                {dictionaryTerms.length} Rules Active
              </span>
            </h3>
            <p style={{ color: "var(--text-muted)", margin: "4px 0 0 0", fontSize: 14 }}>
              Maps enterprise business terminology (e.g., "Net Revenue", "VIP Tier") into verified SQL formulas and dbt definitions.
            </p>
          </div>

          <button
            className="primary"
            onClick={() => setShowAddTerm(!showAddTerm)}
            style={{ padding: "8px 16px", cursor: "pointer" }}
          >
            {showAddTerm ? "✕ Cancel" : "+ Add Business Metric"}
          </button>
        </div>

        {/* Add Term Form */}
        {showAddTerm && (
          <form
            onSubmit={handleAddTerm}
            className="glass-panel"
            style={{
              padding: 24,
              marginBottom: 20,
              display: "flex",
              flexDirection: "column",
              gap: 16,
              border: "1px solid var(--accent)"
            }}
          >
            <h4 style={{ margin: 0, fontSize: 18, color: "var(--accent)" }}>Define New Business Metric / Rule</h4>

            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 16 }}>
              <div>
                <label style={{ display: "block", fontSize: 12, fontWeight: 600, color: "var(--text-muted)", marginBottom: 4 }}>
                  TERM NAME *
                </label>
                <input
                  type="text"
                  placeholder="e.g. Net Revenue, Churned Customer"
                  value={newTerm.term}
                  onChange={(e) => setNewTerm({ ...newTerm, term: e.target.value })}
                  required
                  style={{ width: "100%", padding: 10, borderRadius: 6, background: "rgba(0,0,0,0.3)", border: "1px solid var(--border)", color: "#fff" }}
                />
              </div>

              <div>
                <label style={{ display: "block", fontSize: 12, fontWeight: 600, color: "var(--text-muted)", marginBottom: 4 }}>
                  CATEGORY
                </label>
                <select
                  value={newTerm.category}
                  onChange={(e) => setNewTerm({ ...newTerm, category: e.target.value })}
                  style={{ width: "100%", padding: 10, borderRadius: 6, background: "#1e293b", border: "1px solid var(--border)", color: "#fff" }}
                >
                  <option value="metric">Metric (Calculation Formula)</option>
                  <option value="filter">Filter (WHERE Condition)</option>
                  <option value="segment">Segment (Cohort Rule)</option>
                  <option value="model">Model / Entity Definition</option>
                </select>
              </div>

              <div>
                <label style={{ display: "block", fontSize: 12, fontWeight: 600, color: "var(--text-muted)", marginBottom: 4 }}>
                  APPLICABLE TABLES
                </label>
                <input
                  type="text"
                  placeholder="e.g. orders, customers"
                  value={newTerm.related_tables}
                  onChange={(e) => setNewTerm({ ...newTerm, related_tables: e.target.value })}
                  style={{ width: "100%", padding: 10, borderRadius: 6, background: "rgba(0,0,0,0.3)", border: "1px solid var(--border)", color: "#fff" }}
                />
              </div>
            </div>

            <div>
              <label style={{ display: "block", fontSize: 12, fontWeight: 600, color: "var(--text-muted)", marginBottom: 4 }}>
                SQL FORMULA / CONDITION *
              </label>
              <input
                type="text"
                placeholder="e.g. SUM(orders.total_amount) - SUM(orders.discount_amount)"
                value={newTerm.formula_or_rule}
                onChange={(e) => setNewTerm({ ...newTerm, formula_or_rule: e.target.value })}
                required
                style={{ width: "100%", padding: 10, fontFamily: "monospace", borderRadius: 6, background: "rgba(0,0,0,0.3)", border: "1px solid var(--border)", color: "var(--accent-hover)" }}
              />
            </div>

            <div>
              <label style={{ display: "block", fontSize: 12, fontWeight: 600, color: "var(--text-muted)", marginBottom: 4 }}>
                BUSINESS DEFINITION
              </label>
              <textarea
                rows={2}
                placeholder="Brief business description explaining how this metric should be calculated..."
                value={newTerm.definition}
                onChange={(e) => setNewTerm({ ...newTerm, definition: e.target.value })}
                style={{ width: "100%", padding: 10, borderRadius: 6, background: "rgba(0,0,0,0.3)", border: "1px solid var(--border)", color: "#fff", resize: "vertical" }}
              />
            </div>

            <div style={{ display: "flex", gap: 12, justifyContent: "flex-end" }}>
              <button type="button" onClick={() => setShowAddTerm(false)} disabled={addingTerm}>
                Cancel
              </button>
              <button type="submit" className="primary" disabled={addingTerm}>
                {addingTerm ? "Saving..." : "Save Metric Rule"}
              </button>
            </div>
          </form>
        )}

        {/* Terms List */}
        {dictionaryTerms.length === 0 ? (
          <div className="glass-panel" style={{ padding: 32, textAlign: "center", color: "var(--text-muted)" }}>
            <p style={{ margin: 0 }}>No business glossary terms defined yet.</p>
          </div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
            {dictionaryTerms.map(term => (
              <div
                key={term.id}
                className="glass-panel"
                style={{
                  padding: 20,
                  display: "flex",
                  flexDirection: "column",
                  gap: 12,
                  border: "1px solid rgba(255, 255, 255, 0.08)"
                }}
              >
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 16 }}>
                  <div>
                    <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                      <h4 style={{ margin: 0, fontSize: 18, color: "var(--text-main)" }}>
                        {term.term}
                      </h4>
                      <span
                        className="badge"
                        style={{
                          fontSize: 11,
                          textTransform: "uppercase",
                          background:
                            term.category === "metric"
                              ? "rgba(16, 185, 129, 0.2)"
                              : term.category === "filter"
                              ? "rgba(59, 130, 246, 0.2)"
                              : "rgba(168, 85, 247, 0.2)",
                          color:
                            term.category === "metric"
                              ? "#34d399"
                              : term.category === "filter"
                              ? "#60a5fa"
                              : "#c084fc",
                        }}
                      >
                        {term.category || "metric"}
                      </span>
                      {term.related_tables && (
                        <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
                          &bull; Tables: <code style={{ color: "var(--accent)" }}>{term.related_tables}</code>
                        </span>
                      )}
                    </div>

                    {term.definition && (
                      <p style={{ margin: "6px 0 0 0", color: "var(--text-muted)", fontSize: 14 }}>
                        {term.definition}
                      </p>
                    )}
                  </div>

                  <button
                    onClick={() => handleDeleteTerm(term.id, term.term)}
                    disabled={deletingTermId === term.id}
                    style={{
                      background: "rgba(239, 68, 68, 0.1)",
                      border: "1px solid rgba(239, 68, 68, 0.3)",
                      color: "var(--error)",
                      fontSize: 12,
                      padding: "6px 12px",
                      cursor: "pointer",
                      flexShrink: 0
                    }}
                  >
                    {deletingTermId === term.id ? "Deleting…" : "🗑️ Delete"}
                  </button>
                </div>

                <div
                  style={{
                    background: "rgba(0, 0, 0, 0.3)",
                    padding: 12,
                    borderRadius: 6,
                    fontFamily: "monospace",
                    fontSize: 13,
                    color: "var(--accent-hover)",
                    overflowX: "auto"
                  }}
                >
                  <span style={{ color: "var(--text-muted)", marginRight: 8 }}>Formula / Rule:</span>
                  {term.formula_or_rule}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

