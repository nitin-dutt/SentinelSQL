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
import ResultsTable from "./ResultsTable";
import Icon from "./Icon";
import Verdict from "./Verdict";

interface AdminDashboardProps {
  onDecisionComplete?: (runId: string, result: any) => void;
  /** The signed-in reviewer; requests they submitted themselves cannot be approved by them. */
  currentUserId?: string;
}

export default function AdminDashboard({ onDecisionComplete, currentUserId }: AdminDashboardProps) {
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
  const [refreshingQueue, setRefreshingQueue] = useState(false);
  const [queueNote, setQueueNote] = useState<string | null>(null);
  // Results of requests this reviewer approved in this visit. Salary-class values are readable here; the submitter still sees them masked.
  const [approved, setApproved] = useState<{ run_id: string; question: string; sql: string; results: any }[]>([]);
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

  // Reloads only the review queue (not the whole dashboard), so new requests appear without a page reload.
  async function refreshQueue() {
    setRefreshingQueue(true);
    setQueueNote(null);
    try {
      const latest = await getAdminQueue();
      setQueue(latest);
      setQueueNote(latest.length === 0 ? "Nothing waiting." : `${latest.length} waiting.`);
    } catch (e: any) {
      setQueueNote(e.message || "Could not refresh the queue");
    } finally {
      setRefreshingQueue(false);
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

      // Remove from queue, and keep the answer on screen for the reviewer
      const decided = queue.find(item => item.run_id === runId);
      setQueue(q => q.filter(item => item.run_id !== runId));
      if (res?.status === "completed" && res.results) {
        setApproved(a => [{ run_id: runId, question: decided?.state?.user_query ?? "", sql: res.sql ?? "", results: res.results }, ...a]);
      }

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
    if (!confirm(`Delete verified template #${id}?`)) {
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
    if (!confirm("Clear every cached query entry?")) {
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
      alert("Enter both a term name and an SQL formula or condition.");
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
    if (!confirm(`Delete '${termName}' from the business glossary?`)) {
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
  if (error) return <div className="notice notice-bad" role="alert"><span className="dot dot-bad" aria-hidden="true" /><span>{error}</span></div>;

  const cacheEmpty = (cacheStats?.total_entries ?? 0) === 0;

  return (
    <>
      <div className="page-head">
        <div>
          <h2 className="page-title">Admin queue</h2>
          <p className="lede">Review the queries the policy check held, then approve, edit or reject them.</p>
        </div>
        <div className="actions">
          {queueNote && <span className="meta">{queueNote}</span>}
          <button className="btn" onClick={refreshQueue} disabled={refreshingQueue}>
            <Icon name="refresh" />
            {refreshingQueue ? "Refreshing…" : "Refresh"}
          </button>
        </div>
      </div>

      {approved.map(a => (
        <section key={a.run_id} className="card enter">
          <div className="card-head">
            <h3 className="card-title">Approved and executed</h3>
            <div className="actions" style={{ gap: 16 }}>
              <Verdict tone="ok">approved · executed</Verdict>
              <button className="btn btn-sm" onClick={() => setApproved(list => list.filter(x => x.run_id !== a.run_id))}>Dismiss</button>
            </div>
          </div>
          <div className="field">
            <span className="label">Question</span>
            <p className="quote">{a.question}</p>
          </div>
          <div className="field">
            <span className="label">Executed SQL</span>
            <pre className="code">{a.sql}</pre>
          </div>
          <ResultsTable results={a.results} />
          <p className="muted small field-gap">
            Salary figures are shown to you because you approved this query. The person who asked still sees them as [CONFIDENTIAL]; emails, SSNs and card numbers stay masked for everyone.
          </p>
        </section>
      ))}

      {queue.length === 0 ? (
        <div className="card empty">
          <h3>Queue is empty</h3>
          <p>No queries are waiting for a reviewer</p>
        </div>
      ) : (
        queue.map(item => (
          <section key={item.run_id} className="card">
            <div className="card-head">
              <h3 className="card-title">Human approval required</h3>
              <Verdict tone="warn">
                held{item.state.user_id ? ` · submitted by ${item.state.user_id}${item.state.user_id === currentUserId ? " (you)" : ""}` : ""}
              </Verdict>
            </div>
            <div className="field">
              <span className="label">Question</span>
              <p className="quote">{item.state.user_query}</p>
            </div>
            <div className="field">
              <SQLReview
                runId={item.run_id}
                sql={item.state.sql || ""}
                reason={item.state.validation?.reason || "Flagged for review"}
                onApprove={(saveAsTemplate) => handleDecision(item.run_id, "approve", undefined, saveAsTemplate)}
                onEdit={(edited, saveAsTemplate) => handleDecision(item.run_id, "edit", edited, saveAsTemplate)}
                onReject={() => handleDecision(item.run_id, "reject")}
                loading={processingId === item.run_id}
                blockedReason={item.state.user_id && item.state.user_id === currentUserId
                  ? "You submitted this request, so another administrator must approve it (you can still reject it). Sign in as a different reviewer, or ask the question as the employee."
                  : undefined}
              />
            </div>
          </section>
        ))
      )}

      {/* Verified templates (golden queries) */}
      <section className="section">
        <div className="section-head">
          <div>
            <h3 className="section-title">
              Verified templates
              <span className="meta">{goldenQueries.length} saved</span>
            </h3>
            <p className="section-sub">Reviewer-approved queries, given to the model as few-shot examples for similar questions.</p>
          </div>
        </div>

        {goldenQueries.length === 0 ? (
          <div className="card empty">
            <p>
              No templates saved yet. When approving a query in the admin queue, tick "Save as a verified template" to add it.
            </p>
          </div>
        ) : (
          goldenQueries.map(template => (
            <div key={template.id} className="card">
              <div className="card-head" style={{ alignItems: "flex-start", flexWrap: "nowrap" }}>
                <div style={{ minWidth: 0 }}>
                  <span className="meta">Template #{template.id} · added by {template.created_by}</span>
                  <p className="quote" style={{ marginTop: 4 }}>{template.natural_language_query}</p>
                </div>
                <button
                  className="btn btn-sm btn-danger"
                  onClick={() => handleDeleteTemplate(template.id)}
                  disabled={deletingId === template.id}
                >
                  <Icon name="trash" />
                  {deletingId === template.id ? "Deleting…" : "Delete"}
                </button>
              </div>
              <pre className="code">{template.verified_sql}</pre>
            </div>
          ))
        )}
      </section>

      {/* Semantic cache statistics and clearing */}
      <section className="section">
        <div className="section-head">
          <div>
            <h3 className="section-title">
              Semantic cache
              <span className="meta">{cacheStats?.total_entries ?? 0} cached</span>
            </h3>
            <p className="section-sub">
              Two tiers: an exact-match hash (L1), then vector similarity (L2). A hit returns the stored answer without calling the model or the database.
            </p>
          </div>

          <button className="btn btn-danger" onClick={handleClearCache} disabled={clearingCache || cacheEmpty}>
            <Icon name="trash" />
            {clearingCache ? "Clearing…" : "Clear cache"}
          </button>
        </div>

        {cacheStats && (
          <div className="stats">
            {[
              { label: "Hit ratio", value: `${cacheStats.hit_ratio_percent}%` },
              { label: "Total hits", value: cacheStats.hits },
              { label: "L1 exact hits", value: cacheStats.exact_hits },
              { label: "L2 vector hits", value: cacheStats.semantic_hits },
              { label: "Misses", value: cacheStats.misses },
            ].map(stat => (
              <div key={stat.label} className="card stat">
                <div className="stat-label">{stat.label}</div>
                <div className="stat-value" style={{ marginBottom: 0 }}>{stat.value}</div>
              </div>
            ))}
          </div>
        )}
      </section>

      {/* Business glossary (data dictionary) */}
      <section className="section">
        <div className="section-head">
          <div>
            <h3 className="section-title">
              Business glossary
              <span className="meta">{dictionaryTerms.length} {dictionaryTerms.length === 1 ? "term" : "terms"}</span>
            </h3>
            <p className="section-sub">
              Maps business terms such as "Net revenue" or "VIP tier" to verified SQL formulas the model must use.
            </p>
          </div>

          <button className={showAddTerm ? "btn" : "btn btn-primary"} onClick={() => setShowAddTerm(!showAddTerm)}>
            <Icon name={showAddTerm ? "x" : "plus"} />
            {showAddTerm ? "Cancel" : "Add term"}
          </button>
        </div>

        {showAddTerm && (
          <form onSubmit={handleAddTerm} className="card stack" style={{ gap: 16 }}>
            <h4 className="card-title">New term</h4>

            <div className="form-grid">
              <div>
                <label className="label" htmlFor="term-name">Term name (required)</label>
                <input
                  id="term-name"
                  className="input"
                  type="text"
                  placeholder="Net revenue"
                  value={newTerm.term}
                  onChange={(e) => setNewTerm({ ...newTerm, term: e.target.value })}
                  required
                />
              </div>

              <div>
                <label className="label" htmlFor="term-category">Category</label>
                <select
                  id="term-category"
                  style={{ width: "100%" }}
                  value={newTerm.category}
                  onChange={(e) => setNewTerm({ ...newTerm, category: e.target.value })}
                >
                  <option value="metric">Metric (calculation formula)</option>
                  <option value="filter">Filter (WHERE condition)</option>
                  <option value="segment">Segment (cohort rule)</option>
                  <option value="model">Model or entity definition</option>
                </select>
              </div>

              <div>
                <label className="label" htmlFor="term-tables">Tables it applies to</label>
                <input
                  id="term-tables"
                  className="input input-mono"
                  type="text"
                  placeholder="orders, customers"
                  value={newTerm.related_tables}
                  onChange={(e) => setNewTerm({ ...newTerm, related_tables: e.target.value })}
                />
              </div>
            </div>

            <div>
              <label className="label" htmlFor="term-formula">SQL formula or condition (required)</label>
              <input
                id="term-formula"
                className="input input-mono"
                type="text"
                placeholder="SUM(orders.total_amount) - SUM(orders.discount_amount)"
                value={newTerm.formula_or_rule}
                onChange={(e) => setNewTerm({ ...newTerm, formula_or_rule: e.target.value })}
                required
              />
            </div>

            <div>
              <label className="label" htmlFor="term-definition">Definition</label>
              <textarea
                id="term-definition"
                className="input"
                rows={2}
                placeholder="How this metric should be calculated, in a sentence"
                value={newTerm.definition}
                onChange={(e) => setNewTerm({ ...newTerm, definition: e.target.value })}
              />
            </div>

            <div className="actions" style={{ justifyContent: "flex-end" }}>
              <button type="button" className="btn" onClick={() => setShowAddTerm(false)} disabled={addingTerm}>
                Cancel
              </button>
              <button type="submit" className="btn btn-primary" disabled={addingTerm}>
                {addingTerm ? "Saving…" : "Save term"}
              </button>
            </div>
          </form>
        )}

        {dictionaryTerms.length === 0 ? (
          <div className="card empty">
            <p>No glossary terms defined yet</p>
          </div>
        ) : (
          dictionaryTerms.map(term => (
            <div key={term.id} className="card">
              <div className="card-head" style={{ alignItems: "flex-start", flexWrap: "nowrap" }}>
                <div style={{ minWidth: 0 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                    <h4 className="card-title">{term.term}</h4>
                    <span className="tag">{term.category || "metric"}</span>
                    {term.related_tables && <span className="meta">tables: {term.related_tables}</span>}
                  </div>
                  {term.definition && <p className="muted" style={{ marginTop: 4 }}>{term.definition}</p>}
                </div>

                <button
                  className="btn btn-sm btn-danger"
                  onClick={() => handleDeleteTerm(term.id, term.term)}
                  disabled={deletingTermId === term.id}
                >
                  <Icon name="trash" />
                  {deletingTermId === term.id ? "Deleting…" : "Delete"}
                </button>
              </div>

              <span className="label">Formula or rule</span>
              <pre className="code">{term.formula_or_rule}</pre>
            </div>
          ))
        )}
      </section>
    </>
  );
}
