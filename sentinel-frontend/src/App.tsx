import { useState } from "react";
import { submitQuery, submitDecision } from "./api/sentinel"; // Assuming API functions are correct
import QueryInput from "./components/QueryInput";
import SQLReview from "./components/SQLReview";
import ResultsTable from "./components/ResultsTable";
import ErrorBanner from "./components/ErrorBanner";
import LoadingIndicator from "./components/LoadingIndicator";

/**
 * Explicit UI workflow states
 * Mirrors backend execution lifecycle
 */
type AppStatus =
  | "idle"
  | "loading"
  | "rejected"
  | "requires_human"
  | "completed";

function App() {
  const styles = {
    app: {
      display: "flex",
      flexDirection: "column" as const,
      height: "100vh",
      backgroundColor: "#f7f8fa",
      fontFamily: "Inter, system-ui, sans-serif",
      color: "#1d2939",
    },
    header: {
      padding: "12px 24px",
      borderBottom: "1px solid #e9ecef",
      backgroundColor: "#ffffff",
    },
    logo: {
      fontSize: "24px",
      fontWeight: 700,
      color: "#1a365d",
      margin: 0,
    },
    mainContent: {
      flex: 1,
      overflowY: "auto" as const,
      padding: "24px",
    },
    container: { maxWidth: "1200px", margin: "0 auto", display: "flex", flexDirection: "column" as const, gap: "32px" },
    cardTitle: { marginTop: 0, color: "#1a365d", fontSize: "22px", fontWeight: 600, marginBottom: "24px" },
    executedSqlContainer: { marginBottom: "24px" },
    executedSqlLabel: { fontSize: "14px", fontWeight: 600, color: "#555", marginBottom: "8px" },
    pre: { background: "#f8f9fa", padding: "16px", borderRadius: "8px", overflowX: "auto" as const, fontSize: "14px", border: "1px solid #e0e0e0", color: "#333", fontFamily: "monospace" },
    hr: { border: "none", borderTop: "1px solid #e9ecef", margin: "0 0 24px 0" },
    humanApprovalTitle: { marginTop: 0, fontSize: "22px", fontWeight: 600, color: "#c83e4d" },
    section: { ...{ background: "#ffffff", padding: "32px", borderRadius: "12px", boxShadow: "0 4px 12px rgba(0,0,0,0.05)", border: "1px solid #e9ecef" } },
    inputSection: {
      padding: "24px",
      backgroundColor: "#ffffff",
      borderTop: "1px solid #e9ecef",
      boxShadow: "0 -4px 12px rgba(0,0,0,0.05)",
    },
    inputCard: {
      maxWidth: "800px",
      margin: "0 auto",
    },
  };

  const [status, setStatus] = useState<AppStatus>("idle");
  const [response, setResponse] = useState<any>(null);
  const isLoading = status === "loading";

  // ------------------------
  // Query submission
  // ------------------------
  async function handleSubmit(query: string) {
    setStatus("loading");
    setResponse(null);

    try {
      const res = await submitQuery(query);
      setResponse(res);
      setStatus(res.status);
    } catch {
      setStatus("rejected");
      setResponse({ reason: "Backend error while processing query" });
    }
  }

  // ------------------------
  // HITL actions
  // ------------------------
  async function handleApprove() {
    setStatus("loading");

    try {
      const res = await submitDecision({
        run_id: response.run_id,
        decision: "approve",
      });
      setResponse(res);
      setStatus("completed");
    } catch {
      setStatus("rejected");
      setResponse({ reason: "Approval failed" });
    }
  }

  async function handleEdit(editedSql: string) {
    setStatus("loading");

    try {
      const res = await submitDecision({
        run_id: response.run_id,
        decision: "edit",
        edited_sql: editedSql,
      });
      setResponse(res);
      setStatus("completed");
    } catch {
      setStatus("rejected");
      setResponse({ reason: "Edited SQL execution failed" });
    }
  }

  function handleReject() {
    setStatus("rejected");
    setResponse({
      reason: "Query rejected by human reviewer",
    });
  }

  // ------------------------
  // UI
  // ------------------------
  return (
    <div
      style={styles.app}
    >
      <header style={styles.header}>
        <h1 style={styles.logo}>SentinelSQL</h1>
      </header>

      <main style={styles.mainContent}>
        <div style={styles.container}>
          {isLoading && <LoadingIndicator />}

          {status === "rejected" && response?.reason && (
            <ErrorBanner message={response.reason} />
          )}

          {status === "requires_human" && response && (
            <div style={styles.section}>
              <h2 style={styles.humanApprovalTitle}>Human Approval Required</h2>
              <hr style={styles.hr} />
              <SQLReview
                runId={response.run_id}
                sql={response.sql}
                reason={response.reason}
                onApprove={handleApprove}
                onEdit={handleEdit}
                onReject={handleReject}
                loading={isLoading}
              />
            </div>
          )}

          {status === "completed" && response && (
            <div
              style={styles.section}
            >
              <h2 style={styles.cardTitle}>Results</h2>

              <div style={styles.executedSqlContainer}>
                <p style={styles.executedSqlLabel}>
                  <strong>Executed SQL:</strong>
                </p>
                <pre style={styles.pre}>
                  {response.sql}
                </pre>
              </div>

              {response.results && <ResultsTable results={response.results} />}
            </div>
          )}
        </div>
      </main>

      <section style={styles.inputSection}>
        <div style={styles.inputCard}>
          <QueryInput onSubmit={handleSubmit} loading={isLoading} />
        </div>
      </section>
    </div>
  );
}

export default App;
