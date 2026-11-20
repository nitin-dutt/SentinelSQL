"""The analyst chat: follow-ups, one clarifying question, schema tools behind the policy gate, conversation ownership."""

import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.agents import analyst, conversation_store
from app.main import app
from app.observability import tracing
from app.utils.audit_logger import get_audit_logs
from tests.helpers import ADMIN, EMPLOYEE, EMPLOYEE2, pipeline_writing, use_pipeline

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    conversation_store.clear_conversations()
    tracing.clear_traces()
    for name in ("ANALYST_CLARIFY", "ANALYST_EXPLORE", "ANALYST_MAX_STEPS"):
        monkeypatch.delenv(name, raising=False)
    yield


def _llm(*replies):
    fake = MagicMock()
    fake.invoke.side_effect = list(replies)
    return fake


def _plan_json(question, clarify=None, options=()):
    return json.dumps({"standalone_question": question, "needs_clarification": bool(clarify), "clarifying_question": clarify,
                       "options": list(options)})


COMPLETED_TURN = {"message": "Total order amount per customer", "standalone_question": "Total order amount per customer",
                  "status": "completed", "sql": "SELECT c.name, SUM(o.amount) FROM customers c JOIN orders o ON o.customer_id = c.customer_id GROUP BY c.name",
                  "columns": ["name", "SUM(o.amount)"], "row_count": 4}


# ------------------------------------------------------------------ planner

def test_no_model_call_when_there_is_nothing_to_resolve():
    with patch.object(analyst, "get_llm", side_effect=AssertionError("no model call expected")):
        plan = analyst.plan_turn("How many customers are there?", [], "customers(name)", allow_clarify=False)
    assert plan.standalone_question == "How many customers are there?" and plan.planned_by_model is False


def test_a_follow_up_is_rewritten_using_the_conversation():
    fake = _llm(_plan_json("Total order amount per customer, broken down by region"))
    with patch.object(analyst, "get_llm", return_value=fake):
        plan = analyst.plan_turn("now break that down by region", [COMPLETED_TURN], "customers(name, region)")
    prompt = fake.invoke.call_args[0][0]
    assert plan.standalone_question.endswith("by region") and plan.planned_by_model and not plan.needs_clarification
    assert "Total order amount per customer" in prompt and "SUM(o.amount)" in prompt and "now break that down by region" in prompt


def test_the_planner_can_ask_one_clarifying_question():
    fake = _llm(_plan_json("Who are the top customers?", "Top by what?", ["Total revenue", "Number of orders", "x", "y", "z"]))
    with patch.object(analyst, "get_llm", return_value=fake):
        plan = analyst.plan_turn("Who are the top customers?", [], "customers(name)")
    assert plan.needs_clarification and plan.clarifying_question == "Top by what?" and len(plan.options) == 4


def test_no_second_clarifying_question_after_the_user_answered():
    pending = {"message": "Who are the top customers?", "standalone_question": "Who are the top customers?",
               "status": "needs_clarification", "clarification": {"question": "Top by what?", "options": ["Revenue"]}}
    fake = _llm(_plan_json("Top customers by total revenue", "Which year?", ["2025"]))      # the model tries to ask again
    with patch.object(analyst, "get_llm", return_value=fake):
        plan = analyst.plan_turn("by revenue", [pending], "customers(name)")
    prompt = fake.invoke.call_args[0][0]
    assert not plan.needs_clarification and plan.clarifying_question is None and plan.standalone_question == "Top customers by total revenue"
    assert "The user's answer: by revenue" in prompt and "Top by what?" in prompt and "needs_clarification must be false" in prompt


def test_clarification_can_be_switched_off():
    fake = _llm(_plan_json("Who are the top customers?", "Top by what?", ["a", "b"]))
    with patch.object(analyst, "get_llm", return_value=fake):
        plan = analyst.plan_turn("and the top ones?", [COMPLETED_TURN], "customers(name)", allow_clarify=False)
    assert not plan.needs_clarification


