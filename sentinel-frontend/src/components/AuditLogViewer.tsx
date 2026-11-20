import { useEffect, useState } from "react";
import Icon from "./Icon";
import Verdict, { type Tone } from "./Verdict";
import { getAuditLogs, clearAuditLogs, verifyAuditChain, type AuditLogEntry, type ChainVerification } from "../api/sentinel";

const CHANNEL_LABEL: Record<string, string> = { MCP_AGENT: "MCP agent", WEB_CHAT: "Analyst chat", WEB_UI: "Web UI" };

export default function AuditLogViewer() {
  const [logs, setLogs] = useState<AuditLogEntry[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [clearing, setClearing] = useState<boolean>(false);
  const [verifying, setVerifying] = useState<boolean>(false);
  const [chain, setChain] = useState<ChainVerification | null>(null);
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

  // Recomputes the hash chain on the server; any edited, removed or reordered row makes it report where the chain breaks.
  async function handleVerify() {
    setVerifying(true);
    setError(null);
    try {
      setChain(await verifyAuditChain());
    } catch (err: any) {
      setError(err.message || "Could not verify the audit chain");
    } finally {
      setVerifying(false);
    }
  }

  async function handleClearLogs() {
    if (!window.confirm("Permanently clear the whole audit log? This cannot be undone.")) {
      return;
    }
    setClearing(true);
    setError(null);
    setNotification(null);
    try {
      const res = await clearAuditLogs();
      setNotification(`Cleared ${res.deleted_count} audit log records`);
      await fetchLogs();
    } catch (err: any) {
      setError(err.message || "Failed to clear audit logs");
    } finally {
      setClearing(false);
    }
  }

  function riskVerdict(risk: string) {
    const r = (risk || "LOW").toUpperCase();
    const tone: Tone = r === "HIGH" ? "bad" : r === "MEDIUM" ? "warn" : "ok";
    return <Verdict tone={tone}>{r.toLowerCase()}</Verdict>;
  }

  function statusVerdict(status: string) {
    const s = (status || "").toLowerCase();
    const tone: Tone = s === "cached" ? "info"
      : s.includes("approved") || s === "completed" ? "ok"
      : s.includes("requires_human") || s === "pending" ? "warn"
      : "bad";
    return <Verdict tone={tone}>{status}</Verdict>;
  }

  const busy = loading || clearing;

  return (
    <>
      <div className="page-head">
        <div>
          <h2 className="page-title">Audit log</h2>
          <p className="lede">
            A hash-chained record of every request: the question, the generated SQL, the channel it came through, its risk level and the outcome. Verify chain recomputes the hashes and reports any edited, removed or reordered row.
          </p>
        </div>

        <div className="actions">
          <button className="btn btn-primary" onClick={handleVerify} disabled={busy || verifying}>
            {verifying ? "Verifying…" : "Verify chain"}
          </button>
          <button className="btn" onClick={fetchLogs} disabled={busy}>
            <Icon name="refresh" />
            Refresh
          </button>
          <button className="btn btn-danger" onClick={handleClearLogs} disabled={busy || logs.length === 0}>
            <Icon name="trash" />
            {clearing ? "Clearing…" : "Clear log"}
          </button>
        </div>
      </div>

      {chain && (
        <div role="status" className={chain.valid ? "notice notice-ok" : "notice notice-bad"}>
          <span className={chain.valid ? "dot dot-ok" : "dot dot-bad"} aria-hidden="true" />
          <span>
            {chain.valid
              ? `Chain verified: ${chain.checked} rows recomputed, no tampering found`
              : `Chain broken at row ${chain.first_invalid_id}${chain.reason ? `: ${chain.reason}` : ""}`}
          </span>
        </div>
      )}

      {notification && (
        <div className="notice notice-ok" role="status">
          <span className="dot dot-ok" aria-hidden="true" />
          <span>{notification}</span>
        </div>
      )}

      {error && (
        <div className="notice notice-bad" role="alert">
          <span className="dot dot-bad" aria-hidden="true" />
          <span>{error}</span>
        </div>
      )}

      <div className="actions" style={{ gap: 16 }}>
        <div className="filter">
          <label className="label" htmlFor="audit-channel">Channel</label>
          <select id="audit-channel" value={ingressFilter} onChange={(e) => setIngressFilter(e.target.value)}>
            <option value="ALL">All channels</option>
            <option value="WEB_UI">Web UI</option>
            <option value="WEB_CHAT">Analyst chat</option>
            <option value="MCP_AGENT">MCP agent</option>
          </select>
        </div>

        <div className="filter">
          <label className="label" htmlFor="audit-status">Status</label>
          <select id="audit-status" value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
            <option value="ALL">All statuses</option>
            <option value="completed">Completed</option>
            <option value="cached">Cached</option>
            <option value="requires_human">Requires human</option>
            <option value="hitl_approved">HITL approved</option>
            <option value="hitl_rejected">HITL rejected</option>
            <option value="rejected">Rejected</option>
            <option value="error">Error</option>
          </select>
        </div>
      </div>

      {loading ? (
        <div className="card empty">Loading the audit log…</div>
      ) : logs.length === 0 ? (
        <div className="card empty">No audit records match these filters</div>
      ) : (
        <div className="table-wrap">
          <table className="table table-hover">
            <thead>
              <tr>
                <th>Time</th>
                <th>Channel</th>
                <th>User or agent</th>
                <th>Risk</th>
                <th>Status</th>
                <th>Question</th>
                <th>SQL</th>
                <th className="num">Time taken</th>
                <th className="num">Rows</th>
              </tr>
            </thead>
            <tbody>
              {logs.map((log) => (
                <tr key={log.id}>
                  <td className="nowrap">
                    {log.timestamp ? (
                      <>
                        <div>{new Date(log.timestamp).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" })}</div>
                        <div className="meta">{new Date(log.timestamp).toLocaleTimeString()}</div>
                      </>
                    ) : "–"}
                  </td>
                  <td className="nowrap">
                    <span className="tag">{CHANNEL_LABEL[log.ingress_channel] ?? "Web UI"}</span>
                  </td>
                  <td className="nowrap">
                    <div>{log.user_id}</div>
                    <div className="meta">{log.client_agent || log.role}</div>
                  </td>
                  <td className="nowrap">{riskVerdict(log.risk_level)}</td>
                  <td className="nowrap">
                    {statusVerdict(log.execution_status)}
                    {log.approved_by && <div className="meta">by {log.approved_by}</div>}
                  </td>
                  <td className="truncate" title={log.natural_language_query}>
                    {log.natural_language_query || "–"}
                  </td>
                  <td className="truncate mono" style={{ color: "var(--accent-ink)", fontSize: 12 }} title={log.generated_sql}>
                    {log.generated_sql || "–"}
                  </td>
                  <td className="num">{log.execution_time_ms ? `${log.execution_time_ms.toLocaleString(undefined, { maximumFractionDigits: log.execution_time_ms < 10 ? 1 : 0 })} ms` : "–"}</td>
                  <td className="num">{log.masked_rows_count !== undefined ? log.masked_rows_count : "–"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
