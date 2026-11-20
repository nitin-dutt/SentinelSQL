import pytest
from app.utils.masking import apply_data_masking

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


def test_generator_prompt_steers_away_from_joins_for_negation_and_from_gratuitous_distinct():
    """Regression for the Spider failures: a join with <> instead of NOT IN, and DISTINCT added to ordinary lists."""
    from unittest.mock import patch
    from app.agents.intent_agent import QueryIntent
    from app.agents.sql_generator import SQLGenerationAgent

    seen = {}

    class Recorder:
        def invoke(self, prompt):
            seen["prompt"] = prompt
            return "SELECT 1;"

    with patch("app.agents.sql_generator.get_llm", return_value=Recorder()):
        SQLGenerationAgent().generate_sql(QueryIntent(tables=["student"]), "student(stuid)", "Which students do not have a cat?")
    prompt = seen["prompt"]
    assert "Which students do not have a cat?" in prompt
    assert "NOT IN" in prompt and "instead of a join" in prompt
    assert "do not add DISTINCT just because a join could repeat rows" in prompt
    assert "EXISTS subquery write SELECT 1" in prompt

