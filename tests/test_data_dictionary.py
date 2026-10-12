"""
SentinelSQL Enterprise Data Dictionary & Business Glossary Test Suite
---------------------------------------------------------------------
Tests:
1. Seed initialization of foundational business metrics.
2. add_term, get_all_terms, delete_term persistence.
3. Dual-strategy retrieval (Lexical exact match + Semantic vector search).
4. Prompt context formatting for SQL generation.
5. dbt manifest.json metric and model importation.
6. REST API endpoints (GET /dictionary, GET/POST/DELETE /admin/dictionary).
7. Integration with SQLGenerationAgent prompt construction.
"""

import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from app.main import app
from app.rag.data_dictionary import (
    get_all_terms,
    add_term,
    delete_term,
    find_relevant_terms,
    format_dictionary_context,
    import_dbt_manifest,
)
from app.agents.intent_agent import QueryIntent
from app.agents.sql_generator import SQLGenerationAgent
from tests.helpers import ADMIN, EMPLOYEE

client = TestClient(app)


def test_default_glossary_seeds():
    terms = get_all_terms()
    assert len(terms) >= 6
    term_names = [t["term"] for t in terms]
    assert "Net Revenue" in term_names
    assert "Active Customer" in term_names
    assert "VIP Customer" in term_names


def test_add_and_delete_term():
    term_id = add_term(
        term="Customer Churn Rate",
        definition="Percentage of subscribers who canceled or lapsed in the past 30 days.",
        formula_or_rule="COUNT(CASE WHEN status = 'canceled' THEN 1 END) * 1.0 / COUNT(*)",
        related_tables="subscriptions",
        category="metric",
        created_by="head_of_data"
    )
    assert term_id > 0

    all_terms = get_all_terms()
    saved = next((t for t in all_terms if t["term"] == "Customer Churn Rate"), None)
    assert saved is not None
    assert "canceled" in saved["formula_or_rule"]
    assert saved["created_by"] == "head_of_data"

    # Delete term
    assert delete_term(term_id) is True
    assert not any(t["id"] == term_id for t in get_all_terms())


def test_dual_retrieval_and_formatting():
    # 1. Exact match on Net Revenue
    context = format_dictionary_context("Show me the quarterly Net Revenue by region")
    assert "Enterprise Business Glossary & Metric Rules:" in context
    assert "Net Revenue" in context
    assert "SUM(orders.amount)" in context

    # 2. Semantic search for high value customers (should retrieve VIP Customer)
    relevant = find_relevant_terms("List big spending high volume accounts", top_k=2)
    assert len(relevant) > 0


def test_import_dbt_manifest():
    mock_manifest = {
        "metrics": {
            "monthly_active_users": {
                "label": "Monthly Active Users (MAU)",
                "description": "Distinct active users in 30 days",
                "type": "count_distinct",
                "sql": "user_id",
                "model": "fct_events"
            }
        },
        "nodes": {
            "model.sentinel.dim_customers": {
                "resource_type": "model",
                "name": "dim_customers",
                "description": "Master customer dimension table containing verified identity and loyalty attributes."
            }
        }
    }

    count = import_dbt_manifest(mock_manifest, created_by="dbt_ci")
    assert count == 2

    terms = get_all_terms()
    mau_term = next((t for t in terms if t["term"] == "Monthly Active Users (MAU)"), None)
    assert mau_term is not None
    assert "fct_events" in mau_term["related_tables"]

    dim_term = next((t for t in terms if t["term"] == "dim_customers Model"), None)
    assert dim_term is not None

    # Cleanup
    delete_term(mau_term["id"])
    delete_term(dim_term["id"])


def test_admin_dictionary_rbac_and_endpoints():
    # Any authenticated user may browse the glossary
    res_pub = client.get("/dictionary", headers=EMPLOYEE)
    assert res_pub.status_code == 200
    assert len(res_pub.json()) >= 6

    # Admin endpoints require the admin role
    res_forbidden = client.get("/admin/dictionary", headers=EMPLOYEE)
    assert res_forbidden.status_code == 403

    res_post_forbidden = client.post(
        "/admin/dictionary",
        json={"term": "X", "definition": "Y", "formula_or_rule": "Z"},
        headers=EMPLOYEE
    )
    assert res_post_forbidden.status_code == 403

    # Admin authorized creation
    res_create = client.post(
        "/admin/dictionary",
        json={
            "term": "Test Metric Alpha",
            "definition": "Testing metric for unit tests",
            "formula_or_rule": "SUM(amount) * 1.1",
            "related_tables": "orders",
            "category": "metric"
        },
        headers=ADMIN
    )
    assert res_create.status_code == 200
    new_id = res_create.json()["id"]

    # Admin authorized deletion
    res_del = client.delete(f"/admin/dictionary/{new_id}", headers=ADMIN)
    assert res_del.status_code == 200
    assert res_del.json()["status"] == "success"


@patch("app.agents.sql_generator.get_llm")
def test_sql_generator_injects_dictionary(mock_get_llm):
    mock_llm_instance = MagicMock()
    mock_llm_instance.invoke.return_value = "SELECT c.id, c.name FROM customers c WHERE c.status = 'active' LIMIT 10;"
    mock_get_llm.return_value = mock_llm_instance

    agent = SQLGenerationAgent()
    intent = QueryIntent(
        tables=["customers"],
        metrics=[],
        group_by=[],
        filters=[],
        limit=10
    )

    agent.generate_sql(
        intent=intent,
        schema_context="CREATE TABLE customers (id INT, status TEXT, name TEXT);",
        user_query="List all Active Customers"
    )

    # Verify that the LLM was invoked with the Data Dictionary rules in its prompt
    called_prompt = mock_llm_instance.invoke.call_args[0][0]
    assert "Enterprise Business Glossary & Metric Rules:" in called_prompt
    assert "Active Customer" in called_prompt
    assert "customers.customer_id IN (SELECT orders.customer_id FROM orders)" in called_prompt
