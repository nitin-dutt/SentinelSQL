from fastapi.testclient import TestClient
from unittest.mock import patch
from app.main import app
from tests.helpers import ADMIN, EMPLOYEE, pipeline_writing, use_pipeline

client = TestClient(app)


def test_query_endpoint_safe():
    with use_pipeline(pipeline_writing("SELECT name FROM customers LIMIT 10")):
        response = client.post("/query", json={"query": "Get customer names (api test)"}, headers=ADMIN)

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "completed"
    assert "Alice" in data["results"]["rows"][0]["name"]


def test_query_endpoint_requires_human():
    with use_pipeline(pipeline_writing("SELECT employee_name, salary FROM salaries LIMIT 10")):
        response = client.post("/query", json={"query": "Get payroll (api test)"}, headers=EMPLOYEE)

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "requires_human"
    assert data["run_id"] and "sensitive table" in data["reason"].lower()


def test_query_endpoint_rejected():
    with use_pipeline(pipeline_writing("DROP TABLE customers")):
        response = client.post("/query", json={"query": "remove the customers table (api test)"}, headers=EMPLOYEE)

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "rejected"
    assert "Destructive" in data["reason"]


@patch("app.routers.hitl.get_run")
@patch("app.governance.execute_readonly_query_for_review")
def test_hitl_decision_approve(mock_execute, mock_get_run):
    # Mock the stored run
    mock_get_run.return_value = {
        "user_query": "Get payroll",
        "sql": "SELECT id, salary FROM payroll LIMIT 10"
    }
    
    # Mock the DB execution result
    view = {"columns": ["id", "salary"], "rows": [{"id": 1, "salary": 5000}]}
    mock_execute.return_value = (view, view)        # (the reviewer's view, the submitter's view)
    
    response = client.post(
        "/hitl/decision",
        json={"run_id": "test-run-id", "decision": "approve"},
        headers=ADMIN
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "completed"
    assert data["results"]["rows"][0]["salary"] == 5000

