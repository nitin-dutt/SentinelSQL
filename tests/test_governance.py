"""
phase4_redesign_plan.md section 1: one typed policy verdict (`Verdict`) built in one place (`judge`).
The policy rules themselves are tested through `parse_and_validate_ast` (test_sql_parser.py, test_guardrail_accuracy.py);
these tests cover the verdict's own interface.
"""

from unittest.mock import patch

import pytest

from app.governance import Verdict, judge
from app.utils.sql_parser import parse_and_validate_ast


# ---------------------------------------------------------------- judge: the three outcomes

def test_an_ordinary_read_is_allowed():
    v = judge("SELECT name, price FROM products LIMIT 10;")
    assert v.outcome == "allowed" and v.allowed and not v.held and not v.blocked
    assert v.score == 100 and v.risk_level == "LOW" and "products" in v.tables
    assert "High confidence" in v.reason


def test_a_missing_limit_is_injected_and_still_allowed():
    v = judge("SELECT name, price FROM products;")
    assert v.allowed and v.score == 90 and "LIMIT 100" in v.sanitized_sql.upper() and v.breakdown


def test_a_sensitive_table_is_held_for_review():
    v = judge("SELECT employee_id, salary FROM salaries LIMIT 10;")
    assert v.outcome == "held" and v.held and not v.allowed and not v.blocked
    assert v.risk_level == "HIGH" and "sensitive table" in v.reason.lower()


@pytest.mark.parametrize("sql,reason", [
    ("DROP TABLE customers;", "Destructive SQL operation detected: 'DROP'"),
    ("DELETE FROM orders WHERE order_id = 1;", "Destructive SQL operation"),
    ("SELECT 1; DROP TABLE products;", "Multiple stacked SQL statements detected"),
    ("SELECT * FROM users LIMIT 10", "SELECT *"),
])
def test_destructive_stacked_and_wildcard_sql_is_blocked(sql, reason):
    v = judge(sql)
    assert v.outcome == "blocked" and v.blocked and not v.allowed and not v.held
    assert reason in v.reason


def test_the_verdict_carries_the_policy_engines_own_numbers():
    sql = "SELECT c.name, o.amount FROM customers c JOIN orders o ON o.customer_id = c.customer_id"
    ast, v = parse_and_validate_ast(sql), judge(sql)
    assert (v.score, v.risk_level, v.reason, v.sanitized_sql, v.tables, v.breakdown) == (
        ast.confidence_score, ast.risk_level, ast.reason, ast.sanitized_sql, ast.tables_accessed, ast.score_breakdown)


def test_an_unknown_outcome_cannot_be_built():
    with pytest.raises(ValueError):
        Verdict("maybe")


# ---------------------------------------------------------------- the form stored on a review run

def test_as_dict_is_the_json_form_a_review_run_stores():
    import json
    v = judge("SELECT salary FROM salaries LIMIT 5")
    d = json.loads(json.dumps(v.as_dict()))
    assert d["outcome"] == "held" and d["reason"] == v.reason and d["score"] == 75 and d["tables"] == ["salaries"]
    assert "safe" not in d and "requires_human" not in d          # the two-flag spelling is gone


# ---------------------------------------------------------------- EXPERIMENTAL: VALIDATION_MODE=llm
# Delete this block together with app/experimental_llm_grading.py (see the note in `judge`).

class _Grader:
    def __init__(self, reply):
        self.reply, self.prompts = reply, []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


@pytest.mark.parametrize("reply,outcome,risk", [
    ("95", "allowed", "LOW"),
    ("Score: 70", "held", "MEDIUM"),
    ("12", "blocked", "HIGH"),
    ("no number here", "blocked", "HIGH"),
    (RuntimeError("model down"), "blocked", "HIGH"),
])
def test_llm_grading_replaces_the_verdict_of_an_allowed_query(monkeypatch, reply, outcome, risk):
    monkeypatch.setenv("VALIDATION_MODE", "llm")
    grader = _Grader(reply)
    with patch("app.experimental_llm_grading.get_llm", return_value=grader):
        v = judge("SELECT name FROM products LIMIT 5", question="List product names")
    assert v.outcome == outcome and v.risk_level == risk
    assert "LIMIT 5" in v.sanitized_sql and "List product names" in grader.prompts[0]


@pytest.mark.parametrize("sql,outcome", [("DROP TABLE customers", "blocked"), ("SELECT salary FROM salaries LIMIT 5", "held")])
def test_llm_grading_never_loosens_a_policy_block_or_hold(monkeypatch, sql, outcome):
    monkeypatch.setenv("VALIDATION_MODE", "llm")
    grader = _Grader("100")
    with patch("app.experimental_llm_grading.get_llm", return_value=grader):
        assert judge(sql, question="q").outcome == outcome
    assert grader.prompts == []


def test_llm_grading_is_off_by_default(monkeypatch):
    monkeypatch.setenv("VALIDATION_MODE", "")
    with patch("app.experimental_llm_grading.get_llm", side_effect=AssertionError("must not be called")):
        assert judge("SELECT name FROM products LIMIT 5").allowed