@pytest.mark.parametrize("reply", ["I think you mean customers.", "{not json", '{"needs_clarification": true}', "[]"])
def test_an_unusable_planner_reply_falls_back_to_the_message(reply):
    with patch.object(analyst, "get_llm", return_value=_llm(reply)):
        plan = analyst.plan_turn("and by region?", [COMPLETED_TURN], "customers(name)")
    assert plan.standalone_question == "and by region?" and not plan.needs_clarification and plan.planned_by_model is False


def test_a_clarifying_question_without_text_is_dropped():
    with patch.object(analyst, "get_llm", return_value=_llm(_plan_json("Top customers", clarify=None) .replace("false", "true"))):
        plan = analyst.plan_turn("Top customers", [], "customers(name)")
    assert not plan.needs_clarification


# ------------------------------------------------------------------ tools

def test_list_and_describe_tables():
    listing = analyst.tool_list_tables()
    assert "customers" in listing and "sensitive" in listing
    described = analyst.tool_describe_table("CUSTOMERS")
    assert described.startswith("customers:") and "region" in described
    assert analyst.tool_describe_table("no_such_table").startswith("error")


def test_sample_values_goes_through_the_gate_and_is_audited():
    out = analyst.tool_sample_values("customers", "region")
    assert "North" in out
    last = get_audit_logs(limit=1)[0]
    assert last["client_agent"] == "sentinel-analyst-tool" and last["execution_status"] == "completed"
    assert "SELECT DISTINCT region FROM customers" in last["generated_sql"]


def test_sample_values_masks_personal_data():
    out = analyst.tool_sample_values("customers", "email")
    assert "@example.com" in out and "alice@example.com" not in out          # masked like any query result


def test_sample_values_refuses_a_sensitive_table():
    from app.utils.schemacontext import introspect_schema
    from app.utils.sql_parser import is_sensitive_table
    tables = introspect_schema()["tables"]
    sensitive = next(t for t in sorted(tables) if is_sensitive_table(t))
    column = tables[sensitive]["column_names"][0]
    with patch("app.governance.execute_readonly_query", side_effect=AssertionError("must not execute")):
        out = analyst.tool_sample_values(sensitive, column)
    assert out.startswith("refused by the policy gate")
    assert get_audit_logs(limit=1)[0]["execution_status"] == "rejected"


@pytest.mark.parametrize("table,column", [("customers; DROP TABLE customers", "name"), ("customers", "name FROM customers; --"),
                                          ("sqlite_master", "sql"), ("customers", "no_such_column")])
def test_sample_values_only_accepts_identifiers_from_the_schema(table, column):
    with patch("app.governance.execute_readonly_query", side_effect=AssertionError("must not execute")):
        assert analyst.tool_sample_values(table, column).startswith("error")


# ------------------------------------------------------------------ exploration loop

def test_explore_runs_tools_and_returns_notes():
    fake = _llm('{"tool": "describe_table", "args": {"table": "customers"}}',
                '{"tool": "sample_values", "args": {"table": "customers", "column": "region"}}', '{"done": true}')
    with patch.object(analyst, "get_llm", return_value=fake):
        notes, steps = analyst.explore("Customers in the north", "customers(name, region)")
    assert [s["tool"] for s in steps] == ["describe_table", "sample_values"] and "North" in notes
    assert "describe_table" in fake.invoke.call_args_list[1][0][0]              # the second prompt contains the first result
    assert notes.startswith("Notes from looking at the database")


def test_explore_stops_at_the_step_limit(monkeypatch):
    monkeypatch.setenv("ANALYST_MAX_STEPS", "2")
    tables = ["customers", "orders", "products"]
    fake = _llm(*[json.dumps({"tool": "describe_table", "args": {"table": t}}) for t in tables])
    with patch.object(analyst, "get_llm", return_value=fake):
        _, steps = analyst.explore("q", "schema")
    assert len(steps) == 2 and fake.invoke.call_count == 2


