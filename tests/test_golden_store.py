"""
SentinelSQL Golden Query Memory & Few-Shot Store Test Suite
-----------------------------------------------------------
Tests:
1. save_golden_query, get_all_golden_queries, delete_golden_query persistence.
2. Semantic similarity matching and few-shot prompt formatting.
3. Admin endpoints: GET /admin/golden-queries, DELETE /admin/golden-queries/{id}.
4. HITL decision with save_as_template flag (opt-in validation).
"""

import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient
from app.main import app
from app.rag.golden_store import (
    save_golden_query,
    get_all_golden_queries,
    delete_golden_query,
    find_matching_golden_queries,
    format_few_shot_examples,
)
from tests.helpers import ADMIN, EMPLOYEE

client = TestClient(app)


def test_save_and_retrieve_golden_query():
    nl_query = "List top 5 customers by total order amount"
    sql = "SELECT c.id, c.name, SUM(o.total_amount) as total FROM customers c JOIN orders o ON c.id = o.customer_id GROUP BY c.id ORDER BY total DESC LIMIT 5;"
    
    query_id = save_golden_query(nl_query=nl_query, sql=sql, created_by="lead_dba")
    assert query_id > 0

    all_queries = get_all_golden_queries()
    saved = next((q for q in all_queries if q["id"] == query_id), None)
    assert saved is not None
    assert saved["natural_language_query"] == nl_query
    assert saved["verified_sql"] == sql
    assert saved["created_by"] == "lead_dba"

    # Clean up
    delete_golden_query(query_id)


def test_delete_golden_query():
    nl_query = "Temporary test template to delete"
    sql = "SELECT 1;"
    query_id = save_golden_query(nl_query=nl_query, sql=sql, created_by="test_user")

    assert delete_golden_query(query_id) is True
    all_queries = get_all_golden_queries()
    assert not any(q["id"] == query_id for q in all_queries)

    # Deleting non-existent ID should return False
    assert delete_golden_query(999999) is False


def test_few_shot_formatting():
    nl_query = "Show customer support tickets categorized by priority"
    sql = "SELECT priority, COUNT(*) FROM tickets GROUP BY priority;"
    query_id = save_golden_query(nl_query=nl_query, sql=sql, created_by="support_lead")

    try:
        few_shot = format_few_shot_examples("support tickets priority breakdown", top_k=2)
        assert "Verified Enterprise Query Examples:" in few_shot
        assert "Show customer support tickets categorized by priority" in few_shot
        assert "SELECT priority, COUNT(*)" in few_shot
    finally:
        delete_golden_query(query_id)


def test_admin_golden_queries_endpoints_rbac():
    # Non-admin should be forbidden
    res = client.get("/admin/golden-queries", headers=EMPLOYEE)
    assert res.status_code == 403

    res_del = client.delete("/admin/golden-queries/1", headers=EMPLOYEE)
    assert res_del.status_code == 403

    # Admin should succeed
    res_admin = client.get("/admin/golden-queries", headers=ADMIN)
    assert res_admin.status_code == 200
    assert isinstance(res_admin.json(), list)


@patch("app.routers.hitl.get_run")
@patch("app.routers.hitl.execute_readonly_query")
def test_hitl_decision_save_as_template(mock_execute, mock_get_run):
    mock_get_run.return_value = {
        "user_query": "What is the average transaction value by customer?",
        "sql": "SELECT customer_id, AVG(total_amount) FROM orders GROUP BY customer_id LIMIT 10"
    }
    mock_execute.return_value = {
        "columns": ["customer_id", "avg_val"],
        "rows": [{"customer_id": 1, "avg_val": 42.5}]
    }

    # 1. HITL Decision with save_as_template = False (should NOT save to golden store)
    res_no_save = client.post(
        "/hitl/decision",
        json={"run_id": "run-optout-1", "decision": "approve", "save_as_template": False},
        headers=ADMIN
    )
    assert res_no_save.status_code == 200
    all_templates_before = get_all_golden_queries()
    assert not any(t["natural_language_query"] == "What is the average transaction value by customer?" for t in all_templates_before)

    # 2. HITL Decision with save_as_template = True (Explicit Opt-In: Approach 1)
    res_save = client.post(
        "/hitl/decision",
        json={"run_id": "run-optin-1", "decision": "approve", "save_as_template": True},
        headers=ADMIN
    )
    assert res_save.status_code == 200
    all_templates_after = get_all_golden_queries()
    saved_template = next((t for t in all_templates_after if t["natural_language_query"] == "What is the average transaction value by customer?"), None)
    assert saved_template is not None
    assert saved_template["created_by"] == "admin-1"  # the approving admin, taken from the token
    assert "AVG(total_amount)" in saved_template["verified_sql"]

    # Clean up
    delete_golden_query(saved_template["id"])
