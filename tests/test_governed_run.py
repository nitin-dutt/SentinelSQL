"""
phase4_redesign_plan.md section 2: `run` is the one governed-execution function (verdict, review hold, read-only
execution with masking, one audit record). Tested through its own interface against the temporary SQLite database,
audit log and review store that tests/conftest.py sets up.
"""

from unittest.mock import patch
from uuid import uuid4

import pytest

from app.auth import Actor
from app.db.database import QueryTimeoutError
from app.governance import Approval, Outcome, judge, record, run
from app.hitl.web_store import get_pending_runs, get_run
from app.mcp_server import mcp_submit_sql
from app.utils.audit_logger import get_audit_logs

ALICE = Actor(user_id="emp-run-1", role="employee")


def _go(sql, **kwargs):
    """Runs `sql` under a unique question so its audit rows can be found. Returns (outcome, its audit rows)."""
    question = f"run-test {uuid4()}"
    args = dict(actor=ALICE, channel="WEB_UI", question=question, client_agent="test-agent")
    args.update(kwargs)
    outcome = run(sql, **args)
    return outcome, [r for r in get_audit_logs(limit=500) if r["natural_language_query"] == args["question"]]


def _never_executed():
    return patch("app.governance.execute_readonly_query", side_effect=AssertionError("must not be executed"))


# ---------------------------------------------------------------- completed

def test_an_allowed_query_runs_and_returns_the_sql_that_ran():
    outcome, audit = _go("SELECT name, email FROM customers")
    assert isinstance(outcome, Outcome) and outcome.status == "completed"
    assert "LIMIT 100" in outcome.sql.upper()                      # the sanitized SQL, not the text that was submitted
    assert outcome.rows == len(outcome.results["rows"]) > 0
    assert outcome.run_id is None and outcome.repairable is False


def test_results_are_masked():
    outcome, _ = _go("SELECT name, email FROM customers LIMIT 5")
    emails = [row["email"] for row in outcome.results["rows"]]
    assert emails and all("*" in e for e in emails)


def test_a_completed_run_writes_one_audit_record_with_the_policy_engines_score():
    outcome, audit = _go("SELECT name FROM customers", channel="MCP_AGENT", client_agent="external-mcp-client")
    assert len(audit) == 1
    row = audit[0]
    assert row["execution_status"] == "completed" and row["ingress_channel"] == "MCP_AGENT"
    assert row["client_agent"] == "external-mcp-client" and row["user_id"] == "emp-run-1" and row["role"] == "employee"
    assert row["confidence_score"] == judge("SELECT name FROM customers").score == 90      # a LIMIT was injected: 90, not 100
    assert row["generated_sql"] == outcome.sql and row["masked_rows_count"] == outcome.rows
    assert row["run_id"] is None and row["execution_time_ms"] is not None


# ---------------------------------------------------------------- held

def test_a_sensitive_query_is_held_and_saved_for_review_without_running():
    with _never_executed():
        outcome, audit = _go("SELECT employee_name, salary FROM salaries")
    assert outcome.status == "held" and outcome.results is None and "sensitive table" in outcome.reason.lower()
    saved = get_run(outcome.run_id)
    assert saved["status"] == "pending" and saved["user_id"] == "emp-run-1" and saved["role"] == "employee"
    assert saved["sql"] == outcome.sql and "LIMIT" in outcome.sql.upper()
    assert saved["validation"]["outcome"] == "held"
    assert len(audit) == 1 and audit[0]["execution_status"] == "requires_human" and audit[0]["run_id"] == outcome.run_id
    assert audit[0]["confidence_score"] == outcome.score == 75 and audit[0]["risk_level"] == "HIGH"


def test_refuse_turns_a_hold_into_a_block_and_creates_no_review_run():
    before = set(get_pending_runs())
    with _never_executed():
        outcome, audit = _go("SELECT employee_name, salary FROM salaries LIMIT 3", when_held="refuse")
    assert outcome.status == "blocked" and outcome.run_id is None and outcome.verdict.held
    assert set(get_pending_runs()) == before
    assert len(audit) == 1 and audit[0]["execution_status"] == "rejected"


def test_an_unknown_when_held_is_a_programming_error():
    with pytest.raises(ValueError):
        run("SELECT name FROM customers", actor=ALICE, channel="WEB_UI", question="q", client_agent="t", when_held="execute")


# ---------------------------------------------------------------- blocked