@pytest.mark.parametrize("reply", ['{"tool": "run_sql", "args": {"sql": "DROP TABLE customers"}}', "not json at all", '{"done": true}'])
def test_explore_ends_on_an_invented_tool_a_bad_reply_or_done(reply):
    with patch.object(analyst, "get_llm", return_value=_llm(reply)):
        notes, steps = analyst.explore("q", "schema")
    assert notes == "" and steps == []


def test_explore_does_not_repeat_a_call_and_survives_a_model_outage():
    call = '{"tool": "list_tables", "args": {}}'
    with patch.object(analyst, "get_llm", return_value=_llm(call, call)):
        _, steps = analyst.explore("q", "schema")
    assert len(steps) == 1
    broken = MagicMock()
    broken.invoke.side_effect = RuntimeError("model down")
    with patch.object(analyst, "get_llm", return_value=broken):
        assert analyst.explore("q", "schema") == ("", [])


# ------------------------------------------------------------------ conversation store

def test_conversations_belong_to_their_owner_and_expire(monkeypatch):
    cid = conversation_store.create_conversation("emp-1")
    conversation_store.add_turn(cid, message="q", standalone_question="q", status="completed", sql="SELECT 1", columns=["a"], row_count=1)
    assert conversation_store.get_turns(cid, "emp-1")[0]["sql"] == "SELECT 1"
    with pytest.raises(KeyError):
        conversation_store.get_turns(cid, "emp-2")
    assert conversation_store.delete_conversation(cid, "emp-2") is False
    monkeypatch.setenv("CONVERSATION_TTL_HOURS", "0.00001")
    import time
    time.sleep(0.1)
    with pytest.raises(KeyError):
        conversation_store.get_turns(cid, "emp-1")


# ------------------------------------------------------------------ the endpoint

def _pipeline():
    """The model's part is replaced: it always writes this one-row count. The governed run after it is real."""
    return pipeline_writing("SELECT COUNT(*) AS n FROM customers")


def test_chat_first_turn_then_follow_up():
    planner = _llm(_plan_json("How many orders were placed in 2025?"),
                   _plan_json("How many orders were placed in 2025, broken down by region?"))
    with patch.object(analyst, "get_llm", return_value=planner), use_pipeline(_pipeline()) as pipeline:
        first = client.post("/chat", json={"message": "How many orders were placed in 2025?"}, headers=EMPLOYEE).json()
        second = client.post("/chat", json={"message": "now by region", "conversation_id": first["conversation_id"]}, headers=EMPLOYEE).json()

    assert first["status"] == "completed" and first["conversation_id"] and first["trace_id"]
    assert second["standalone_question"] == "How many orders were placed in 2025, broken down by region?"
    asked = [(s["user_query"], s["channel"], s["client_agent"]) for s in pipeline.states]
    assert asked == [("How many orders were placed in 2025?", "WEB_CHAT", "sentinel-analyst"),
                     (second["standalone_question"], "WEB_CHAT", "sentinel-analyst")]
    turns = client.get(f"/chat/{first['conversation_id']}", headers=EMPLOYEE).json()["turns"]
    assert [t["message"] for t in turns] == ["How many orders were placed in 2025?", "now by region"]
    assert turns[0]["columns"] == ["n"] and turns[0]["row_count"] == 1 and "rows" not in turns[0]      # shape only, never rows
    trace = tracing.get_trace(second["trace_id"])
    assert "planner" in tracing.stage_times(trace["spans"]) and trace["attrs"]["rewritten"] is True and trace["channel"] == "WEB_CHAT"


