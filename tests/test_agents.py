import pytest
from app.agents.sql_validator import SQLValidationAgent
from app.utils.masking import apply_data_masking

def test_sql_validator_blocks_destructive_queries(mock_llm_validation):
    agent = SQLValidationAgent()
    
    # Test DROP
    result = agent.validate("DROP TABLE users", "delete users")
    assert result["safe"] is False
    assert result["requires_human"] is False
    assert "Destructive SQL operation" in result["reason"]  # rejected by the AST engine, not a keyword match

    # Test DELETE
    result = agent.validate("DELETE FROM users WHERE id=1", "remove user")
    assert result["safe"] is False
    assert result["requires_human"] is False

def test_sql_validator_blocks_select_star(mock_llm_validation):
    agent = SQLValidationAgent()
    result = agent.validate("SELECT * FROM users LIMIT 10", "get users")
    assert result["safe"] is False
    assert result["requires_human"] is False
    assert "SELECT *" in result["reason"]

def test_sql_validator_injects_missing_limit_and_auto_executes(mock_llm_validation):
    # Policy (alignment_plan GRD-04): a missing LIMIT is a safety fix, not a risk. It is injected and
    # the query still runs (90%), identically on the web path and the MCP path.
    agent = SQLValidationAgent()
    result = agent.validate("SELECT id, name FROM users", "get users")
    assert result["safe"] is True
    assert result["requires_human"] is False
    assert result["score"] == 90
    assert "LIMIT 100" in result["sanitized_sql"].upper()

def test_sql_validator_high_confidence(mock_llm_validation):
    agent = SQLValidationAgent()
    result = agent.validate("SELECT id, name FROM users LIMIT 10", "get 10 users")
    assert result["safe"] is True
    assert result["requires_human"] is False
    assert "High confidence" in result["reason"]

def test_data_masking():
    raw_results = {
        "columns": ["id", "name", "email", "ssn"],
        "rows": [
            {"id": 1, "name": "Alice", "email": "alice.wonderland@example.com", "ssn": "123456789"},
            {"id": 2, "name": "Bob", "email": "b@example.com", "ssn": "987654321"}
        ]
    }
    
    masked = apply_data_masking(raw_results)
    rows = masked["rows"]
    
    # Check Alice
    assert rows[0]["name"] == "Alice"  # Unchanged
    assert rows[0]["email"] == "a**************d@example.com"
    assert rows[0]["ssn"] == "*****6789"
    
    # Check Bob (edge case for short name)
    assert rows[1]["email"] == "*@example.com"
    assert rows[1]["ssn"] == "*****4321"

