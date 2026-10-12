from fastapi.testclient import TestClient
from unittest.mock import patch
from app.main import app
from tests.helpers import ADMIN, EMPLOYEE

client = TestClient(app)

@patch("app.routers.query.build_graph")
def test_query_endpoint_safe(mock_build_graph):
    # Mock the LangGraph workflow's return value
    mock_graph = mock_build_graph.return_value
    mock_graph.invoke.return_value = {
        "sql": "SELECT id, name FROM users LIMIT 10",
        "validation": {"safe": True, "requires_human": False},
        "results": {"columns": ["id", "name"], "rows": [{"id": 1, "name": "Alice"}]}
    }
    
    response = client.post(
        "/query",
        json={"query": "Get users"},
        headers=ADMIN
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "completed"
    assert "Alice" in data["results"]["rows"][0]["name"]

@patch("app.routers.query.build_graph")
def test_query_endpoint_requires_human(mock_build_graph):
    mock_graph = mock_build_graph.return_value
    mock_graph.invoke.return_value = {
        "sql": "SELECT * FROM payroll LIMIT 10",
        "validation": {"safe": True, "requires_human": True, "reason": "Medium confidence"}
    }
    
    response = client.post("/query", json={"query": "Get payroll"}, headers=EMPLOYEE)
    
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "requires_human"
    assert "run_id" in data

@patch("app.routers.query.build_graph")
def test_query_endpoint_rejected(mock_build_graph):
    mock_graph = mock_build_graph.return_value
    mock_graph.invoke.return_value = {
        "sql": "DROP TABLE users",
        "validation": {"safe": False, "requires_human": False, "reason": "Forbidden"}
    }
    
    response = client.post("/query", json={"query": "delete everything"}, headers=EMPLOYEE)
    
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "rejected"
    assert "Forbidden" in data["reason"]

@patch("app.routers.hitl.get_run")
@patch("app.routers.hitl.execute_readonly_query")
def test_hitl_decision_approve(mock_execute, mock_get_run):
    # Mock the stored run
    mock_get_run.return_value = {
        "user_query": "Get payroll",
        "sql": "SELECT id, salary FROM payroll LIMIT 10"
    }
    
    # Mock the DB execution result
    mock_execute.return_value = {
        "columns": ["id", "salary"],
        "rows": [{"id": 1, "salary": 5000}]
    }
    
    response = client.post(
        "/hitl/decision",
        json={"run_id": "test-run-id", "decision": "approve"},
        headers=ADMIN
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "completed"
    assert data["results"]["rows"][0]["salary"] == 5000

