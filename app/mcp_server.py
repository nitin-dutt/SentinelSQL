"""
SentinelSQL MCP Governance Proxy
---------------------------------
Model Context Protocol (MCP) server that acts as a zero-trust database
governance firewall for external frontier LLMs (e.g. Claude Desktop, Cursor, Devin).

Exposes tools:
1. get_database_schema(): Safely inspect database schema.
2. submit_sql_for_execution(sql, rationale): Intercepts, validates, masks PII, and routes high-risk queries to HITL queue.
3. check_query_status(run_id): Polls for execution results after human administrator approval.
"""

import os
import re
from typing import Dict, Any, Optional

from app.db.database import execute_readonly_query
from app.utils.schemacontext import get_schema_context
from app.hitl.web_store import save_run, get_run
from app.utils.audit_logger import log_audit_event

from app.utils.sql_parser import parse_and_validate_ast
from app.utils.logger import get_logger

logger = get_logger("sentinel.mcp")


def validate_and_classify_sql(sql: str) -> Dict[str, Any]:
    """
    Validates SQL safety and classifies risk level using deterministic AST parsing.
    """
    ast_res = parse_and_validate_ast(sql)
    return {
        "safe": ast_res.is_valid,
        "requires_human": ast_res.requires_human,
        "reason": ast_res.reason,
        "risk_level": ast_res.risk_level,
        "sanitized_sql": ast_res.sanitized_sql,
        "tables": ast_res.tables_accessed
    }



def mcp_get_database_schema() -> str:
    """
    Returns the schema of the enterprise database, including tables,
    columns, and foreign key relationships.
    """
    return get_schema_context()


DEFAULT_MCP_CLIENT_ID = os.getenv("MCP_CLIENT_ID", "mcp-stdio")


def mcp_submit_sql(
    sql: str,
    rationale: str = "",
    client_id: Optional[str] = None,
    role: str = "external_ai",
) -> Dict[str, Any]:
    """
    Submits a SQL query for governance inspection and execution.
    - Low-risk reads: Executed immediately; PII is dynamically masked.
    - High-risk / sensitive: Paused and placed into the Admin Approval Queue.
    """
    client_id = client_id or DEFAULT_MCP_CLIENT_ID
    classification = validate_and_classify_sql(sql)

    # 1. Rejected queries
    if not classification["safe"]:
        log_audit_event(
            user_id=client_id,
            role=role,
            user_query=f"[MCP] {rationale}" if rationale else "[MCP] SQL Execution",
            generated_sql=sql,
            confidence_score=0,
            execution_status="mcp_rejected",
            ingress_channel="MCP_AGENT",
            client_agent="external-mcp-client",
            risk_level="HIGH"
        )
        return {
            "status": "rejected",
            "sql": sql,
            "reason": classification["reason"]
        }

    # 2. Queries requiring Human-in-the-Loop approval
    sql_to_run = classification.get("sanitized_sql", sql)
    if classification["requires_human"]:
        state = {
            "user_query": f"[MCP Agent] {rationale}" if rationale else "[MCP Agent] Tool Execution Request",
            "sql": sql_to_run,
            "user_id": client_id,
            "role": role,
            "validation": {
                "safe": True,
                "requires_human": True,
                "reason": classification["reason"],
                "score": 75
            }
        }
        run_id = save_run(state, status="pending")

        log_audit_event(
            run_id=run_id,
            user_id=client_id,
            role=role,
            user_query=state["user_query"],
            generated_sql=sql_to_run,
            confidence_score=75,
            execution_status="requires_human",
            ingress_channel="MCP_AGENT",
            client_agent="external-mcp-client",
            risk_level=classification.get("risk_level", "MEDIUM")
        )

        return {
            "status": "requires_human",
            "run_id": run_id,
            "sql": sql_to_run,
            "reason": classification["reason"],
            "message": "Query paused for Admin review in SentinelSQL Dashboard. Call check_query_status(run_id) to retrieve data once approved."
        }

    # 3. Safe low-risk queries: Execute immediately with PII masking
    try:
        results = execute_readonly_query(sql_to_run)
        rows_count = len(results.get("rows", [])) if results and isinstance(results, dict) else 0
        log_audit_event(
            user_id=client_id,
            role=role,
            user_query=f"[MCP] {rationale}" if rationale else "[MCP] SQL Execution",
            generated_sql=sql_to_run,
            confidence_score=100,
            execution_status="completed",
            ingress_channel="MCP_AGENT",
            client_agent="external-mcp-client",
            risk_level="LOW",
            masked_rows_count=rows_count
        )
        return {
            "status": "completed",
            "sql": sql,
            "results": results
        }
    except Exception as e:
        log_audit_event(
            user_id=client_id,
            role=role,
            user_query=f"[MCP] {rationale}" if rationale else "[MCP] SQL Execution",
            generated_sql=sql,
            confidence_score=0,
            execution_status="mcp_failed",
            ingress_channel="MCP_AGENT",
            client_agent="external-mcp-client",
            risk_level="HIGH"
        )

        return {
            "status": "failed",
            "sql": sql,
            "reason": str(e)
        }


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
    from mcp.server.fastmcp import FastMCP
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
