const TOKEN_KEY = "sentinel.token";

export interface Session {
  user_id: string;
  role: "employee" | "admin" | "external_ai";
}

export function getToken(): string | null {
  try {
    return sessionStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string | null): void {
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage unavailable: session will not persist across reloads */
  }
}

/** Thrown when the server rejects the session token; the UI responds by returning to the login screen. */
export class AuthError extends Error {
  constructor() {
    super("Your session has expired or the token is invalid");
    this.name = "AuthError";
  }
}

function authHeaders(): Record<string, string> {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

/** Validates the stored token with the server and returns who it belongs to. */
export async function fetchSession(): Promise<Session | null> {
  if (!getToken()) return null;
  const res = await fetch("/auth/me", { headers: authHeaders() });
  if (!res.ok) return null;
  return res.json();
}

/** Tries a token; stores it only if the server accepts it. */
export async function login(token: string): Promise<Session | null> {
  setToken(token.trim());
  const session = await fetchSession();
  if (!session) setToken(null);
  return session;
}

export type QueryResponse =
  | {
      status: "completed";
      sql: string;
      results: any[];
      confidence_score?: number;
      cached?: boolean;
      latency_ms?: number;
      cache_match_type?: "exact" | "semantic";
      cache_similarity?: number;
      cache_original_query?: string;
    }
  | {
      status: "requires_human";
      run_id: string;
      sql: string;
      reason: string;
      confidence_score?: number;
    }
  | {
      status: "rejected" | "error";
      reason: string;
      confidence_score?: number;
    };

export async function submitQuery(query: string): Promise<QueryResponse> {
  const res = await fetch("/query", {
    method: "POST",
    headers: { 
      "Content-Type": "application/json",
      ...authHeaders()
    },
    body: JSON.stringify({ query }),
  });

  if (res.status === 401) {
    throw new AuthError();
  }
  if (res.status === 403) {
    throw new Error("Your role is not allowed to run queries");
  }
  if (!res.ok) {
    // The demo's limits (429 rate limit or daily budget, 422 too long) explain themselves in `detail`; show that text.
    const body = await res.json().catch(() => null);
    throw new Error(typeof body?.detail === "string" ? body.detail : "Backend error while submitting query");
  }

  return res.json();
}

export interface AgentStep {
  tool: string;
  args: Record<string, string>;
  result: string;
}

/** A /chat answer: a /query answer plus the conversation it belongs to and, sometimes, a clarifying question. */
export type ChatResponse = {
  status: "completed" | "requires_human" | "rejected" | "error" | "needs_clarification";
  conversation_id: string;
  standalone_question?: string | null;
  clarification?: { question: string; options: string[] } | null;
  agent_steps?: AgentStep[];
  sql?: string | null;
  results?: any;
  run_id?: string | null;
  reason?: string | null;
  confidence_score?: number | null;
  cached?: boolean;
  latency_ms?: number | null;
  cache_match_type?: "exact" | "semantic" | null;
  cache_similarity?: number | null;
  cache_original_query?: string | null;
  trace_id?: string | null;
};

/** Sends one message of a conversation. Without `conversationId` the server starts a new conversation. */
export async function submitChat(message: string, conversationId?: string | null): Promise<ChatResponse> {
  const res = await fetch("/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(conversationId ? { message, conversation_id: conversationId } : { message }),
  });
  if (res.status === 401) throw new AuthError();
  if (res.status === 403) throw new Error("Your role is not allowed to run queries");
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new Error(typeof body?.detail === "string" ? body.detail : "Backend error while submitting the question");
  }
  return res.json();
}

export interface LatencyDist {
  n: number;
  p50_ms: number | null;
  p95_ms: number | null;
  mean_ms: number | null;
}

export interface ObservabilitySummary {
  enabled: boolean;
  window_hours: number;
  requests: number;
  pipeline_requests: number;
  cache_hits: number;
  cache_hit_pct: number | null;
  statuses: Record<string, number>;
  total_ms: LatencyDist;
  model_ms: LatencyDist;
  gateway_ms: LatencyDist;
  cache_hit_ms: LatencyDist;
  stages: (LatencyDist & { stage: string; kind: "model" | "gateway"; share_of_requests_pct: number })[];
  models: { model: string; calls: number; input_tokens: number; output_tokens: number; p50_ms: number | null; p95_ms: number | null; cost_usd: number | null }[];
  model_calls_per_request: number | null;
  tokens_per_request: { input: number; output: number } | null;
  tokens_estimated: boolean;
  cost_per_1000_requests_usd: number | null;
  price_configured: boolean;
}

