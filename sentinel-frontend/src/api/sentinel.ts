export type QueryResponse =
  | {
      status: "completed";
      sql: string;
      results: any[];
    }
  | {
      status: "requires_human";
      run_id: string;
      sql: string;
      reason: string;
    }
  | {
      status: "rejected";
      reason: string;
    };

export async function submitQuery(query: string): Promise<QueryResponse> {
  const res = await fetch("/query", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query }),
  });

  if (!res.ok) {
    throw new Error("Backend error while submitting query");
  }

  return res.json();
}

export async function submitDecision(payload: {
  run_id: string;
  decision: "approve" | "edit" | "reject";
  edited_sql?: string;
}) {
  const res = await fetch("/hitl/decision", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

  if (!res.ok) {
    throw new Error("Backend error while submitting HITL decision");
  }

  return res.json();
}
