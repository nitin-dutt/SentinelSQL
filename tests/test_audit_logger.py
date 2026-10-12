import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.utils.audit_logger import log_audit_event, get_audit_logs
from tests.helpers import ADMIN, EMPLOYEE

client = TestClient(app)


def test_log_audit_event_and_retrieve():
    log_audit_event(
        run_id="audit-test-run-1",
        user_id="analyst_jane",
        role="analyst",
        user_query="Show top customers by revenue",
        generated_sql="SELECT name, region FROM customers LIMIT 10;",
        confidence_score=95,
        execution_status="completed",
        ingress_channel="WEB_UI",
        client_agent="sentinel-agent",
        risk_level="LOW",
        execution_time_ms=45.2,
        masked_rows_count=4
    )

    logs = get_audit_logs(limit=10)
    assert len(logs) > 0
    recent = logs[0]
    assert recent["user_id"] == "analyst_jane"
    assert recent["ingress_channel"] == "WEB_UI"
    assert recent["risk_level"] == "LOW"
    assert recent["execution_status"] == "completed"
    assert recent["execution_time_ms"] == 45.2
    assert recent["masked_rows_count"] == 4


def test_audit_logs_filtering():
    # Log an MCP rejected query
    log_audit_event(
        user_id="mcp_agent",
        role="external_ai",
        user_query="DROP TABLE secret;",
        generated_sql="DROP TABLE secret;",
        confidence_score=0,
        execution_status="mcp_rejected",
        ingress_channel="MCP_AGENT",
        client_agent="claude-3-5-sonnet",
        risk_level="HIGH"
    )

    # Log a web UI requires_human query
    log_audit_event(
        user_id="user_bob",
        role="manager",
        user_query="Review salaries",
        generated_sql="SELECT * FROM salaries;",
        confidence_score=75,
        execution_status="requires_human",
        ingress_channel="WEB_UI",
        client_agent="sentinel-agent",
        risk_level="HIGH"
    )

    # Filter by status
    mcp_rejected = get_audit_logs(status_filter="mcp_rejected")
    assert len(mcp_rejected) >= 1
    assert mcp_rejected[0]["execution_status"] == "mcp_rejected"

    # Filter by ingress
    mcp_logs = get_audit_logs(ingress_filter="MCP_AGENT")
    assert len(mcp_logs) >= 1
    assert all(l["ingress_channel"] == "MCP_AGENT" for l in mcp_logs)


def test_admin_audit_logs_endpoint_rbac():
    # 1. Non-admin request receives 403 Forbidden
    resp = client.get("/admin/audit-logs", headers=EMPLOYEE)
    assert resp.status_code == 403

    # 2. Admin request receives 200 OK with list of logs
    resp = client.get("/admin/audit-logs", headers=ADMIN)
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) > 0
