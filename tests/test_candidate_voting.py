"""SQL_CANDIDATES: execution-guided voting picks the most agreed-on result and never weakens the policy gate."""

from unittest.mock import MagicMock, patch

import pytest

from app.agents.candidate_voting import candidate_count, choose_candidate, result_signature
from app.agents.sql_generator import SQLGenerationAgent
from app.governance import Outcome, Verdict
from app.graph import workflow

SAFE = Verdict("allowed")


def _never(sql):
    raise AssertionError(f"must not be executed: {sql}")


@pytest.mark.parametrize("value,expected", [(None, 1), ("3", 3), ("0", 1), ("99", 5), ("abc", 1)])
def test_candidate_count_is_clamped(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("SQL_CANDIDATES", raising=False)
    else:
        monkeypatch.setenv("SQL_CANDIDATES", value)
    assert candidate_count() == expected


def test_signature_ignores_row_order_column_order_and_int_float():
    assert result_signature([(1, "a"), (2, "b")]) == result_signature([{"x": "b", "y": 2.0}, {"x": "a", "y": 1}])
    assert result_signature([(1,), (1,)]) != result_signature([(1,)])          # duplicates count


def test_majority_result_wins():
    rows = {"A": [(1,)], "B": [(2,)], "C": [(2,)]}
    sql, info = choose_candidate(["A", "B", "C"], lambda s: SAFE, lambda s: rows[s])
    assert sql == "B" and info["votes"] == 2 and info["reason"] == "majority" and info["distinct_results"] == 2


def test_no_agreement_keeps_the_first_candidate():
    rows = {"A": [(1,)], "B": [(2,)], "C": [(3,)]}
    sql, info = choose_candidate(["A", "B", "C"], lambda s: SAFE, lambda s: rows[s])
    assert sql == "A" and info["reason"] == "no_agreement"


def test_a_failing_candidate_is_dropped():
    def execute(s):
        if s == "A":
            raise RuntimeError("no such column")
        return [(7,)]
    sql, info = choose_candidate(["A", "B", "C"], lambda s: SAFE, execute)
    assert sql == "B" and info["statuses"] == ["error", "ok", "ok"]


def test_a_blocked_candidate_is_never_executed_and_cannot_win():
    executed = []
    verdicts = {"BAD": Verdict("blocked"), "B": SAFE, "C": SAFE}
    sql, info = choose_candidate(["BAD", "B", "C"], lambda s: verdicts[s], lambda s: executed.append(s) or [(1,)])
    assert sql == "B" and "BAD" not in executed and info["statuses"][0] == "blocked"


def test_a_review_hold_stops_the_vote():
    verdicts = {"A": SAFE, "SENSITIVE": Verdict("held"), "C": SAFE}
    executed = []
    sql, info = choose_candidate(["A", "SENSITIVE", "C"], lambda s: verdicts[s], lambda s: executed.append(s) or [(1,)])
    assert sql == "SENSITIVE" and info["reason"] == "review_hold" and "SENSITIVE" not in executed and "C" not in executed


def test_all_blocked_returns_the_first_candidate_for_the_normal_path_to_block():
    sql, info = choose_candidate(["A", "B"], lambda s: Verdict("blocked"), _never)
    assert sql == "A" and info["reason"] == "none_executed"


def test_the_sanitized_sql_is_what_gets_executed():
    executed = []
    choose_candidate(["A", "B"], lambda s: Verdict("allowed", sanitized_sql=s + " LIMIT 100"), lambda s: executed.append(s) or [(1,)])
    assert executed == ["A LIMIT 100", "B LIMIT 100"]


def test_identical_candidates_run_once():
    executed = []
    sql, info = choose_candidate(["A", "A", "B"], lambda s: SAFE, lambda s: executed.append(s) or [(len(executed),)])
    assert executed == ["A", "B"] and sql == "A" and info["votes"] == 2


def test_generate_candidates_builds_the_prompt_once_and_drops_unusable_replies():
    fake_llm = MagicMock()
    fake_llm.invoke.side_effect = ["SELECT name FROM customers;", "I cannot help with that.", "SELECT region FROM customers;"]
    with patch("app.agents.sql_generator.get_llm", return_value=fake_llm):
        agent = SQLGenerationAgent()
        with patch.object(agent, "build_prompt", wraps=agent.build_prompt) as build:
            out = agent.generate_candidates(None, "customers(name, region)", "List customer names", 3)
    assert build.call_count == 1 and fake_llm.invoke.call_count == 3
    assert sorted(out) == ["SELECT name FROM customers;", "SELECT region FROM customers;"]


def test_generate_candidates_raises_when_nothing_is_usable():
    fake_llm = MagicMock()
    fake_llm.invoke.return_value = "Sorry."
    with patch("app.agents.sql_generator.get_llm", return_value=fake_llm):
        with pytest.raises(ValueError):
            SQLGenerationAgent().generate_candidates(None, "customers(name)", "List customer names", 2)


def test_pipeline_uses_one_call_by_default(monkeypatch):
    monkeypatch.delenv("SQL_CANDIDATES", raising=False)
    agent = MagicMock()
    agent.generate_sql.return_value = "SELECT 1;"
    with patch.object(workflow, "SQLGenerationAgent", return_value=agent):
        state = workflow.sql_generation_node({"user_query": "q", "intent": {}, "intent_used": False, "schema_context": "t(a)"})
    assert state["sql"] == "SELECT 1;" and "candidate_vote" not in state and not agent.generate_candidates.called


def test_pipeline_votes_when_enabled(monkeypatch):
    monkeypatch.setenv("SQL_CANDIDATES", "3")
    agent = MagicMock()
    agent.generate_candidates.return_value = ["SELECT 1;", "SELECT 2;", "SELECT 2 ;"]
    rows = {"SELECT 1;": [{"x": 1}], "SELECT 2;": [{"x": 2}], "SELECT 2 ;": [{"x": 2}]}
    with patch.object(workflow, "SQLGenerationAgent", return_value=agent), \
         patch.object(workflow, "judge", return_value=SAFE), \
         patch.object(workflow, "run", side_effect=lambda s, **kw: Outcome("completed", s, SAFE, results={"rows": rows[s]})) as governed:
        state = workflow.sql_generation_node({"user_query": "q", "intent": {}, "intent_used": False, "schema_context": "t(a)"})
    assert state["sql"] == "SELECT 2;" and state["candidate_vote"]["votes"] == 2
    assert all(c.kwargs["client_agent"] == "sentinel-candidate-vote" and c.kwargs["when_held"] == "refuse" for c in governed.call_args_list)


def test_candidate_executions_are_audited(monkeypatch):
    """Every candidate that reaches the database goes through the governed run, so each has an audit record."""
    from uuid import uuid4
    from app.utils.audit_logger import get_audit_logs
    monkeypatch.setenv("SQL_CANDIDATES", "3")
    question = f"vote-audit {uuid4()}"
    agent = MagicMock()
    agent.generate_candidates.return_value = ["SELECT COUNT(*) AS n FROM customers", "SELECT COUNT(customer_id) AS n FROM customers",
                                              "SELECT nickname FROM customers LIMIT 5"]
    with patch.object(workflow, "SQLGenerationAgent", return_value=agent):
        state = workflow.sql_generation_node({"user_query": question, "user_id": "emp-1", "role": "employee",
                                              "intent": {}, "intent_used": False, "schema_context": "customers(customer_id, name)"})
    assert state["candidate_vote"]["statuses"] == ["ok", "ok", "error"] and state["candidate_vote"]["votes"] == 2
    rows = [r for r in get_audit_logs(limit=200) if r["natural_language_query"] == question]
    assert sorted(r["execution_status"] for r in rows) == ["completed", "completed", "error"]
    assert {r["client_agent"] for r in rows} == {"sentinel-candidate-vote"} and {r["user_id"] for r in rows} == {"emp-1"}
