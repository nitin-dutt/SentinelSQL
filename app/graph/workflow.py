from typing import TypedDict, Optional, Dict, Any

from app.agents.intent_agent import QueryIntent

class AgentState(TypedDict):
    user_query: str
    intent: Optional[QueryIntent]
    sql: Optional[str]
    validation: Optional[Dict[str, Any]]
    results: Optional[Any]

from langgraph.graph import StateGraph, END

from app.agents.intent_agent import IntentAnalysisAgent
from app.agents.sql_generator import SQLGenerationAgent
from app.agents.sql_validator import SQLValidationAgent
from app.hitl.approval import human_approval
from app.db.database import execute_readonly_query
from app.utils.schemacontext import get_schema_context

def intent_node(state: AgentState) -> AgentState:
    agent = IntentAnalysisAgent()
    try:
        intent = agent.analyze(state["user_query"], get_schema_context())
        state["intent"] = intent.model_dump()
    except ValueError as e:
        state["intent"] = None
        state["validation"] = {
            "safe": False,
            "requires_human": False,
            "reason": str(e)
        }
    return state


def sql_generation_node(state: AgentState) -> AgentState:
    # 🚫 If intent is missing, skip SQL generation
    if state.get("intent") is None:
        return state

    agent = SQLGenerationAgent()
    sql = agent.generate_sql(
        intent=QueryIntent(**state["intent"]),
        schema_context=get_schema_context()
    )
    state["sql"] = sql
    return state


def validation_node(state: AgentState) -> AgentState:
    validator = SQLValidationAgent()
    result = validator.validate(state["sql"])
    state["validation"] = result
    return state

def hitl_node(state: AgentState) -> AgentState:
    approved_sql = human_approval(state["sql"])
    if approved_sql is None:
        return state
    state["sql"] = approved_sql
    return state

def route_after_intent(state: AgentState) -> str:
    # If destructive intent was detected, stop immediately
    if state.get("intent") is None and state.get("validation"):
        return END

    return "sql_gen"

def execution_node(state: AgentState) -> AgentState:
    results = execute_readonly_query(state["sql"])
    state["results"] = results
    return state

def route_after_validation(state: AgentState) -> str:
    validation = state.get("validation")

    if not validation:
        return END

    # 🛑 Destructive or hard-blocked
    if not validation["safe"] and not validation.get("requires_human"):
        return END

    # 👩‍💻 Risky → HITL
    if validation.get("requires_human"):
        return END


    # ✅ Safe → execute
    return "execute"


def build_graph():
    graph = StateGraph(AgentState)

    graph.add_node("intent", intent_node)
    graph.add_node("sql_gen", sql_generation_node)
    graph.add_node("validate", validation_node)
    graph.add_node("hitl", hitl_node)
    graph.add_node("execute", execution_node)

    graph.set_entry_point("intent")

    graph.add_conditional_edges(
        "intent",
        route_after_intent,
        {
            "sql_gen": "sql_gen",
            END: END
        }
    )
    graph.add_edge("sql_gen", "validate")

    graph.add_conditional_edges(
        "validate",
        route_after_validation,
        {
            "hitl": "hitl",
            "execute": "execute",
            END: END
        }
    )

    graph.add_edge("execute", END)

    return graph.compile()