export interface TraceRow {
  trace_id: string;
  ts: number;
  channel: string;
  user_id: string | null;
  question: string | null;
  status: string;
  total_ms: number;
  model_ms: number;
  model_calls: number;
  input_tokens: number;
  output_tokens: number;
  tokens_estimated: boolean;
  cost_usd: number | null;
  stages_ms?: Record<string, number>;
}

export interface TraceDetail extends TraceRow {
  spans: { name: string; start_ms: number; ms: number; depth: number; error?: string }[];
  calls: { stage: string; model: string; ms: number; input_tokens: number; output_tokens: number; estimated: boolean; ok: boolean; cost_usd: number | null }[];
  attrs: Record<string, unknown>;
}

async function adminGet<T>(path: string, failure: string): Promise<T> {
  const res = await fetch(path, { headers: authHeaders() });
  if (res.status === 401) throw new AuthError();
  if (!res.ok) throw new Error(failure);
  return res.json();
}

export const getObservabilitySummary = (hours: number) =>
  adminGet<ObservabilitySummary>(`/admin/observability/summary?hours=${hours}`, "Could not load the observability summary");
export const getTraces = (limit = 30) => adminGet<TraceRow[]>(`/admin/observability/traces?limit=${limit}`, "Could not load traces");
export const getTrace = (id: string) =>
  adminGet<TraceDetail>(`/admin/observability/traces/${encodeURIComponent(id)}`, "Could not load the trace");

export async function submitDecision(payload: {
  run_id: string;
  decision: "approve" | "edit" | "reject";
  edited_sql?: string;
  save_as_template?: boolean;
}) {
  const res = await fetch("/hitl/decision", {
    method: "POST",
    headers: { 
      "Content-Type": "application/json",
      ...authHeaders()
    },
    body: JSON.stringify(payload),
  });

  if (res.status === 401) {
    throw new AuthError();
  }
  if (res.status === 403) {
    // e.g. self-approval is blocked: surface the server's explanation
    const body = await res.json().catch(() => null);
    throw new Error(body?.detail ?? "You are not allowed to decide this request");
  }
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new Error(typeof body?.detail === "string" ? body.detail : "Backend error while submitting HITL decision");
  }

  return res.json();
}

export async function getAdminQueue(): Promise<any[]> {
  const res = await fetch("/admin/queue", {
    method: "GET",
    headers: {
      "Content-Type": "application/json",
      ...authHeaders()
    }
  });

  if (!res.ok) {
    throw new Error("Failed to fetch admin queue");
  }

  return res.json();
}

export interface AuditLogEntry {
  id: number;
  run_id?: string;
  user_id: string;
  role: string;
  natural_language_query: string;
  generated_sql: string;
  confidence_score?: number;
  execution_status: string;
  ingress_channel: string;
  client_agent: string;
  risk_level: string;
  approved_by?: string;
  execution_time_ms?: number;
  masked_rows_count?: number;
  timestamp: string;
}

export async function getAuditLogs(params?: {
  limit?: number;
  status?: string;
  ingress?: string;
}): Promise<AuditLogEntry[]> {
  const query = new URLSearchParams();
  if (params?.limit) query.append("limit", params.limit.toString());
  if (params?.status) query.append("status", params.status);
  if (params?.ingress) query.append("ingress", params.ingress);

  const res = await fetch(`/admin/audit-logs?${query.toString()}`, {
    method: "GET",
    headers: {
      "Content-Type": "application/json",
      ...authHeaders()
    }
  });

  if (!res.ok) {
    throw new Error("Failed to fetch audit logs");
  }

  return res.json();
}

export async function clearAuditLogs(): Promise<{ status: string; deleted_count: number; message: string }> {
  const res = await fetch("/admin/audit-logs", {
    method: "DELETE",
    headers: {
      "Content-Type": "application/json",
      ...authHeaders()
    }
  });

  if (!res.ok) {
    throw new Error("Failed to clear audit logs");
  }

  return res.json();
}

