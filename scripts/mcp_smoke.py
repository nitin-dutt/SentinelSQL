"""
End-to-end check of the MCP server over its real stdio transport, the way Claude Desktop or the MCP Inspector talks to it.

    python scripts/mcp_smoke.py

It starts `python -m app.mcp_server` as a child process against a throwaway database, audit log and review queue (your .env data is
never touched, and no language-model calls are made), then lists the tools and calls each one. The review step uses the HTTP API,
because a human reviewer approves held queries there. Exit code 0 means every check passed.
"""
import asyncio
import json
import os
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

WORK = tempfile.mkdtemp(prefix="sentinel-mcp-")
ENV = {
    "DATABASE_URL": "sqlite:///" + os.path.join(WORK, "demo.db").replace(os.sep, "/"),
    "ACTIVE_DB": "",
    "READONLY_DATABASE_URL": "",
    "SEED_DEMO_DATA": "true",
    "AUDIT_DB_PATH": os.path.join(WORK, "audit.db"),
    "HITL_DB_PATH": os.path.join(WORK, "hitl_runs.db"),
    "AUTH_TOKENS": "smoke-reviewer:smoke-reviewer:admin",
    "EMBEDDING_PROVIDER": "offline",
    "MCP_CLIENT_ID": "smoke-agent",
    "PYTHONPATH": ROOT,
}
os.environ.update(ENV)

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

failures = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({detail})" if detail and not ok else ""))
    if not ok:
        failures.append(name)


def payload(result) -> dict:
    """A tool result as a dict (FastMCP returns JSON text for dict results)."""
    if getattr(result, "structuredContent", None):
        data = result.structuredContent
        return data.get("result", data) if isinstance(data, dict) else data
    text = "".join(getattr(c, "text", "") for c in result.content)
    try:
        return json.loads(text)
    except ValueError:
        return {"text": text}


async def main() -> int:
    from app.db.database import init_db
    init_db()  # the child process uses the same database file, so seed it first

    params = StdioServerParameters(command=sys.executable, args=["-X", "utf8", "-m", "app.mcp_server"], env={**os.environ, **ENV}, cwd=WORK)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            print("Tools")
            tools = {t.name for t in (await session.list_tools()).tools}
            check("the three governance tools are offered", {"get_database_schema", "submit_sql_for_execution", "check_query_status"} <= tools, str(tools))

            print("Schema")
            schema = (await session.call_tool("get_database_schema", {})).content[0].text
            check("the schema lists the demo tables", "customers" in schema and "orders" in schema)

            print("Governance")
            bad = payload(await session.call_tool("submit_sql_for_execution", {"sql": "SELECT * FROM customers", "rationale": "smoke"}))
            check("SELECT * is rejected with an explanation", bad.get("status") == "rejected" and bool(bad.get("reason")), str(bad)[:160])

            write_attempt = payload(await session.call_tool("submit_sql_for_execution", {"sql": "DELETE FROM orders", "rationale": "smoke"}))
            check("a DELETE is rejected", write_attempt.get("status") == "rejected", str(write_attempt)[:160])

            good = payload(await session.call_tool("submit_sql_for_execution", {"sql": "SELECT name, email FROM customers LIMIT 3", "rationale": "smoke"}))
            rows = (good.get("results") or {}).get("rows") or []
            check("a safe query runs", good.get("status") == "completed" and len(rows) == 3, str(good)[:160])
            check("emails come back masked", bool(rows) and all("*" in str(r.get("email")) for r in rows), str(rows)[:160])

            held = payload(await session.call_tool("submit_sql_for_execution", {"sql": "SELECT salary FROM salaries", "rationale": "smoke"}))
            run_id = held.get("run_id")
            check("a sensitive-table query is held for review", held.get("status") == "requires_human" and bool(run_id), str(held)[:160])

            print("Human review")
            pending = payload(await session.call_tool("check_query_status", {"run_id": run_id or "missing"}))
            check("the held query reports pending", pending.get("status") in ("pending", "requires_human"), str(pending)[:160])

            from fastapi.testclient import TestClient
            from app.main import app
            reviewer = {"Authorization": "Bearer smoke-reviewer"}
            approved = TestClient(app).post("/hitl/decision", json={"run_id": run_id, "decision": "approve"}, headers=reviewer)
            check("a reviewer approves it over HTTP", approved.status_code == 200 and approved.json().get("status") == "completed", approved.text[:160])

            done = payload(await session.call_tool("check_query_status", {"run_id": run_id}))
            check("the agent then sees it completed", done.get("status") == "completed", str(done)[:160])
            check("and the salary is masked for the agent", "[CONFIDENTIAL]" in json.dumps(done), str(done)[:200])

            print("Audit")
            logs = TestClient(app).get("/admin/audit-logs?limit=50", headers=reviewer).json()
            logs = logs if isinstance(logs, list) else logs.get("logs", [])
            mine = [entry for entry in logs if entry.get("user_id") == "smoke-agent"]
            check("the agent's calls are in the audit log under its own identity", len(mine) >= 3, f"{len(mine)} entries")
            statuses = {entry.get("execution_status") for entry in mine}
            print("        statuses seen:", ", ".join(sorted(str(s) for s in statuses)))

    print(f"\n{'ALL CHECKS PASSED' if not failures else 'FAILED: ' + ', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
