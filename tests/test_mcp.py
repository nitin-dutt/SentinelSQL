import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch

from app.main import app
from app.mcp_server import (
    validate_and_classify_sql,
    mcp_get_database_schema,
    mcp_submit_sql,
    mcp_check_status
)
from app.db.database import init_db
from app.hitl.web_store import clear_runs
from tests.helpers import ADMIN, MCP

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_run_store():
    """Ensure database has tables and sample data, and clear in-memory store before each test."""
    init_db()
    clear_runs()
    yield
    clear_runs()



# ==========================================
# 1. Schema Inspection Tests
# ==========================================

def test_mcp_get_database_schema():
    schema = mcp_get_database_schema()
    assert "customers" in schema
    assert "orders" in schema
    assert "products" in schema


def test_mcp_schema_endpoint():
    response = client.get("/mcp/schema", headers=MCP)
    assert response.status_code == 200
    data = response.json()
    assert "customers" in data["schema"]


# ==========================================
# 2. SQL Validation & Risk Classification Tests
# ==========================================

def test_classify_safe_query():
    res = validate_and_classify_sql("SELECT name, price FROM products LIMIT 10;")
    assert res["safe"] is True
    assert res["requires_human"] is False


def test_classify_destructive_drop_blocked():
    res = validate_and_classify_sql("DROP TABLE customers;")
    assert res["safe"] is False
    assert "Destructive SQL operation detected: 'DROP'" in res["reason"]


def test_classify_destructive_delete_blocked():
    res = validate_and_classify_sql("DELETE FROM orders WHERE order_id = 1;")
    assert res["safe"] is False


def test_classify_stacked_statements_blocked():
    res = validate_and_classify_sql("SELECT 1; DROP TABLE products;")
    assert res["safe"] is False
    assert "Multiple stacked SQL statements detected" in res["reason"]


def test_classify_sensitive_table_triggers_hitl():
    res = validate_and_classify_sql("SELECT employee_id, salary FROM salaries LIMIT 10;")
    assert res["safe"] is True
    assert res["requires_human"] is True
    assert "sensitive table" in res["reason"].lower()


def test_classify_missing_limit_is_injected_and_auto_executes():
    res = validate_and_classify_sql("SELECT name, price FROM products;")
    assert res["safe"] is True
    assert res["requires_human"] is False
    assert "LIMIT 100" in res["sanitized_sql"].upper()


# ==========================================
# 3. Submission & Execution Flow Tests
# ==========================================

def test_mcp_submit_safe_query_auto_executes():
    # Submit safe query via REST bridge
    payload = {
        "sql": "SELECT name, price FROM products LIMIT 2;",
        "rationale": "Fetch top 2 product prices"
    }
    response = client.post("/mcp/submit", json=payload, headers=MCP)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "completed"
    assert "results" in data
    assert len(data["results"]["rows"]) > 0


def test_mcp_submit_destructive_query_rejected():
    payload = {
        "sql": "DROP TABLE customers;",
        "rationale": "Testing injection"
    }
    response = client.post("/mcp/submit", json=payload, headers=MCP)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "rejected"
    assert "Destructive SQL operation detected" in data["reason"]


def test_mcp_submit_sensitive_query_pauses_for_hitl():
    payload = {
        "sql": "SELECT employee_name, salary FROM salaries LIMIT 5;",
        "rationale": "Executive payroll review"
    }
    response = client.post("/mcp/submit", json=payload, headers=MCP)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "requires_human"
    assert "run_id" in data
    run_id = data["run_id"]

    # Poll status immediately -> should be pending
    status_resp = client.get(f"/mcp/status/{run_id}", headers=MCP)
    assert status_resp.status_code == 200
    status_data = status_resp.json()
    assert status_data["status"] == "pending"


def test_mcp_hitl_approval_and_polling_lifecycle():
    # 1. Submit a high-risk query (sensitive table triggers HITL)
    payload = {
        "sql": "SELECT employee_name, department FROM salaries;",
        "rationale": "Staff directory sync"
    }
    submit_resp = client.post("/mcp/submit", json=payload, headers=MCP)
    run_id = submit_resp.json()["run_id"]

    # 2. Check Admin Queue has this pending run
    admin_queue_resp = client.get("/admin/queue", headers=ADMIN)
    assert admin_queue_resp.status_code == 200
    queue_items = admin_queue_resp.json()
    assert any(item["run_id"] == run_id for item in queue_items)

    # 3. Admin approves query via /hitl/decision
    decision_resp = client.post(
        "/hitl/decision",
        json={"run_id": run_id, "decision": "approve"},
        headers=ADMIN
    )
    assert decision_resp.status_code == 200
    assert decision_resp.json()["status"] == "completed"

    # 4. MCP client polls /mcp/status/{run_id} -> now receives completed results with PII masked
    poll_resp = client.get(f"/mcp/status/{run_id}", headers=MCP)
    assert poll_resp.status_code == 200
    poll_data = poll_resp.json()
    assert poll_data["status"] == "completed"
    assert "results" in poll_data
    # Verify emails are masked
    rows = poll_data["results"]["rows"]
    for row in rows:
        email = row.get("email")
        if email:
            assert "*" in email and email not in ["alice@example.com", "bob@example.com", "charlie@example.com", "diana@example.com"], f"Email was not masked: {email}"