export interface GoldenQuery {
  id: number;
  natural_language_query: string;
  verified_sql: string;
  created_by: string;
  created_at: string;
}

export async function getGoldenQueries(): Promise<GoldenQuery[]> {
  const res = await fetch("/admin/golden-queries", {
    method: "GET",
    headers: {
      "Content-Type": "application/json",
      ...authHeaders()
    }
  });

  if (!res.ok) {
    throw new Error("Failed to fetch golden query templates");
  }

  return res.json();
}

export async function deleteGoldenQuery(id: number): Promise<{ status: string; message: string }> {
  const res = await fetch(`/admin/golden-queries/${id}`, {
    method: "DELETE",
    headers: {
      "Content-Type": "application/json",
      ...authHeaders()
    }
  });

  if (!res.ok) {
    throw new Error("Failed to delete golden query template");
  }

  return res.json();
}

export interface CacheStats {
  enabled: boolean;
  total_entries: number;
  hits: number;
  exact_hits: number;
  semantic_hits: number;
  misses: number;
  hit_ratio_percent: number;
  threshold: number;
  default_ttl_seconds: number;
  max_entries: number;
}

export async function getCacheStats(): Promise<CacheStats> {
  const res = await fetch("/admin/cache/stats", {
    method: "GET",
    headers: {
      "Content-Type": "application/json",
      ...authHeaders()
    }
  });

  if (!res.ok) {
    throw new Error("Failed to fetch cache statistics");
  }

  return res.json();
}

export async function clearCache(): Promise<{ status: string; cleared_count: number; message: string }> {
  const res = await fetch("/admin/cache", {
    method: "DELETE",
    headers: {
      "Content-Type": "application/json",
      ...authHeaders()
    }
  });

  if (!res.ok) {
    throw new Error("Failed to clear cache");
  }

  return res.json();
}

export interface DictionaryTerm {
  id: number;
  term: string;
  definition: string;
  formula_or_rule: string;
  related_tables?: string;
  category?: string;
  created_by?: string;
  created_at?: string;
}

export async function getDictionaryTerms(): Promise<DictionaryTerm[]> {
  const res = await fetch("/dictionary", {
    method: "GET",
    headers: {
      "Content-Type": "application/json",
      ...authHeaders()
    }
  });

  if (!res.ok) {
    throw new Error("Failed to fetch dictionary terms");
  }

  return res.json();
}

export async function addDictionaryTerm(term: {
  term: string;
  definition: string;
  formula_or_rule: string;
  related_tables?: string;
  category?: string;
}): Promise<{ status: string; id: number; message: string }> {
  const res = await fetch("/admin/dictionary", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...authHeaders()
    },
    body: JSON.stringify(term)
  });

  if (!res.ok) {
    throw new Error("Failed to save dictionary term");
  }

  return res.json();
}

export async function deleteDictionaryTerm(id: number): Promise<{ status: string; message: string }> {
  const res = await fetch(`/admin/dictionary/${id}`, {
    method: "DELETE",
    headers: {
      "Content-Type": "application/json",
      ...authHeaders()
    }
  });

  if (!res.ok) {
    throw new Error("Failed to delete dictionary term");
  }

  return res.json();
}




export interface SchemaTable {
  name: string;
  description: string;
  sensitive: boolean;
  columns: { name: string; type: string; primary_key: boolean }[];
  foreign_keys: string[];
}

/** Tables, columns and sensitivity flags for the structure panel. */
export async function fetchSchemaOverview(): Promise<SchemaTable[]> {
  const res = await fetch("/schema/overview", { headers: authHeaders() });
  if (res.status === 401) throw new AuthError();
  if (!res.ok) throw new Error("Could not load the schema");
  return (await res.json()).tables;
}

export interface RunStatus {
  run_id: string;
  status: "pending" | "completed" | "rejected" | "failed" | string;
  sql?: string | null;
  results?: any;
  reason?: string | null;
}

/** Current state of a request that was held for human review. */
export async function fetchRunStatus(runId: string): Promise<RunStatus> {
  const res = await fetch(`/query/status/${encodeURIComponent(runId)}`, { headers: authHeaders() });
  if (res.status === 401) throw new AuthError();
  if (res.status === 404) throw new Error("This request was not found. It may have expired.");
  if (!res.ok) throw new Error("Could not check the request status");
  return res.json();
}

