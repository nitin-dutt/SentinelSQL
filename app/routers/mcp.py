"""HTTP/REST bridge for external AI agents (MCP governance). Identity comes from the caller's token."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.auth import Actor, require_roles, ROLE_ADMIN, ROLE_EXTERNAL_AI
from app.mcp_server import mcp_get_database_schema, mcp_submit_sql, mcp_check_status

router = APIRouter(prefix="/mcp")
_mcp_callers = require_roles(ROLE_EXTERNAL_AI, ROLE_ADMIN)


class MCPSubmitRequest(BaseModel):
    sql: str
    rationale: str = ""


@router.get("/schema")
def mcp_schema_endpoint(actor: Actor = Depends(_mcp_callers)):
    """Exposes database schema for external AI assistants."""
    return {"schema": mcp_get_database_schema()}


@router.post("/submit")
def mcp_submit_endpoint(req: MCPSubmitRequest, actor: Actor = Depends(_mcp_callers)):
    """Intercepts and governs external AI SQL execution. The caller's token identity is recorded in the audit log."""
    return mcp_submit_sql(sql=req.sql, rationale=req.rationale, client_id=actor.user_id, role=actor.role)


@router.get("/status/{run_id}")
def mcp_status_endpoint(run_id: str, actor: Actor = Depends(_mcp_callers)):
    """Allows the submitting client (or an admin) to poll the status of a query paused in the Admin Queue."""
    return mcp_check_status(run_id=run_id, requester_id=actor.user_id, requester_role=actor.role)
