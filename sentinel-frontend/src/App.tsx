import { useEffect, useState } from "react";
import {
  submitChat, fetchSession, fetchRunStatus, setToken, AuthError,
  fetchLaunchInfo, fetchWorkspace, switchRole, disconnectWorkspace, getRoleTokens, setRoleTokens,
} from "./api/sentinel";
import type { Role, RoleTokens, Session, WorkspaceInfo } from "./api/sentinel";
import Launch from "./components/Launch";
import McpGuide from "./components/McpGuide";
import QueryInput from "./components/QueryInput";
import ResultsTable from "./components/ResultsTable";
import ErrorBanner from "./components/ErrorBanner";
import LoadingIndicator from "./components/LoadingIndicator";
import AdminDashboard from "./components/AdminDashboard";
import AuditLogViewer from "./components/AuditLogViewer";
import SchemaPanel from "./components/SchemaPanel";
import ObservabilityDashboard from "./components/ObservabilityDashboard";
import Verdict from "./components/Verdict";

type AppStatus = "idle" | "loading" | "rejected" | "error" | "requires_human" | "completed" | "needs_clarification";
type Tab = "query" | "admin" | "audit" | "observability" | "agent";

/** One earlier turn of the current conversation, shown above the newest answer. */
interface ThreadTurn {
  message: string;
  understoodAs?: string | null;
  outcome: string;
}

const OUTCOME_LABEL: Record<string, string> = {
  completed: "answered",
  requires_human: "held for review",
  rejected: "blocked",
  error: "could not complete",
  needs_clarification: "asked you a question",
};

const ADMIN_TABS: { id: Tab; label: string }[] = [
  { id: "admin", label: "Admin queue" },
  { id: "audit", label: "Audit log" },
  { id: "observability", label: "Observability" },
];

