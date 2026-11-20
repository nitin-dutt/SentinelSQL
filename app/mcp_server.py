"""
SentinelSQL MCP Governance Proxy
---------------------------------
Model Context Protocol (MCP) server that acts as a zero-trust database
governance firewall for external frontier LLMs (e.g. Claude Desktop, Cursor, Devin).

Exposes tools:
1. get_database_schema(): Safely inspect database schema.
2. submit_sql_for_execution(sql, rationale): Runs the SQL through the governed sequence (`run` in app/governance.py):
   policy verdict, review hold for high-risk queries, read-only execution with PII masking, one audit record.
3. check_query_status(run_id): Polls for execution results after human administrator approval.
"""

from typing import Dict, Any, Optional

from app.auth import Actor
from app.governance import BLOCKED, COMPLETED, HELD, run
from app.hitl.web_store import get_run
from app.observability.tracing import set_status, start_trace
from app.utils.logger import get_logger
from app.utils.schemacontext import get_schema_context
from app.settings import settings

logger = get_logger("sentinel.mcp")


def mcp_get_database_schema() -> str:
    """
    Returns the schema of the enterprise database, including tables,
    columns, and foreign key relationships.
    """
    return get_schema_context()


DEFAULT_MCP_CLIENT_ID = settings.access.mcp_client_id      # read once, when the server module is imported


def mcp_submit_sql(
    sql: str,
    rationale: str = "",
    client_id: Optional[str] = None,
    role: str = "external_ai",
) -> Dict[str, Any]:
    """Traced wrapper: one trace per submission (stages: gate, execution), then the governed submission below."""
    with start_trace("MCP_AGENT", client_id or DEFAULT_MCP_CLIENT_ID, role, rationale or "[MCP] SQL submission"):
        outcome = _submit_sql(sql=sql, rationale=rationale, client_id=client_id, role=role)
        set_status(outcome.get("status", "unknown"))
        return outcome


def _submit_sql(
    sql: str,
    rationale: str = "",
    client_id: Optional[str] = None,
    role: str = "external_ai",
) -> Dict[str, Any]:
    """
    Submits a SQL query for governance inspection and execution.
    - Low-risk reads: Executed immediately; PII is dynamically masked.
    - High-risk / sensitive: Paused and placed into the Admin Approval Queue.
    The sequence itself (verdict, hold, execution, masking, audit) is `run`; this function only words the reply.
    """
    outcome = run(
        sql,
        actor=Actor(user_id=client_id or DEFAULT_MCP_CLIENT_ID, role=role),
        channel="MCP_AGENT",
        client_agent="external-mcp-client",
        question=f"[MCP] {rationale}" if rationale else "[MCP] SQL Execution",
    )
    if outcome.status == COMPLETED:
        return {"status": "completed", "sql": outcome.sql, "results": outcome.results}   # the SQL that actually ran
    if outcome.status == HELD:
        return {
            "status": "requires_human",
            "run_id": outcome.run_id,
            "sql": outcome.sql,
            "reason": outcome.reason,
            "message": "Query paused for Admin review in SentinelSQL Dashboard. Call check_query_status(run_id) to retrieve data once approved."
        }
    if outcome.status == BLOCKED:
        return {"status": "rejected", "sql": sql, "reason": outcome.reason}
    return {"status": "failed", "sql": outcome.sql, "reason": outcome.reason}


def mcp_check_status(run_id: str, requester_id: Optional[str] = None, requester_role: str = "external_ai") -> Dict[str, Any]:
    """
    Checks the status of a paused SQL query in the Human-in-the-Loop queue.
    When `requester_id` is given, only the run's submitter (or an admin) may see it;
    anyone else gets the same answer as for a run that does not exist.
    """
    try:
        state = get_run(run_id)
        if requester_id is not None and requester_role != "admin" and state.get("user_id") != requester_id:
            raise KeyError(run_id)
        status = state.get("status", "pending")

        if status == "pending":
            return {
                "run_id": run_id,
                "status": "pending",
                "message": "Query is awaiting administrator approval in SentinelSQL Dashboard."
            }

        if status == "completed":
            return {
                "run_id": run_id,
                "status": "completed",
                "sql": state.get("sql"),
                "results": state.get("results")
            }

        if status == "rejected":
            return {
                "run_id": run_id,
                "status": "rejected",
                "reason": state.get("reason", "Query was rejected by human administrator.")
            }

        return {
            "run_id": run_id,
            "status": status,
            "reason": state.get("reason")
        }

    except KeyError:
        return {
            "run_id": run_id,
            "status": "not_found",
            "reason": "Run ID not found or expired."
        }


# ----------------------------------------------------
# FastMCP Integration (Model Context Protocol Standard)
# ----------------------------------------------------

try:
    try:
        from mcp.server.fastmcp import FastMCP            # mcp 1.x
    except ImportError:
        from mcp.server.mcpserver import MCPServer as FastMCP   # mcp 2.x renamed it; the decorator and run() are the same
    mcp_app = FastMCP("SentinelSQL Governance Proxy")

    @mcp_app.tool()
    def get_database_schema() -> str:
        """Inspect the enterprise database schema (tables, columns, relationships)."""
        return mcp_get_database_schema()

    @mcp_app.tool()
    def submit_sql_for_execution(sql: str, rationale: str = "") -> dict:
        """Submit a SQL query to SentinelSQL's zero-trust database governance engine."""
        return mcp_submit_sql(sql=sql, rationale=rationale)

    @mcp_app.tool()
    def check_query_status(run_id: str) -> dict:
        """Check status of a paused query in the SentinelSQL Admin Approval Queue."""
        return mcp_check_status(run_id=run_id)

    HAS_FASTMCP = True
except ImportError:
    HAS_FASTMCP = False
    mcp_app = None


if __name__ == "__main__":
    if HAS_FASTMCP and mcp_app is not None:
        logger.info("Starting SentinelSQL MCP Governance Proxy (STDIO transport)...")
        mcp_app.run(transport="stdio")
    else:
        logger.warning("FastMCP is not installed. Run 'pip install mcp' to enable MCP STDIO transport.")
