import { useEffect, useState } from "react";
import { getAuditLogs, clearAuditLogs, type AuditLogEntry } from "../api/sentinel";

export default function AuditLogViewer() {
  const [logs, setLogs] = useState<AuditLogEntry[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [clearing, setClearing] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [notification, setNotification] = useState<string | null>(null);
  const [ingressFilter, setIngressFilter] = useState<string>("ALL");
  const [statusFilter, setStatusFilter] = useState<string>("ALL");

  async function fetchLogs() {
    setLoading(true);
    setError(null);
    try {
      const params: any = { limit: 50 };
      if (ingressFilter !== "ALL") params.ingress = ingressFilter;
      if (statusFilter !== "ALL") params.status = statusFilter;
      const data = await getAuditLogs(params);
      setLogs(data);
    } catch (err: any) {
      setError(err.message || "Failed to load audit logs");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    fetchLogs();
  }, [ingressFilter, statusFilter]);

  async function handleClearLogs() {
    if (!window.confirm("Are you sure you want to permanently clear all audit logs? This action cannot be undone.")) {
      return;
    }
    setClearing(true);
    setError(null);
    setNotification(null);
    try {
      const res = await clearAuditLogs();
      setNotification(`Successfully cleared ${res.deleted_count} audit log records.`);
      await fetchLogs();
    } catch (err: any) {
      setError(err.message || "Failed to clear audit logs");
    } finally {
      setClearing(false);
    }
  }

  function getRiskBadge(risk: string) {
    const r = (risk || "LOW").toUpperCase();
    if (r === "HIGH") {
      return <span style={{ background: "rgba(239, 68, 68, 0.2)", color: "#ef4444", padding: "3px 8px", borderRadius: "12px", fontSize: "11px", fontWeight: 700, border: "1px solid rgba(239, 68, 68, 0.4)" }}>HIGH RISK</span>;
    }
    if (r === "MEDIUM") {
      return <span style={{ background: "rgba(245, 158, 11, 0.2)", color: "#f59e0b", padding: "3px 8px", borderRadius: "12px", fontSize: "11px", fontWeight: 700, border: "1px solid rgba(245, 158, 11, 0.4)" }}>MEDIUM</span>;
    }
    return <span style={{ background: "rgba(16, 185, 129, 0.2)", color: "#10b981", padding: "3px 8px", borderRadius: "12px", fontSize: "11px", fontWeight: 700, border: "1px solid rgba(16, 185, 129, 0.4)" }}>LOW</span>;
  }

  function getStatusBadge(status: string) {
    const s = (status || "").toLowerCase();
    if (s === "cached") {
      return <span style={{ color: "#38bdf8", fontWeight: 600 }}>⚡ cached</span>;
    }
    if (s.includes("approved") || s === "completed") {
      return <span style={{ color: "#34d399", fontWeight: 600 }}>● {status}</span>;
    }
    if (s.includes("requires_human") || s === "pending") {
      return <span style={{ color: "#fbbf24", fontWeight: 600 }}>⏳ {status}</span>;
    }
    return <span style={{ color: "#f87171", fontWeight: 600 }}>✕ {status}</span>;
  }

  function getIngressBadge(ingress: string) {
    if (ingress === "MCP_AGENT") {
      return <span style={{ background: "rgba(168, 85, 247, 0.2)", color: "#c084fc", padding: "3px 8px", borderRadius: "6px", fontSize: "11px", fontWeight: 600, border: "1px solid rgba(168, 85, 247, 0.4)" }}>🤖 MCP AGENT</span>;
    }
    return <span style={{ background: "rgba(59, 130, 246, 0.2)", color: "#60a5fa", padding: "3px 8px", borderRadius: "6px", fontSize: "11px", fontWeight: 600, border: "1px solid rgba(59, 130, 246, 0.4)" }}>💻 WEB UI</span>;
  }

  return (
    <div className="glass-panel animate-fade-in" style={{ padding: "32px", maxWidth: "1200px", margin: "0 auto" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "24px" }}>
        <div>
          <h2 style={{ margin: "0 0 6px 0", fontSize: "24px" }}>SOC 2 Compliance Audit Log</h2>
          <p style={{ margin: 0, color: "var(--text-muted)", fontSize: "14px" }}>
            Immutable event trail capturing prompt intent, generated SQL, ingress channels, risk evaluations, and execution metrics.
          </p>
        </div>

        <div style={{ display: "flex", gap: "10px", alignItems: "center" }}>
          <button onClick={fetchLogs} style={{ padding: "8px 16px", cursor: "pointer" }} disabled={loading || clearing}>
            🔄 Refresh
          </button>
          <button 
            onClick={handleClearLogs} 
            disabled={loading || clearing || logs.length === 0}
            style={{ 
              padding: "8px 16px", 
              cursor: (loading || clearing || logs.length === 0) ? "not-allowed" : "pointer",
              backgroundColor: "rgba(239, 68, 68, 0.15)",
              color: "#f87171",
              border: "1px solid rgba(239, 68, 68, 0.4)",
              opacity: (loading || clearing || logs.length === 0) ? 0.5 : 1
            }}
          >
            {clearing ? "⏳ Clearing..." : "🗑️ Clear All Logs"}
          </button>
        </div>
      </div>

      {/* Filter Controls */}
      <div style={{ display: "flex", gap: "16px", marginBottom: "20px", alignItems: "center" }}>
        <div>
          <label style={{ fontSize: "12px", color: "var(--text-muted)", marginRight: "8px", fontWeight: 600 }}>INGRESS:</label>
          <select 
            value={ingressFilter} 
            onChange={(e) => setIngressFilter(e.target.value)}
            style={{ padding: "6px 12px", borderRadius: "6px", backgroundColor: "#1e293b", color: "#f1f5f9", border: "1px solid var(--border)" }}
          >
            <option value="ALL" style={{ backgroundColor: "#0f172a", color: "#f1f5f9" }}>All Channels</option>
            <option value="WEB_UI" style={{ backgroundColor: "#0f172a", color: "#f1f5f9" }}>Web UI</option>
            <option value="MCP_AGENT" style={{ backgroundColor: "#0f172a", color: "#f1f5f9" }}>MCP Agent</option>
          </select>
        </div>

        <div>
          <label style={{ fontSize: "12px", color: "var(--text-muted)", marginRight: "8px", fontWeight: 600 }}>STATUS:</label>
          <select 
            value={statusFilter} 
            onChange={(e) => setStatusFilter(e.target.value)}
            style={{ padding: "6px 12px", borderRadius: "6px", backgroundColor: "#1e293b", color: "#f1f5f9", border: "1px solid var(--border)" }}
          >
            <option value="ALL" style={{ backgroundColor: "#0f172a", color: "#f1f5f9" }}>All Statuses</option>
            <option value="completed" style={{ backgroundColor: "#0f172a", color: "#f1f5f9" }}>Completed</option>
            <option value="cached" style={{ backgroundColor: "#0f172a", color: "#f1f5f9" }}>⚡ Cached</option>
            <option value="requires_human" style={{ backgroundColor: "#0f172a", color: "#f1f5f9" }}>Requires Human</option>
            <option value="hitl_approved" style={{ backgroundColor: "#0f172a", color: "#f1f5f9" }}>HITL Approved</option>
            <option value="hitl_rejected" style={{ backgroundColor: "#0f172a", color: "#f1f5f9" }}>HITL Rejected</option>
            <option value="rejected" style={{ backgroundColor: "#0f172a", color: "#f1f5f9" }}>Rejected</option>
            <option value="mcp_rejected" style={{ backgroundColor: "#0f172a", color: "#f1f5f9" }}>MCP Rejected</option>
          </select>
        </div>
      </div>

      {notification && (
        <div style={{ padding: "12px 16px", background: "rgba(16, 185, 129, 0.15)", border: "1px solid rgba(16, 185, 129, 0.4)", borderRadius: "8px", color: "#34d399", marginBottom: "16px" }}>
          {notification}
        </div>
      )}

      {error && (
        <div style={{ padding: "12px 16px", background: "rgba(239, 68, 68, 0.15)", border: "1px solid rgba(239, 68, 68, 0.4)", borderRadius: "8px", color: "#f87171", marginBottom: "16px" }}>
          {error}
        </div>
      )}

      {loading ? (
        <div style={{ textAlign: "center", padding: "40px", color: "var(--text-muted)" }}>
          Loading compliance audit trail...
        </div>
      ) : logs.length === 0 ? (
        <div style={{ textAlign: "center", padding: "40px", color: "var(--text-muted)" }}>
          No audit records found matching selected filters.
        </div>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "13px" }}>
            <thead>
              <tr style={{ borderBottom: "1px solid var(--border)", textAlign: "left", color: "var(--text-muted)" }}>
                <th style={{ padding: "12px 8px" }}>DATE & TIME</th>
                <th style={{ padding: "12px 8px" }}>INGRESS</th>
                <th style={{ padding: "12px 8px" }}>USER / AGENT</th>
                <th style={{ padding: "12px 8px" }}>RISK</th>
                <th style={{ padding: "12px 8px" }}>STATUS</th>
                <th style={{ padding: "12px 8px" }}>INTENT / QUERY</th>
                <th style={{ padding: "12px 8px" }}>SQL STATEMENT</th>
                <th style={{ padding: "12px 8px" }}>METRICS</th>
              </tr>
            </thead>
            <tbody>
              {logs.map((log) => (
                <tr key={log.id} style={{ borderBottom: "1px solid rgba(255, 255, 255, 0.05)" }}>
                  <td style={{ padding: "12px 8px", whiteSpace: "nowrap", fontSize: "12px" }}>
                    {log.timestamp ? (
                      <div>
                        <div style={{ color: "var(--text-main)", fontWeight: 500 }}>
                          {new Date(log.timestamp).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" })}
                        </div>
                        <div style={{ fontSize: "11px", color: "var(--text-muted)" }}>
                          {new Date(log.timestamp).toLocaleTimeString()}
                        </div>
                      </div>
                    ) : "-"}
                  </td>
                  <td style={{ padding: "12px 8px", whiteSpace: "nowrap" }}>
                    {getIngressBadge(log.ingress_channel)}
                  </td>
                  <td style={{ padding: "12px 8px", whiteSpace: "nowrap" }}>
                    <strong>{log.user_id}</strong>
                    <div style={{ fontSize: "11px", color: "var(--text-muted)" }}>{log.client_agent || log.role}</div>
                  </td>
                  <td style={{ padding: "12px 8px", whiteSpace: "nowrap" }}>
                    {getRiskBadge(log.risk_level)}
                  </td>
                  <td style={{ padding: "12px 8px", whiteSpace: "nowrap" }}>
                    {getStatusBadge(log.execution_status)}
                    {log.approved_by && (
                      <div style={{ fontSize: "10px", color: "var(--text-muted)" }}>by: {log.approved_by}</div>
                    )}
                  </td>
                  <td style={{ padding: "12px 8px", maxWidth: "200px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={log.natural_language_query}>
                    {log.natural_language_query || "-"}
                  </td>
                  <td style={{ padding: "12px 8px", maxWidth: "240px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontFamily: "monospace", color: "#60a5fa" }} title={log.generated_sql}>
                    {log.generated_sql || "-"}
                  </td>
                  <td style={{ padding: "12px 8px", whiteSpace: "nowrap", fontSize: "11px", color: "var(--text-muted)" }}>
                    <div>⏱ {log.execution_time_ms ? `${log.execution_time_ms}ms` : "-"}</div>
                    <div>📊 {log.masked_rows_count !== undefined ? `${log.masked_rows_count} rows` : "-"}</div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
