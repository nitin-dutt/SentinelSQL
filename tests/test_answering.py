"""
`answer_question` (app/answering.py) through its own interface: the pipeline is handed in as an argument, nothing is
patched. The stand-in pipelines replace only the model; the governed run, the cache and the audit log are real.
"""

from uuid import uuid4

import pytest

from app.answering import answer_question
from app.auth import Actor
from app.cache.semantic_cache import get_semantic_cache
from app.hitl.web_store import get_run
from app.utils.audit_logger import get_audit_logs
from tests.helpers import pipeline_stopping, pipeline_writing

BOB = Actor(user_id="emp-answer-1", role="employee")


@pytest.fixture(autouse=True)
def empty_cache():
    get_semantic_cache().clear()
    yield
    get_semantic_cache().clear()


def _audit(question):
    return [r for r in reversed(get_audit_logs(limit=300)) if r["natural_language_query"] == question]


def test_a_completed_answer_is_audited_once_by_the_governed_run_and_then_served_from_the_cache():
    question = f"names of customers {uuid4()}"
    pipeline = pipeline_writing("SELECT name FROM customers")
    first = answer_question(question, BOB, pipeline=pipeline)
    second = answer_question(question, BOB, pipeline=pipeline)

    assert first.status == "completed" and first.cached is False and "LIMIT 100" in first.sql.upper() and first.confidence_score == 90
    assert second.status == "completed" and second.cached is True and second.results == first.results
    assert len(pipeline.states) == 1                                       # the second answer never reached the pipeline
    assert [(r["execution_status"], r["user_id"], r["ingress_channel"], r["client_agent"]) for r in _audit(question)] == [
        ("completed", "emp-answer-1", "WEB_UI", "sentinel-agent"), ("cached", "emp-answer-1", "WEB_UI", "sentinel-agent")]


def test_the_pipeline_is_told_who_is_asking_and_through_which_door():
    pipeline = pipeline_writing("SELECT name FROM customers LIMIT 1")
    answer_question(f"door check {uuid4()}", BOB, channel="WEB_CHAT", client_agent="sentinel-analyst", extra_context="notes", pipeline=pipeline)
    state = pipeline.states[0]
    assert (state["user_id"], state["role"], state["channel"], state["client_agent"], state["extra_context"]) == (
        "emp-answer-1", "employee", "WEB_CHAT", "sentinel-analyst", "notes")


def test_a_held_answer_returns_the_review_run_and_is_not_cached():
    question = f"salaries please {uuid4()}"
    pipeline = pipeline_writing("SELECT employee_name, salary FROM salaries LIMIT 5")
    first = answer_question(question, BOB, pipeline=pipeline)
    answer_question(question, BOB, pipeline=pipeline)
    assert first.status == "requires_human" and first.results is None and first.confidence_score == 75
    assert get_run(first.run_id)["user_id"] == "emp-answer-1"
    assert len(pipeline.states) == 2                                       # asked twice, reached the pipeline twice
    assert [r["execution_status"] for r in _audit(question)] == ["requires_human", "requires_human"]


def test_a_blocked_answer_is_rejected_with_the_policys_reason():
    question = f"drop it {uuid4()}"
    answer = answer_question(question, BOB, pipeline=pipeline_writing("DROP TABLE customers"))
    assert answer.status == "rejected" and "Destructive" in answer.reason and answer.sql is None
    assert [r["execution_status"] for r in _audit(question)] == ["rejected"]


def test_a_database_failure_is_an_error_answer():
    question = f"bad column {uuid4()}"
    answer = answer_question(question, BOB, pipeline=pipeline_writing("SELECT nickname FROM customers LIMIT 5"))
    assert answer.status == "error" and answer.reason.startswith("Query could not be completed:") and answer.confidence_score == 0
    assert [r["execution_status"] for r in _audit(question)] == ["error"]


@pytest.mark.parametrize("error,status,risk", [(False, "rejected", "HIGH"), (True, "error", "LOW")])
def test_a_request_that_stops_before_any_sql_is_recorded_by_answer_question(error, status, risk):
    question = f"stopped early {uuid4()}"
    answer = answer_question(question, BOB, pipeline=pipeline_stopping("no SQL was written", error=error))
    assert answer.status == status and answer.reason == "no SQL was written" and answer.confidence_score == 0
    assert [(r["execution_status"], r["risk_level"], r["generated_sql"]) for r in _audit(question)] == [(status, risk, None)]


def test_a_pipeline_that_returns_nothing_usable_is_an_error_not_an_exception():
    question = f"empty pipeline {uuid4()}"
    answer = answer_question(question, BOB, pipeline=lambda state: {})
    assert answer.status == "error"
    assert [r["execution_status"] for r in _audit(question)] == ["error"]