@pytest.mark.parametrize("sql", ["DROP TABLE customers", "SELECT * FROM customers", "SELECT 1; DELETE FROM orders"])
def test_a_blocked_query_is_never_executed_and_is_audited_as_rejected(sql):
    with _never_executed():
        outcome, audit = _go(sql)
    assert outcome.status == "blocked" and outcome.sql == sql and outcome.reason and outcome.results is None
    assert len(audit) == 1 and audit[0]["execution_status"] == "rejected" and audit[0]["generated_sql"] == sql
    assert audit[0]["confidence_score"] == outcome.score and audit[0]["risk_level"] == "HIGH"


# ---------------------------------------------------------------- error

def test_a_statement_the_database_rejects_is_a_repairable_error():
    outcome, audit = _go("SELECT no_such_column FROM customers LIMIT 5")
    assert outcome.status == "error" and outcome.repairable is True and "no_such_column" in outcome.reason
    assert outcome.verdict.allowed and outcome.results is None          # the policy allowed it; the database did not
    assert len(audit) == 1 and audit[0]["execution_status"] == "error" and audit[0]["masked_rows_count"] == 0


def test_a_timeout_is_an_error_that_a_rewrite_cannot_fix():
    with patch("app.governance.execute_readonly_query", side_effect=QueryTimeoutError("too slow")):
        outcome, audit = _go("SELECT name FROM customers LIMIT 5")
    assert outcome.status == "error" and outcome.repairable is False and "too slow" in outcome.reason
    assert len(audit) == 1 and audit[0]["execution_status"] == "error"


# ---------------------------------------------------------------- a reviewer's approval

def _held(sql="SELECT employee_name, salary FROM salaries LIMIT 3"):
    outcome, _ = _go(sql)
    assert outcome.status == "held"
    return outcome


def test_an_approved_hold_runs_and_the_reviewer_and_submitter_get_different_views():
    held = _held()
    outcome, audit = _go(held.sql, question="approved run", approval=Approval(run_id=held.run_id, approved_by="admin-9"))
    assert outcome.status == "completed" and outcome.run_id == held.run_id and outcome.verdict.held
    reviewer = outcome.results["rows"]
    stored = get_run(held.run_id)
    assert stored["status"] == "completed" and stored["sql"] == outcome.sql
    submitter = stored["results"]["rows"]
    assert all(isinstance(r["salary"], (int, float)) for r in reviewer)                 # the reviewer sees the figures
    assert all(r["salary"] != s["salary"] for r, s in zip(reviewer, submitter))         # the submitter's copy is masked
    assert [r["employee_name"] for r in reviewer] == [s["employee_name"] for s in submitter]


def test_an_approval_is_audited_with_the_approver_and_the_review_run():
    held = _held()
    question = f"approval-audit {uuid4()}"
    run(held.sql, actor=ALICE, channel="WEB_UI", client_agent="sentinel-internal", question=question,
        approval=Approval(run_id=held.run_id, approved_by="admin-9"))
    rows = [r for r in get_audit_logs(limit=200) if r["natural_language_query"] == question]
    assert [(r["execution_status"], r["approved_by"], r["run_id"], r["user_id"]) for r in rows] == [
        ("hitl_approved", "admin-9", held.run_id, "emp-run-1")]


def test_an_approval_cannot_get_past_a_policy_block():
    held = _held()
    with patch("app.governance.execute_readonly_query_for_review", side_effect=AssertionError("must not be executed")):
        outcome, audit = _go("SELECT * FROM salaries", approval=Approval(run_id=held.run_id, approved_by="admin-9"))
    assert outcome.status == "blocked"
    assert get_run(held.run_id)["status"] == "failed"
    assert [r["execution_status"] for r in audit] == ["hitl_blocked_by_policy"]


def test_an_approved_query_the_database_rejects_marks_the_review_run_failed():
    held = _held()
    with patch("app.governance.execute_readonly_query_for_review", side_effect=QueryTimeoutError("too slow")):
        outcome, audit = _go(held.sql, approval=Approval(run_id=held.run_id, approved_by="admin-9"))
    stored = get_run(held.run_id)
    assert outcome.status == "error" and stored["status"] == "failed" and "too slow" in stored["reason"]
    assert [r["execution_status"] for r in audit] == ["hitl_execution_failed"]


def test_only_an_approval_reveals_salary_values():
    with patch("app.governance.execute_readonly_query", return_value={"columns": ["n"], "rows": [{"n": 1}]}) as execute:
        _go("SELECT name FROM customers LIMIT 1")
    assert execute.call_args.kwargs == {} and len(execute.call_args.args) == 1          # run() has no switch for it


# ---------------------------------------------------------------- record(): events with no execution behind them

