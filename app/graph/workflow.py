"""
SentinelSQL LangGraph pipeline: intent -> SQL generation -> validation -> execution.

State fields are all declared (alignment_plan RUN-04) so nodes can rely on them: the caller's
identity (`user_id`, `role`) travels with the request, the retrieved schema is computed once and
shared (`schema_context`, RUN-03), and infrastructure failures are reported through `validation`
with `error: True` instead of raising (RUN-02).
"""

import os
from functools import lru_cache
from typing import Any, Dict, Optional, TypedDict

from langgraph.graph import StateGraph, END

from app.agents.intent_agent import IntentAnalysisAgent, QueryIntent
from app.agents.sql_generator import SQLGenerationAgent
from app.agents.sql_validator import SQLValidationAgent
from app.db.database import QueryTimeoutError, execute_readonly_query
from app.rag.schema_retriever import get_rag_schema_context
from app.utils.logger import get_logger

logger = get_logger("sentinel.pipeline")


class AgentState(TypedDict, total=False):
    user_query: str
    user_id: Optional[str]
    role: Optional[str]
    schema_context: Optional[str]
    intent: Optional[Dict[str, Any]]
    sql: Optional[str]
    validation: Optional[Dict[str, Any]]
    results: Optional[Any]
    repair_attempts: int                 # how many times the SQL was sent back to the model after a database error
    last_error: Optional[str]            # the database error awaiting a repair attempt


# Database errors that rewriting the SQL cannot fix: permissions, read-only enforcement, timeouts.
_NOT_REPAIRABLE = ("permission denied", "access denied", "read-only", "read only", "readonly", "statement timeout", "interrupted")


def _max_repairs() -> int:
    try:
        return max(0, int(os.getenv("SQL_REPAIR_ATTEMPTS", "1")))
    except ValueError:
        return 1


def _is_repairable(error: Exception) -> bool:
    """True for a statement the database rejected (unknown column, syntax, ambiguity), not for timeouts, connection loss or permissions."""
    from sqlalchemy.exc import DBAPIError
    if isinstance(error, QueryTimeoutError) or not isinstance(error, DBAPIError) or getattr(error, "connection_invalidated", False):
        return False
    text = str(error).lower()
    return not any(marker in text for marker in _NOT_REPAIRABLE)


def _blocked(reason: str, *, error: bool = False) -> Dict[str, Any]:
    """A terminal verdict. `error=True` marks an infrastructure failure rather than a policy decision."""
    return {
        "safe": False,
        "requires_human": False,
        "reason": reason,
        "score": 0,
        "risk_level": "LOW" if error else "HIGH",
        "error": error,
    }


def intent_node(state: AgentState) -> AgentState:
    try:
        # Retrieved once here and reused by SQL generation (it used to be retrieved twice per request).
        schema = get_rag_schema_context(state["user_query"])
        state["schema_context"] = schema
        intent = IntentAnalysisAgent().analyze(state["user_query"], schema)
        state["intent"] = intent.model_dump()
    except ValueError as e:  # destructive intent, or the model's output was unusable
        state["intent"] = None
        state["validation"] = _blocked(str(e))
    except Exception as e:  # LLM unavailable, schema unavailable, ...
        logger.error("Intent stage failed: %s: %s", type(e).__name__, e)
        state["intent"] = None
        state["validation"] = _blocked(str(e) or "The request could not be processed", error=True)
    return state


def sql_generation_node(state: AgentState) -> AgentState:
    if state.get("intent") is None:
        return state

    try:
        sql = SQLGenerationAgent().generate_sql(
            intent=QueryIntent(**state["intent"]),
            schema_context=state.get("schema_context") or get_rag_schema_context(state["user_query"]),
            user_query=state.get("user_query") or "",
        )
        state["sql"] = sql
    except ValueError as e:
        state["sql"] = None
        state["validation"] = _blocked(f"SQL Generation Failed: {e}")
    except Exception as e:
        logger.error("SQL generation stage failed: %s: %s", type(e).__name__, e)
        state["sql"] = None
        state["validation"] = _blocked(str(e) or "SQL could not be generated", error=True)
    return state


