import pytest
from app.utils.sql_parser import parse_and_validate_ast


def test_ast_parse_valid_select():
    res = parse_and_validate_ast("SELECT name, email FROM customers LIMIT 10;")
    assert res.is_valid is True
    assert res.requires_human is False
    assert res.risk_level == "LOW"
    assert "customers" in res.tables_accessed
    assert res.has_limit is True


def test_ast_auto_limit_injection():
    # Query without explicit LIMIT
    res = parse_and_validate_ast("SELECT name, email FROM customers;")
    assert res.is_valid is True
    # Verify automated LIMIT 100 injection
    assert "LIMIT 100" in res.sanitized_sql.upper()
    assert res.requires_human is False  # injected LIMIT = auto-execute at 90% (PHASE_2 rubric)
    assert res.risk_level == "LOW"
    assert res.confidence_score == 90
    assert "lacked an explicit limit" in res.reason.lower()


def test_ast_cap_excessive_limit():
    # Query requesting 5000 rows
    res = parse_and_validate_ast("SELECT name FROM customers LIMIT 5000;")
    assert res.is_valid is True
    # Capped to 1000
    assert "LIMIT 1000" in res.sanitized_sql.upper()


def test_ast_block_drop_table():
    res = parse_and_validate_ast("DROP TABLE customers;")
    assert res.is_valid is False
    assert "Destructive SQL operation detected: 'DROP'" in res.reason


def test_ast_block_delete_statement():
    res = parse_and_validate_ast("DELETE FROM orders WHERE order_id = 5;")
    assert res.is_valid is False
    assert "Destructive SQL operation detected: 'DELETE'" in res.reason


def test_ast_block_stacked_queries():
    res = parse_and_validate_ast("SELECT 1; DROP TABLE products;")
    assert res.is_valid is False
    assert "Multiple stacked SQL statements detected" in res.reason


def test_ast_sensitive_table_triggers_high_risk():
    res = parse_and_validate_ast("SELECT employee_id, amount FROM salaries LIMIT 10;")
    assert res.is_valid is True
    assert res.requires_human is True
    assert res.risk_level == "HIGH"
    assert "salaries" in res.tables_accessed
    assert "confidential table" in res.reason.lower()


def test_ast_complex_joins_triggers_medium_risk():
    query = """
    SELECT c.name, o.amount, p.name 
    FROM customers c 
    JOIN orders o ON c.customer_id = o.customer_id 
    JOIN products p ON o.amount > p.price 
    JOIN orders o2 ON o2.order_id = o.order_id 
    LIMIT 10;
    """
    res = parse_and_validate_ast(query)
    assert res.is_valid is True
    assert res.requires_human is True
    assert res.risk_level == "MEDIUM"
    assert res.join_count == 3
    assert "Complex query with 3 JOINs" in res.reason
