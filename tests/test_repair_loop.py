"""
The one-shot repair loop: when the database rejects the model's SQL (typically an invented column), the error goes back to
the model once, and the corrected SQL goes through the SAME governed run (policy gate first) before it can run. The real graph and the real
database are used; only the model calls are replaced.
"""

from unittest.mock import patch

import pytest
from sqlalchemy.exc import ProgrammingError

from app.agents.intent_agent import QueryIntent
from app.db.database import QueryTimeoutError
from app.governance import is_repairable
from app.graph.workflow import build_graph

GOOD = "SELECT name FROM customers"
BAD_COLUMN = "SELECT nme FROM customers"          # the database answers: no such column


def run(first_sql, repairs, question="List the customers"):
    """Runs the graph with generation returning `first_sql` and repair_sql returning each of `repairs` in turn."""
    with patch("app.graph.workflow.get_rag_schema_context", return_value="customers(customer_id, name, email, region)"), \
            patch("app.graph.workflow.IntentAnalysisAgent") as intent, patch("app.graph.workflow.SQLGenerationAgent") as gen:
        intent.return_value.analyze.return_value = QueryIntent(tables=["customers"])
        gen.return_value.generate_sql.return_value = first_sql
        seq = list(repairs)
        gen.return_value.repair_sql.side_effect = lambda **kw: seq.pop(0) if not isinstance(seq[0], Exception) else (_ for _ in ()).throw(seq.pop(0))
        state = build_graph().invoke({"user_query": question, "intent": None, "sql": None, "results": None,
                                      "user_id": "u", "role": "employee"})
        return state, gen.return_value.repair_sql


def test_an_invented_column_is_repaired_and_the_query_then_runs():
    state, repair = run(BAD_COLUMN, [GOOD])
    assert repair.call_count == 1
    assert state["results"]["rows"] and state["outcome"].status == "completed"
    assert state["sql"].lower().startswith("select name from customers")
    assert "no such column" in repair.call_args.kwargs["error"].lower()           # the model was told what the database said


def test_a_query_that_works_the_first_time_is_never_sent_for_repair():
    state, repair = run(GOOD, [])
    assert repair.call_count == 0 and state["results"]["rows"]


def test_the_repaired_sql_is_validated_again_and_a_destructive_rewrite_never_runs():
    state, repair = run(BAD_COLUMN, ["DELETE FROM customers"])
    assert repair.call_count == 1
    assert state["results"] is None
    assert state["outcome"].status == "blocked"


def test_a_repaired_query_on_a_sensitive_table_waits_for_a_human():
    state, _ = run(BAD_COLUMN, ["SELECT employee_name, salary FROM salaries"])
    assert state["results"] is None
    assert state["outcome"].status == "held" and state["outcome"].run_id


def test_only_one_repair_attempt_is_made():
    state, repair = run(BAD_COLUMN, ["SELECT nme2 FROM customers", GOOD])
    assert repair.call_count == 1
    assert state["results"] is None and state["outcome"].status == "error"
    assert "nme2" in state["outcome"].reason                 # the second failure is reported; it is not sent for repair again


def test_repair_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("SQL_REPAIR_ATTEMPTS", "0")
    state, repair = run(BAD_COLUMN, [GOOD])
    assert repair.call_count == 0 and state["results"] is None and state["outcome"].status == "error"


def test_a_failing_repair_call_reports_the_original_database_error():
    state, repair = run(BAD_COLUMN, [RuntimeError("model unavailable")])
    assert repair.call_count == 1
    assert state["results"] is None and state["outcome"].status == "error" and state["repair_failed"] is True
    assert "no such column" in state["outcome"].reason.lower()


def test_a_timeout_is_not_sent_for_repair():
    with patch("app.governance.execute_readonly_query", side_effect=QueryTimeoutError("too slow")):
        state, repair = run(GOOD, [GOOD])
    assert repair.call_count == 0 and state["outcome"].status == "error" and state["outcome"].repairable is False


def test_a_permission_error_is_not_sent_for_repair():
    denied = ProgrammingError("SELECT 1", {}, Exception("permission denied for table customers"))
    with patch("app.governance.execute_readonly_query", side_effect=denied):
        state, repair = run(GOOD, [GOOD])
    assert repair.call_count == 0 and state["outcome"].status == "error" and state["outcome"].repairable is False


def test_every_statement_sent_to_the_database_gets_its_own_audit_record():
    """A repaired request leaves two rows: the SQL the database rejected, then the SQL that ran."""
    from uuid import uuid4
    from app.utils.audit_logger import get_audit_logs
    question = f"repair-audit {uuid4()}"
    run(BAD_COLUMN, [GOOD], question=question)
    rows = [r for r in reversed(get_audit_logs(limit=200)) if r["natural_language_query"] == question]
    assert [(r["execution_status"], r["generated_sql"].split(" FROM")[0]) for r in rows] == [("error", "SELECT nme"), ("completed", "SELECT name")]


@pytest.mark.parametrize("message,expected", [
    ("no such column: nme", True),
    ('column "nme" does not exist', True),
    ("Unknown column 'nme' in 'field list'", True),
    ("syntax error at or near FROM", True),
    ("permission denied for table salaries", False),
    ("attempt to write a readonly database", False),
    ("canceling statement due to statement timeout", False),
])
def test_which_database_errors_are_worth_a_repair_attempt(message, expected):
    assert is_repairable(ProgrammingError("SELECT 1", {}, Exception(message))) is expected


def test_non_database_errors_are_not_repairable():
    assert is_repairable(ValueError("boom")) is False


def test_the_repair_prompt_carries_the_question_schema_failed_sql_and_error():
    from app.agents.sql_generator import SQLGenerationAgent
    seen = {}

    class Recorder:
        def invoke(self, prompt):
            seen["p"] = prompt
            return "SELECT name FROM customers;"

    with patch("app.agents.sql_generator.get_llm", return_value=Recorder()):
        sql = SQLGenerationAgent().repair_sql("List the customers", "customers(name)", "SELECT nme FROM customers", "no such column: nme")
    p = seen["p"]
    assert sql.startswith("SELECT name FROM customers")
    for needle in ("List the customers", "customers(name)", "SELECT nme FROM customers", "no such column: nme", "Use ONLY tables and columns"):
        assert needle in p


def test_the_few_shot_count_is_configurable(monkeypatch):
    from app.agents.sql_generator import _few_shot_count
    assert _few_shot_count() == 2
    monkeypatch.setenv("FEWSHOT_TOP_K", "5")
    assert _few_shot_count() == 5
    monkeypatch.setenv("FEWSHOT_TOP_K", "0")
    assert _few_shot_count() == 0
    monkeypatch.setenv("FEWSHOT_TOP_K", "many")
    assert _few_shot_count() == 2