def validation_node(state: AgentState) -> AgentState:
    if not state.get("sql"):
        return state

    result = SQLValidationAgent().validate(state["sql"], state["user_query"])
    state["validation"] = result
    if result.get("sanitized_sql"):
        state["sql"] = result["sanitized_sql"]
    return state


def execution_node(state: AgentState) -> AgentState:
    try:
        state["results"] = execute_readonly_query(state["sql"])
    except Exception as e:
        logger.warning("Execution failed: %s: %s", type(e).__name__, e)
        state["results"] = None
        if _is_repairable(e) and state.get("repair_attempts", 0) < _max_repairs():
            # The model wrote SQL the database rejected (typically an invented column): give it the error once.
            state["last_error"] = str(e)
            return state
        # Timeouts and database errors become a clean response instead of an HTTP 500.
        state["last_error"] = None
        state["validation"] = _blocked(f"Query could not be completed: {e}", error=True)
    return state


def repair_node(state: AgentState) -> AgentState:
    """Asks the model to correct the SQL the database rejected. The result goes back through validation, never straight to execution."""
    state["repair_attempts"] = state.get("repair_attempts", 0) + 1
    error = state.get("last_error") or ""
    state["last_error"] = None
    try:
        state["sql"] = SQLGenerationAgent().repair_sql(
            user_query=state.get("user_query") or "",
            schema_context=state.get("schema_context") or get_rag_schema_context(state.get("user_query")),
            failed_sql=state.get("sql") or "",
            error=error,
        )
        logger.info("SQL repair attempt %d produced a candidate for validation", state["repair_attempts"])
    except Exception as e:  # the repair call failed or returned nothing usable: report the original database error
        logger.warning("SQL repair failed (%s: %s)", type(e).__name__, e)
        state["validation"] = _blocked(f"Query could not be completed: {error}", error=True)
    return state


def route_after_intent(state: AgentState) -> str:
    # Destructive intent or an infrastructure failure: stop immediately
    if state.get("intent") is None and state.get("validation"):
        return END
    return "sql_gen"


def route_after_sql_generation(state: AgentState) -> str:
    return "validate" if state.get("sql") else END


def route_after_execution(state: AgentState) -> str:
    # A repairable database error is waiting: one repair attempt, then validation and execution again
    if state.get("last_error") and state.get("results") is None:
        return "repair"
    return END


def route_after_repair(state: AgentState) -> str:
    validation = state.get("validation")
    if validation and not validation.get("safe", True) and validation.get("error"):
        return END
    return "validate"


def route_after_validation(state: AgentState) -> str:
    validation = state.get("validation")
    if not validation:
        return END
    # Hard-blocked, or risky -> human review: either way nothing executes automatically
    if not validation["safe"] or validation.get("requires_human"):
        return END
    return "execute"


@lru_cache(maxsize=1)
def build_graph():
    """Compiled once and reused: the topology never changes between requests (RUN-03)."""
    graph = StateGraph(AgentState)

    graph.add_node("intent", intent_node)
    graph.add_node("sql_gen", sql_generation_node)
    graph.add_node("validate", validation_node)
    graph.add_node("execute", execution_node)
    graph.add_node("repair", repair_node)

    graph.set_entry_point("intent")

    graph.add_conditional_edges("intent", route_after_intent, {"sql_gen": "sql_gen", END: END})
    graph.add_conditional_edges("sql_gen", route_after_sql_generation, {"validate": "validate", END: END})
    graph.add_conditional_edges("validate", route_after_validation, {"execute": "execute", END: END})
    graph.add_conditional_edges("execute", route_after_execution, {"repair": "repair", END: END})
    graph.add_conditional_edges("repair", route_after_repair, {"validate": "validate", END: END})

    return graph.compile()