def test_record_writes_one_audit_row_with_the_actor_and_ingress():
    question = f"record-test {uuid4()}"
    record(actor=ALICE, channel="WEB_CHAT", client_agent="sentinel-analyst", question=question, status="needs_clarification")
    rows = [r for r in get_audit_logs(limit=100) if r["natural_language_query"] == question]
    assert [(r["execution_status"], r["user_id"], r["role"], r["ingress_channel"], r["client_agent"], r["confidence_score"])
            for r in rows] == [("needs_clarification", "emp-run-1", "employee", "WEB_CHAT", "sentinel-analyst", 0)]


def test_the_audit_logger_is_only_called_from_the_governance_module():
    import os
    import re
    import app
    root = os.path.dirname(app.__file__)
    callers = []
    for folder, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(folder, name)
            if name.endswith(".py") and re.search(r"\blog_audit_event\(", open(path, encoding="utf-8").read()):
                callers.append(os.path.relpath(path, root).replace(os.sep, "/"))
    assert sorted(callers) == ["governance.py", "utils/audit_logger.py"]


def test_no_router_imports_another_router():
    import os
    import re
    import app
    folder = os.path.join(os.path.dirname(app.__file__), "routers")
    for name in os.listdir(folder):
        if name.endswith(".py"):
            assert not re.search(r"from app\.routers|import app\.routers", open(os.path.join(folder, name), encoding="utf-8").read()), name


# ---------------------------------------------------------------- MCP goes through run (the three drifts)

def _mcp(sql):
    rationale = f"mcp-run-test {uuid4()}"
    reply = mcp_submit_sql(sql, rationale=rationale, client_id="agent-7")
    return reply, [r for r in get_audit_logs(limit=500) if r["natural_language_query"] == f"[MCP] {rationale}"]


def test_mcp_returns_the_sanitized_sql_and_audits_the_real_score():
    reply, audit = _mcp("SELECT name FROM customers")
    assert reply["status"] == "completed" and "LIMIT 100" in reply["sql"].upper() and reply["results"]["rows"]
    assert len(audit) == 1 and audit[0]["execution_status"] == "completed" and audit[0]["confidence_score"] == 90
    assert audit[0]["generated_sql"] == reply["sql"] and audit[0]["user_id"] == "agent-7" and audit[0]["ingress_channel"] == "MCP_AGENT"


def test_mcp_rejections_use_the_same_audit_status_as_the_web_path():
    reply, audit = _mcp("DROP TABLE customers")
    assert reply["status"] == "rejected" and reply["sql"] == "DROP TABLE customers" and "Destructive" in reply["reason"]
    assert [r["execution_status"] for r in audit] == ["rejected"]


def test_mcp_database_failures_are_audited_as_error_and_reported_as_failed():
    reply, audit = _mcp("SELECT no_such_column FROM customers LIMIT 5")
    assert reply["status"] == "failed" and "no_such_column" in reply["reason"]
    assert [r["execution_status"] for r in audit] == ["error"]


def test_mcp_holds_carry_the_run_id_and_the_engines_score():
    reply, audit = _mcp("SELECT employee_name, salary FROM salaries LIMIT 5")
    assert reply["status"] == "requires_human" and get_run(reply["run_id"])["status"] == "pending"
    assert [(r["execution_status"], r["run_id"], r["confidence_score"]) for r in audit] == [("requires_human", reply["run_id"], 75)]


def test_mcp_has_no_hand_written_copy_of_the_sequence():
    import app.mcp_server
    source = open(app.mcp_server.__file__, encoding="utf-8").read()
    for name in ("log_audit_event", "save_run", "execute_readonly_query", "judge("):
        assert name not in source, name


# ---------------------------------------------------------------- an answer cut by the gate's LIMIT is marked

def _rows(n):
    return lambda sql: {"columns": ["name"], "rows": [{"name": str(i)} for i in range(n)]}


def test_an_answer_that_reaches_the_injected_limit_is_marked_truncated():
    from app.utils.sql_parser import DEFAULT_SAFETY_LIMIT
    outcome, _ = _go("SELECT name FROM customers", executor=_rows(DEFAULT_SAFETY_LIMIT))
    assert outcome.results["truncated"] is True and outcome.results["row_limit"] == DEFAULT_SAFETY_LIMIT


def test_a_shorter_answer_or_the_querys_own_limit_is_never_marked():
    from app.utils.sql_parser import DEFAULT_SAFETY_LIMIT
    short, _ = _go("SELECT name FROM customers", executor=_rows(DEFAULT_SAFETY_LIMIT - 1))
    top5, _ = _go("SELECT name FROM customers LIMIT 5", executor=_rows(5))
    assert "truncated" not in short.results and "truncated" not in top5.results