def test_chat_asks_then_uses_the_answer():
    planner = _llm(_plan_json("Who are the top customers?", "Top by what?", ["Total revenue", "Number of orders"]),
                   _plan_json("Who are the top customers by total revenue?"))
    with patch.object(analyst, "get_llm", return_value=planner), use_pipeline(_pipeline()) as pipeline:
        first = client.post("/chat", json={"message": "Who are the top customers?"}, headers=EMPLOYEE).json()
        assert first["status"] == "needs_clarification" and first["clarification"]["options"] == ["Total revenue", "Number of orders"]
        assert pipeline.states == []                                              # nothing ran while we wait for the user
        second = client.post("/chat", json={"message": "Total revenue", "conversation_id": first["conversation_id"]}, headers=EMPLOYEE).json()
    assert second["status"] == "completed" and pipeline.states[-1]["user_query"] == "Who are the top customers by total revenue?"
    assert get_audit_logs(limit=5, status_filter="needs_clarification")[0]["ingress_channel"] == "WEB_CHAT"


def test_chat_without_clarification_makes_no_planner_call_on_the_first_turn(monkeypatch):
    monkeypatch.setenv("ANALYST_CLARIFY", "false")
    with patch.object(analyst, "get_llm", side_effect=AssertionError("no planner call expected")), use_pipeline(_pipeline()) as pipeline:
        out = client.post("/chat", json={"message": "How many customers are there?"}, headers=EMPLOYEE).json()
    assert out["status"] == "completed" and pipeline.states[-1]["user_query"] == "How many customers are there?"


def test_chat_command_to_change_data_skips_the_planner_and_is_rejected_by_the_pipeline():
    with patch.object(analyst, "get_llm", side_effect=AssertionError("the planner must not see a destructive command")):
        out = client.post("/chat", json={"message": "Delete all orders placed in 2025"}, headers=EMPLOYEE).json()
    assert out["status"] == "rejected" and "Destructive intent" in out["reason"]


def test_chat_exploration_notes_reach_the_pipeline(monkeypatch):
    monkeypatch.setenv("ANALYST_EXPLORE", "true")
    monkeypatch.setenv("ANALYST_CLARIFY", "false")
    fake = _llm('{"tool": "sample_values", "args": {"table": "customers", "column": "region"}}', '{"done": true}')
    with patch.object(analyst, "get_llm", return_value=fake), use_pipeline(_pipeline()) as pipeline:
        out = client.post("/chat", json={"message": "Customers in the north"}, headers=EMPLOYEE).json()
    assert out["agent_steps"][0]["tool"] == "sample_values" and "North" in pipeline.states[-1]["extra_context"]
    trace = tracing.get_trace(out["trace_id"])
    assert "explore" in tracing.stage_times(trace["spans"]) and any(s["name"] == "tool:sample_values" for s in trace["spans"])


def test_extra_context_is_appended_to_the_schema_the_generator_sees():
    from app.graph import workflow
    with patch.object(workflow, "get_rag_schema_context", return_value="customers(name, region)"), \
         patch.object(workflow, "use_intent_model", return_value=False):
        state = workflow.intent_node({"user_query": "Customers in the north", "extra_context": "Notes: region values: North, South"})
    assert state["schema_context"] == "customers(name, region)\n\nNotes: region values: North, South"


def test_chat_conversations_are_private_and_roles_are_enforced():
    with patch.object(analyst, "get_llm", return_value=_llm(_plan_json("q"))), use_pipeline(_pipeline()):
        cid = client.post("/chat", json={"message": "q"}, headers=EMPLOYEE).json()["conversation_id"]
    assert client.get(f"/chat/{cid}", headers=EMPLOYEE2).status_code == 404
    assert client.post("/chat", json={"message": "and more", "conversation_id": cid}, headers=EMPLOYEE2).status_code == 404
    assert client.post("/chat", json={"message": "and more", "conversation_id": cid}, headers=ADMIN).status_code == 404
    assert client.post("/chat", json={"message": "q"}).status_code == 401
    assert client.post("/chat", json={"message": "   "}, headers=EMPLOYEE).status_code == 422
    assert client.delete(f"/chat/{cid}", headers=EMPLOYEE).status_code == 200
    assert client.get(f"/chat/{cid}", headers=EMPLOYEE).status_code == 404