export default function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [sessionChecked, setSessionChecked] = useState(false);
  const [activeTab, setActiveTab] = useState<Tab>("query");
  const [status, setStatus] = useState<AppStatus>("idle");
  const [response, setResponse] = useState<any>(null);
  const [submittedQuery, setSubmittedQuery] = useState<string>("");
  // The server keeps the conversation (questions, SQL and answer shape, never rows) so a follow-up like "now by region" works.
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [thread, setThread] = useState<ThreadTurn[]>([]);

  const [examples, setExamples] = useState<string[]>([]);
  const [maxChars, setMaxChars] = useState<number | undefined>(undefined);
  const [refreshing, setRefreshing] = useState(false);
  const [refreshNote, setRefreshNote] = useState<string | null>(null);
  // Set when the session came from the launch page: the visitor's own database, and the tokens of the roles they can play.
  const [workspace, setWorkspace] = useState<WorkspaceInfo | null>(null);
  const [roles, setRoles] = useState<RoleTokens>({});

  const isLoading = status === "loading";
  const isAdmin = session?.role === "admin";

  // Restore a previous session (the token lives in sessionStorage) if the server still accepts it.
  useEffect(() => {
    fetchSession()
      .then(setSession)
      .catch(() => setSession(null))
      .finally(() => setSessionChecked(true));
  }, []);

  // Example questions are about the sample database, so they are offered on a dev or demo server and never on a visitor's own
  // database. A public demo also limits the question length.
  useEffect(() => {
    if (!session) return;
    setRoles(getRoleTokens());
    Promise.all([fetchLaunchInfo(), fetchWorkspace()]).then(([info, ws]) => {
      setWorkspace(ws);
      setExamples(info && info.mode !== "standard" && !ws ? info.examples : []);
      setMaxChars(info?.limits?.max_question_chars);
    });
  }, [session]);

  function handleLogout() {
    if (workspace) void disconnectWorkspace();   // the server drops the connection and deletes the workspace's files now
    setToken(null);
    setRoleTokens(null);
    setRoles({});
    setWorkspace(null);
    setSession(null);
    setResponse(null);
    setStatus("idle");
    setActiveTab("query");
    setConversationId(null);
    setThread([]);
  }

  // One person plays every role of a demo or a workspace: ask as the employee, then approve as the reviewer.
  async function handleSwitchRole(role: Role) {
    const next = await switchRole(role);
    if (!next) return;
    handleNewConversation();
    setActiveTab(role === "admin" && activeTab === "query" && status === "requires_human" ? "admin" : "query");
    setSession(next);
  }

  function handleNewConversation() {
    setConversationId(null);
    setThread([]);
    setResponse(null);
    setSubmittedQuery("");
    setStatus("idle");
  }

  // The employee asks again whether a reviewer has decided; a finished request replaces the "held for review" card with its answer.
  async function handleRefreshStatus(runId: string) {
    setRefreshing(true);
    setRefreshNote(null);
    try {
      const run = await fetchRunStatus(runId);
      if (run.status === "completed") {
        setResponse({ status: "completed", sql: run.sql, results: run.results, run_id: runId, confidence_score: 100 });
        setStatus("completed");
      } else if (run.status === "rejected" || run.status === "failed") {
        setResponse({ status: "rejected", reason: run.reason || "The reviewer rejected this request.", confidence_score: 0 });
        setStatus("rejected");
      } else {
        setRefreshNote("Still waiting: no reviewer has decided yet.");
      }
    } catch (e) {
      if (e instanceof AuthError) {
        handleLogout();
        return;
      }
      setRefreshNote(e instanceof Error ? e.message : "Could not check the request status");
    } finally {
      setRefreshing(false);
    }
  }

  async function handleSubmit(query: string) {
    setRefreshNote(null);
    setSubmittedQuery(query);
    setStatus("loading");
    setResponse(null);

    // the turn that was on screen moves into the thread before the new one replaces it
    if (response && submittedQuery && status !== "idle" && status !== "loading") {
      setThread((prev) => [...prev, { message: submittedQuery, understoodAs: response.standalone_question, outcome: status }]);
    }

    try {
      let res;
      try {
        res = await submitChat(query, conversationId);
      } catch (e) {
        // an expired conversation (24 hours without a question) is not an error for the user: start a new one
        if (conversationId && e instanceof Error && e.message.startsWith("Conversation not found")) {
          setThread([]);
          res = await submitChat(query, null);
        } else {
          throw e;
        }
      }
      setConversationId(res.conversation_id);
      setResponse(res);
      setStatus(res.status);
      if (workspace) void fetchWorkspace().then((ws) => ws && setWorkspace(ws));   // the free-question counter moved
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
  if (!session) return <Launch onEnter={setSession} />;

  const canSwitch = !!roles.employee && !!roles.admin && (session.role === "employee" || session.role === "admin");

  const question = submittedQuery && (
    <div className="field">
      <span className="label">Your question</span>
      <p className="quote">{submittedQuery}</p>
    </div>
  );

  return (
    <div className="app">
      <header className="topbar">
        <h1 className="brand">Sentinel<span>SQL</span></h1>

        <nav className="tabs" aria-label="Sections">
          <button className="tab" aria-current={activeTab === "query" ? "page" : undefined} onClick={() => setActiveTab("query")}>
            Terminal
          </button>
          {isAdmin && ADMIN_TABS.map((tab) => (
            <button key={tab.id} className="tab" aria-current={activeTab === tab.id ? "page" : undefined} onClick={() => setActiveTab(tab.id)}>
              {tab.label}
            </button>
          ))}
          {roles.external_ai && (
            <button className="tab" aria-current={activeTab === "agent" ? "page" : undefined} onClick={() => setActiveTab("agent")}>
              Agent access
            </button>
          )}
        </nav>

        <div className="topbar-end">
          {workspace && (
            <span className="meta session-chip" title={`${workspace.label}: ${workspace.tables} tables`}>
              <span className="dot dot-ok" aria-hidden="true" />
              {workspace.label}
            </span>
          )}
          {canSwitch ? (
            <div className="seg" role="group" aria-label="Role">
              <button aria-pressed={session.role === "employee"} onClick={() => handleSwitchRole("employee")}>Employee</button>
              <button aria-pressed={session.role === "admin"} onClick={() => handleSwitchRole("admin")}>Reviewer</button>
            </div>
          ) : (
            <span className="meta">{session.user_id} · {session.role}</span>
          )}
          <button className="btn btn-sm" onClick={handleLogout}>{workspace ? "Disconnect" : "Sign out"}</button>
        </div>
      </header>

      <main className="main">
        <div className="page enter" key={activeTab}>
          {activeTab === "agent" && roles.external_ai ? (
            <>
              <div className="page-head">
                <div>
                  <h2 className="page-title">Agent access</h2>
                  <p className="lede">
                    Drive SentinelSQL the way an AI agent does. These commands carry your own agent token for {workspace ? workspace.label : "the sample database"};
                    what you submit shows up in the audit log, and held queries in the admin queue.
                  </p>
                </div>
              </div>
              <McpGuide token={roles.external_ai} sampleTables={!workspace} />
            </>
          ) : activeTab === "observability" && isAdmin ? (
            <ObservabilityDashboard onAuthError={handleLogout} />
          ) : activeTab === "audit" && isAdmin ? (
            <AuditLogViewer />
          ) : activeTab === "admin" && isAdmin ? (
            <AdminDashboard
              currentUserId={session.user_id}
              onDecisionComplete={(runId, result) => {
                if (response?.run_id === runId) {
                  setResponse(result);
                  setStatus(result.status);
                  setActiveTab("query");
                }
              }}
            />
          ) : (
            <div className="query-layout">
              <div className="stack">
                <div>
                  <h2 className="page-title">Ask the database</h2>
                  <p className="lede">
                    Ask in plain English. SentinelSQL writes the SQL, checks it against policy and holds sensitive queries for a reviewer. Follow-up questions such as "now break that down by region" work.
                  </p>
                  {workspace && (
                    <p className="meta" style={{ marginTop: 8 }}>
                      {workspace.questions_used} of {workspace.question_quota} free questions used · the workspace closes after {workspace.idle_minutes_allowed} idle minutes
                    </p>
                  )}
                </div>

                {thread.length > 0 && (
                  <div className="card card-tight">
                    <div className="card-head" style={{ marginBottom: 8 }}>
                      <span className="label" style={{ margin: 0 }}>Earlier in this conversation</span>
                      <button type="button" className="btn btn-sm" disabled={isLoading} onClick={handleNewConversation}>New conversation</button>
                    </div>
                    <div className="stack-sm" style={{ gap: 4 }}>
                      {thread.map((turn, i) => (
                        <div key={i} style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 16 }}>
                          <span>{turn.message}</span>
                          <span className="meta" style={{ flexShrink: 0 }}>{OUTCOME_LABEL[turn.outcome] ?? turn.outcome}</span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                <div className="stack-sm">
                  <QueryInput onSubmit={handleSubmit} loading={isLoading} maxChars={maxChars} />
                  {examples.length > 0 && (
                    <div className="chips">
                      <span className="muted small">Try</span>
                      {examples.map((q) => (
                        <button key={q} type="button" className="btn btn-sm" disabled={isLoading} onClick={() => handleSubmit(q)}>
                          {q}
                        </button>
                      ))}
                    </div>
                  )}
                </div>

                {isLoading && (
                  <div className="card">
                    <LoadingIndicator />
                  </div>
                )}

                {(status === "rejected" || status === "error") && response?.reason && (
                  <section className="card enter">
                    <div className="card-head">
                      <h3 className="card-title">{status === "error" ? "Could not complete the request" : "Query blocked"}</h3>
                      <Verdict tone={status === "error" ? "warn" : "bad"}>
                        {status === "error" ? "system error · please retry" : `blocked · security violation · confidence ${response.confidence_score ?? 0}%`}
                      </Verdict>
                    </div>
                    {question}
                    <div className="field">
                      <ErrorBanner message={response.reason} />
                    </div>
                  </section>
                )}

                {status === "needs_clarification" && response?.clarification && (
                  <section className="card enter">
                    <div className="card-head">
                      <h3 className="card-title">One question before I answer</h3>
                      <Verdict tone="info">waiting for your answer</Verdict>
                    </div>
                    {question}
                    <div className="field">
                      <p className="quote">{response.clarification.question}</p>
                      <div className="chips" style={{ marginTop: 12 }}>
                        {(response.clarification.options ?? []).map((option: string) => (
                          <button key={option} type="button" className="btn btn-primary" onClick={() => handleSubmit(option)}>{option}</button>
                        ))}
                      </div>
                      <p className="muted small" style={{ marginTop: 12 }}>Or type your own answer in the box above.</p>
                    </div>
                  </section>
                )}

                {status === "requires_human" && (
                  <section className="card enter">
                    <div className="card-head">
                      <h3 className="card-title">Held for human review</h3>
                      <Verdict tone="warn">held · deterministic AST sensitive tier · confidence {response.confidence_score ?? 75}%</Verdict>
                    </div>
                    {question}
                    <div className="field">
                      <span className="label">Reason</span>
                      <p>{response.reason}</p>
                    </div>
                    <p className="muted field-gap">
                      This query was flagged for human review and added to the admin queue.
                      {!isAdmin && !canSwitch && <> An administrator other than you will review it. Run ID <code>{response.run_id}</code></>}
                      {!isAdmin && canSwitch && <> In this demo you play both roles: switch to the reviewer to approve or reject it.</>}
                    </p>
                    <div className="actions field-gap">
                      {isAdmin ? (
                        <button className="btn" onClick={() => setActiveTab("admin")}>
                          Go to admin queue
                        </button>
                      ) : (
                        <>
                          <button className="btn btn-primary" disabled={refreshing} onClick={() => handleRefreshStatus(response.run_id)}>
                            {refreshing ? "Checking…" : "Refresh status"}
                          </button>
                          {canSwitch && (
                            <button className="btn" onClick={() => handleSwitchRole("admin")}>
                              Review it as the reviewer
                            </button>
                          )}
                        </>
                      )}
                      {refreshNote && <span className="muted">{refreshNote}</span>}
                    </div>
                  </section>
                )}

                {status === "completed" && response && (
                  <section className="card enter">
                    <div className="card-head">
                      <h3 className="card-title">Answer</h3>
                      <div className="actions" style={{ gap: 16 }}>
                        {response.cached && <Verdict tone="info">cached · {response.latency_ms ?? 0.5} ms</Verdict>}
                        <Verdict tone="ok">allowed · AST validated · confidence {response.confidence_score ?? 100}%</Verdict>
                      </div>
                    </div>

                    {question}

                    {response.cached && response.cache_match_type === "semantic" && response.cache_original_query && (
                      <p className="muted small field-gap">
                        Reused the answer to a similar question, "{response.cache_original_query}" ({Math.round((response.cache_similarity ?? 0) * 100)}% match)
                      </p>
                    )}

                    {response.standalone_question && response.standalone_question !== submittedQuery && (
                      <div className="field">
                        <span className="label">Understood as</span>
                        <p>{response.standalone_question}</p>
                      </div>
                    )}

                    {response.agent_steps?.length > 0 && (
                      <details className="field muted small">
                        <summary style={{ cursor: "pointer" }}>The assistant looked at the database first ({response.agent_steps.length} step{response.agent_steps.length === 1 ? "" : "s"})</summary>
                        <ul style={{ margin: "8px 0 0", paddingLeft: 20 }}>
                          {response.agent_steps.map((step: any, i: number) => (
                            <li key={i}><code>{step.tool}({Object.values(step.args ?? {}).join(", ")})</code>: {step.result}</li>
                          ))}
                        </ul>
                      </details>
                    )}

                    <div className="field">
                      <span className="label">Executed SQL</span>
                      <pre className="code">{response.sql}</pre>
                    </div>
                    {response.results && <ResultsTable results={response.results} />}
                  </section>
                )}
              </div>

              {/* Beside the results on a wide screen (scrolls on its own); below them on a narrow one. */}
              <aside className="schema-aside">
                <SchemaPanel onAuthError={handleLogout} workspaceLabel={workspace?.label} />
              </aside>
            </div>
          )}
        </div>
      </main>
    </div>
  );
}