export interface ChainVerification {
  valid: boolean;
  checked: number;
  first_invalid_id?: number;
  reason?: string;
}

/** Recomputes the audit hash chain on the server and reports whether it is intact. */
export async function verifyAuditChain(): Promise<ChainVerification> {
  const res = await fetch("/admin/audit-logs/verify", { headers: authHeaders() });
  if (res.status === 401) throw new AuthError();
  if (!res.ok) throw new Error("Could not verify the audit chain");
  return res.json();
}

// ---------------------------------------------------------------------------------------------
// Launch page: entering the sample database without a token, and connecting your own database.

const ROLES_KEY = "sentinel.roles";

export type Role = "employee" | "admin" | "external_ai";
/** The session tokens the launch page was given, by role. One person plays every role in a demo or a workspace. */
export type RoleTokens = Partial<Record<Role, string>>;

export function getRoleTokens(): RoleTokens {
  try {
    return JSON.parse(sessionStorage.getItem(ROLES_KEY) || "{}");
  } catch {
    return {};
  }
}

export function setRoleTokens(tokens: RoleTokens | null): void {
  try {
    if (tokens) sessionStorage.setItem(ROLES_KEY, JSON.stringify(tokens));
    else sessionStorage.removeItem(ROLES_KEY);
  } catch {
    /* storage unavailable: the role switch will not survive a reload */
  }
}

export interface WorkspaceInfo {
  id: string;
  label: string;
  dialect: string;
  tables: number;
  questions_used: number;
  question_quota: number;
  idle_minutes_allowed: number;
}

export interface LaunchInfo {
  mode: "dev" | "demo" | "standard";
  sample: { one_click: boolean; roles: Role[]; dialect?: string; tables?: number };
  connect: { enabled: boolean; question_quota: number; idle_minutes: number; max_tables: number; private_hosts: boolean };
  examples: string[];
  limits: null | { rate_per_minute: number; daily_cap: number; remaining_today: number; max_question_chars: number; notice: string };
}

export async function fetchLaunchInfo(): Promise<LaunchInfo | null> {
  try {
    const res = await fetch("/launch/info");
    return res.ok ? await res.json() : null;
  } catch {
    return null;
  }
}

async function detail(res: Response, fallback: string): Promise<string> {
  const body = await res.json().catch(() => null);
  return typeof body?.detail === "string" ? body.detail : fallback;
}

/** Enters the sample database as `role` without typing a token (dev and demo servers only). */
export async function enterSample(role: Role): Promise<Session | null> {
  const res = await fetch("/launch/sample", { method: "POST" });
  if (!res.ok) throw new Error(await detail(res, "This server requires an access token"));
  const tokens: RoleTokens = (await res.json()).tokens;
  const token = tokens[role];
  if (!token) throw new Error("That role is not offered on this server");
  setRoleTokens(tokens);
  return login(token);
}

/** Connects the visitor's own database and enters its workspace as the tester. */
export async function connectDatabase(url: string): Promise<Session | null> {
  const res = await fetch("/launch/connect", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url }),
  });
  if (!res.ok) throw new Error(await detail(res, "Could not connect to the database"));
  const tokens: RoleTokens = (await res.json()).tokens;
  setRoleTokens(tokens);
  return login(tokens.employee ?? "");
}

/** Becomes another role of the same demo or workspace. */
export async function switchRole(role: Role): Promise<Session | null> {
  const token = getRoleTokens()[role];
  if (!token) return null;
  setToken(token);
  return fetchSession();
}

/** The workspace this session belongs to, or null on the server's own database. */
export async function fetchWorkspace(): Promise<WorkspaceInfo | null> {
  const res = await fetch("/launch/session", { headers: authHeaders() });
  if (!res.ok) return null;
  return (await res.json()).workspace ?? null;
}

/** Closes the visitor's workspace now: the server drops the connection, the tokens and every file kept for it. */
export async function disconnectWorkspace(): Promise<void> {
  await fetch("/launch/workspace", { method: "DELETE", headers: authHeaders() }).catch(() => undefined);
}
