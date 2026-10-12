import { useEffect, useState } from "react";
import { submitQuery, fetchSession, setToken, AuthError } from "./api/sentinel";
import type { Session } from "./api/sentinel";
import Login from "./components/Login";
import QueryInput from "./components/QueryInput";
import ResultsTable from "./components/ResultsTable";
import ErrorBanner from "./components/ErrorBanner";
import LoadingIndicator from "./components/LoadingIndicator";
import AdminDashboard from "./components/AdminDashboard";
import AuditLogViewer from "./components/AuditLogViewer";

type AppStatus = "idle" | "loading" | "rejected" | "error" | "requires_human" | "completed";

export default function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [sessionChecked, setSessionChecked] = useState(false);
  const [activeTab, setActiveTab] = useState<"query" | "admin" | "audit">("query");
  const [status, setStatus] = useState<AppStatus>("idle");
  const [response, setResponse] = useState<any>(null);
  const [submittedQuery, setSubmittedQuery] = useState<string>("");
  
  const isLoading = status === "loading";
  const isAdmin = session?.role === "admin";

  // Restore a previous session (the token lives in sessionStorage) if the server still accepts it.
  useEffect(() => {
    fetchSession()
      .then(setSession)
      .catch(() => setSession(null))
      .finally(() => setSessionChecked(true));
  }, []);

  function handleLogout() {
    setToken(null);
    setSession(null);
    setResponse(null);
    setStatus("idle");
    setActiveTab("query");
  }

  async function handleSubmit(query: string) {
    setSubmittedQuery(query);
    setStatus("loading");
    setResponse(null);

    try {
      const res = await submitQuery(query);
      setResponse(res);
      setStatus(res.status);
    } catch (e) {
      if (e instanceof AuthError) {
        handleLogout();
        return;
      }
      setStatus("rejected");
      setResponse({ reason: e instanceof Error ? e.message : "Backend error while processing query" });
    }
  }

  if (!sessionChecked) return null;
  if (!session) return <Login onLogin={setSession} />;

  return (
    <div className="animate-fade-in" style={{ display: "flex", flexDirection: "column", height: "100vh" }}>
      <header className="glass-header" style={{ padding: "16px 32px", display: "flex", justifyContent: "space-between", alignItems: "center", position: "sticky", top: 0, zIndex: 50 }}>
        <h1 style={{ fontSize: "24px", fontWeight: 700, margin: 0, letterSpacing: "-0.5px" }}>
          Sentinel<span style={{ color: "var(--accent)" }}>SQL</span>
        </h1>
        
        <div style={{ display: "flex", gap: "12px", alignItems: "center" }}>
          <button className={activeTab === "query" ? "primary" : ""} onClick={() => setActiveTab("query")}>
            Terminal
          </button>
          {isAdmin && (
            <button className={activeTab === "admin" ? "primary" : ""} onClick={() => setActiveTab("admin")}>
              Admin Queue
            </button>
          )}
          {isAdmin && (
            <button className={activeTab === "audit" ? "primary" : ""} onClick={() => setActiveTab("audit")}>
              Audit Logs
            </button>
          )}
          <span style={{ color: "var(--text-muted)", fontSize: "13px", marginLeft: "8px" }}>
            {session.user_id} ({session.role})
          </span>
          <button onClick={handleLogout}>Sign out</button>
        </div>
      </header>

      <main style={{ flex: 1, overflowY: "auto", padding: "32px" }}>
        {activeTab === "audit" && isAdmin ? (
          <AuditLogViewer />
        ) : activeTab === "admin" && isAdmin ? (
          <AdminDashboard 
            onDecisionComplete={(runId, result) => {
              if (response?.run_id === runId) {
                setResponse(result);
                setStatus(result.status);
                setActiveTab("query");
              }
            }}
          />
        ) : (
          <div style={{ maxWidth: "1000px", margin: "0 auto", display: "flex", flexDirection: "column", gap: "32px" }}>

            <div className="glass-panel animate-fade-in" style={{ padding: "32px" }}>
              <h2 style={{ marginTop: 0, marginBottom: "8px", fontSize: "28px" }}>Query Data</h2>
              <p style={{ color: "var(--text-muted)", margin: "0 0 24px 0" }}>
                Ask questions in plain English and let AI generate the SQL securely.
              </p>
              <QueryInput onSubmit={handleSubmit} loading={isLoading} />
            </div>

            {isLoading && (
              <div className="glass-panel" style={{ padding: "48px", textAlign: "center" }}>
                <LoadingIndicator />
              </div>
            )}

            {(status === "rejected" || status === "error") && response?.reason && (
              <div className="glass-panel animate-fade-in" style={{ padding: "24px" }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "16px" }}>
                  <h3 style={{ margin: 0, color: status === "error" ? "var(--warning)" : "#f87171" }}>
                    {status === "error" ? "Could Not Complete Request" : "Query Intercepted & Blocked"}
                  </h3>
                  <span className="badge badge-danger" style={{ fontSize: "13px", padding: "6px 12px", borderRadius: "16px" }}>
                    {status === "error" ? "System error: please retry" : `Confidence: ${response.confidence_score ?? 0}% (Security Violation)`}
                  </span>
                </div>
                {submittedQuery && (
                  <div style={{ marginBottom: "16px" }}>
                    <span style={{ fontSize: "12px", textTransform: "uppercase", letterSpacing: "0.5px", color: "var(--text-muted)", fontWeight: 600 }}>Your Question:</span>
                    <p style={{ margin: "4px 0 0 0", color: "var(--text-main)", fontStyle: "italic" }}>"{submittedQuery}"</p>
                  </div>
                )}
                <ErrorBanner message={response.reason} />
              </div>
            )}

            {status === "requires_human" && (
              <div className="glass-panel animate-fade-in" style={{ padding: "32px", border: "1px solid var(--warning)" }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "16px" }}>
                  <h2 style={{ margin: 0, color: "var(--warning)" }}>Human Approval Required</h2>
                  <span className="badge badge-warning" style={{ fontSize: "13px", padding: "6px 12px", borderRadius: "16px" }}>
                    Confidence: {response.confidence_score ?? 75}% (Deterministic AST Sensitive Tier)
                  </span>
                </div>
                {submittedQuery && (
                  <div style={{ marginBottom: "16px", padding: "12px 16px", background: "rgba(255,255,255,0.03)", borderRadius: "8px", border: "1px solid var(--border)" }}>
                    <span style={{ fontSize: "11px", textTransform: "uppercase", letterSpacing: "0.5px", color: "var(--text-muted)", fontWeight: 700 }}>Your Question:</span>
                    <p style={{ margin: "4px 0 0 0", color: "var(--text-main)", fontSize: "15px" }}>"{submittedQuery}"</p>
                  </div>
                )}
                <p>This query has been flagged for human review and added to the <strong>Admin Queue</strong>.</p>
                <p style={{ color: "var(--text-muted)", fontSize: "14px", fontStyle: "italic" }}>
                  Reason: {response.reason}
                </p>
                {isAdmin ? (
                  <button style={{ marginTop: 16 }} onClick={() => setActiveTab("admin")}>
                    Go to Admin Queue
                  </button>
                ) : (
                  <p style={{ color: "var(--text-muted)", fontSize: "14px" }}>
                    An administrator other than you will review it. Run ID: <code>{response.run_id}</code>
                  </p>
                )}
              </div>
            )}

            {status === "completed" && response && (
              <div className="glass-panel animate-fade-in" style={{ padding: "32px" }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "16px", flexWrap: "wrap", gap: "8px" }}>
                  <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
                    <h2 style={{ margin: 0, color: "var(--success)" }}>Success</h2>
                    {response.cached && (
                      <span className="badge" style={{ fontSize: "12px", padding: "4px 10px", borderRadius: "12px", background: "rgba(56, 189, 248, 0.2)", color: "#38bdf8", border: "1px solid rgba(56, 189, 248, 0.4)", display: "flex", alignItems: "center", gap: "4px" }}>
                        ⚡ Cached ({response.latency_ms ?? 0.5}ms)
                        {response.cache_match_type === "semantic" && response.cache_original_query && (
                          <> — reused answer to a similar question: "{response.cache_original_query}" ({Math.round((response.cache_similarity ?? 0) * 100)}% match)</>
                        )}
                      </span>
                    )}
                  </div>
                  <span className="badge badge-success" style={{ fontSize: "13px", padding: "6px 12px", borderRadius: "16px" }}>
                    Confidence: {response.confidence_score ?? 100}% (AST Validated)
                  </span>
                </div>
                
                {submittedQuery && (
                  <div style={{ marginBottom: "20px" }}>
                    <span style={{ fontSize: "11px", textTransform: "uppercase", letterSpacing: "0.5px", color: "var(--text-muted)", fontWeight: 700 }}>Your Question:</span>
                    <p style={{ margin: "4px 0 0 0", color: "var(--text-main)", fontSize: "16px", fontWeight: 500 }}>"{submittedQuery}"</p>
                  </div>
                )}

                <div style={{ marginBottom: "24px" }}>
                  <p style={{ fontSize: "14px", fontWeight: 600, color: "var(--text-muted)", marginBottom: "8px" }}>Executed SQL:</p>
                  <pre style={{ background: "rgba(0,0,0,0.3)", padding: "16px", borderRadius: "8px", overflowX: "auto", border: "1px solid var(--border)", fontFamily: "monospace", color: "#60a5fa" }}>
                    {response.sql}
                  </pre>
                </div>
                {response.results && <ResultsTable results={response.results} />}
              </div>
            )}
          </div>
        )}
      </main>
    </div>
  );
}
