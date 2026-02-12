/**
 * Enterprise API response types
 * Mirrors backend response structure
 */

export type AppStatus =
  | "idle"
  | "loading"
  | "rejected"
  | "requires_human"
  | "completed";

export interface SentinelResponse {
  status: AppStatus;
  run_id: string;
  sql: string;
  reason?: string;
  results?: any[];
}

export interface ApprovalDecision {
  run_id: string;
  decision: "approve" | "edit" | "reject";
  edited_sql?: string;
}
