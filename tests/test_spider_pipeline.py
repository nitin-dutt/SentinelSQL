"""
phase4_redesign_plan.md section 4: the pipeline accepts its schema and its executor from the caller, and the Spider
benchmark's governed arm is that pipeline, not a copy of it. Only the model is replaced here; the graph, the policy
gate, the execution path and the audit log are real.
"""

import inspect
import sqlite3
from unittest.mock import patch

import pytest

from app.auth import Actor
from app.governance import Approval, run
from app.graph import workflow
from app.utils.audit_logger import get_audit_logs
from benchmarks.lib.sqlcompare import run_sqlite
from benchmarks.suites import spider

SCHEMA = "singer(singer_id, name, country)"
GOLD = "SELECT name FROM singer WHERE country = 'France'"


@pytest.fixture
def singer_db(tmp_path, monkeypatch):
    """A small database that is NOT the configured one: only the injected executor can answer from it."""
    path = str(tmp_path / "concert.sqlite")
    con = sqlite3.connect(path)
    con.executescript("CREATE TABLE singer(singer_id INTEGER, name TEXT, country TEXT);"
                      "INSERT INTO singer VALUES (1, 'Joe', 'France'), (2, 'Ann', 'Spain'), (3, 'Lea', 'France');")
    con.commit()
    con.close()
    monkeypatch.setenv("INTENT_MODE", "direct")          # no intent model call: one model reply per question
    return path


def _governed(db, *replies, repair=False):
    item = {"index": 0, "db_id": "concert", "question": "Which singers are from France?", "gold": GOLD}
    gold_rows = run_sqlite(db, GOLD)[1]
    with patch("app.utils.llm_factory.UniversalLLM.invoke", side_effect=list(replies)):
        return spider.run_governed(item, db, gold_rows, SCHEMA, repair=repair)


def test_the_governed_arm_answers_from_the_spider_database_through_the_real_pipeline(singer_db):
    out = _governed(singer_db, "SELECT name FROM singer WHERE country = 'France'")
    assert out["outcome"] == "executed" and out["correct"] is True and out["intent_path"] is False
    assert out["executions"] == 1 and out["audit_rows"] == 1
    assert "LIMIT" in out["sql"].upper()                                   # the policy gate's row cap was applied
    row = get_audit_logs(limit=1)[0]                                       # written by governance.run, like any request
    assert (row["ingress_channel"], row["client_agent"], row["execution_status"]) == ("BENCHMARK", "spider-benchmark", "completed")


def test_a_policy_block_is_the_pipelines_and_nothing_is_executed(singer_db):
    out = _governed(singer_db, "SELECT * FROM singer")
    assert out["outcome"] == "blocked_by_policy" and out["correct"] is False and out["executions"] == 0
    assert get_audit_logs(limit=1)[0]["execution_status"] == "rejected"


def test_the_repair_variant_uses_the_pipelines_repair_loop(singer_db):
    out = _governed(singer_db, "SELECT nme FROM singer WHERE country = 'France'", GOLD, repair=True)
    assert out["outcome"] == "executed" and out["correct"] is True and out["repaired"] is True
    assert out["executions"] == 2 and out["audit_rows"] == 2 and "nme" in out["failed_executions"][0]["error"]


def test_without_the_repair_variant_a_rejected_statement_is_a_wrong_answer(singer_db):
    out = _governed(singer_db, "SELECT nme FROM singer")
    assert out["outcome"] == "executed" and out["correct"] is False and "nme" in out["error"] and "repaired" not in out


def test_a_provider_failure_stops_the_run_instead_of_being_scored(singer_db):
    with pytest.raises(RuntimeError, match="quota"):
        _governed(singer_db, RuntimeError("quota exhausted"))


def test_run_governed_holds_no_copy_of_the_pipeline():
    source = inspect.getsource(spider.run_governed)
    for name in ("IntentAnalysisAgent", "SQLGenerationAgent", "parse_and_validate_ast", "policy_judge", "choose_candidate", "repair_sql"):
        assert name not in source, name


# ---------------------------------------------------------------- the two inputs, on their own

def test_a_schema_the_caller_supplies_is_used_and_retrieval_is_skipped():
    with patch.object(workflow, "get_rag_schema_context", side_effect=AssertionError("must not retrieve")), \
         patch.object(workflow, "use_intent_model", return_value=False):
        state = workflow.intent_node({"user_query": "q", "schema_context": "t(a)", "extra_context": "Notes: x"})
    assert state["schema_context"] == "t(a)\n\nNotes: x" and state["intent_used"] is False and not state.get("stopped")


def test_run_uses_the_executor_it_is_given_and_still_gates_and_audits():
    seen = []

    def executor(sql):
        seen.append(sql)
        return {"columns": ["n"], "rows": [{"n": 7}]}
    args = dict(actor=Actor(user_id="emp-x", role="employee"), channel="BENCHMARK", question="executor-test", client_agent="t")
    with patch("app.governance.execute_readonly_query", side_effect=AssertionError("the configured database must not be used")):
        done = run("SELECT name FROM customers", executor=executor, **args)
        blocked = run("SELECT * FROM customers", executor=executor, **args)
    assert done.status == "completed" and done.results["rows"] == [{"n": 7}] and seen == [done.sql] and "LIMIT" in done.sql.upper()
    assert blocked.status == "blocked" and len(seen) == 1
    assert [r["execution_status"] for r in get_audit_logs(limit=2)] == ["rejected", "completed"]
    with pytest.raises(ValueError):
        run("SELECT name FROM customers", executor=executor, approval=Approval(run_id="r", approved_by="a"), **args)
