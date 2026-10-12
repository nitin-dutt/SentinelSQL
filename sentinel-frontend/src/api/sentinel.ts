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
    throw new Error("Backend error while submitting query");
  }

  return res.json();
}

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
    throw new Error("Backend error while submitting HITL decision");
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



