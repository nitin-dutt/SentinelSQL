"""
SentinelSQL LangGraph pipeline: intent -> SQL generation -> governed run (policy verdict, review hold, execution, audit).

State fields are all declared (alignment_plan RUN-04) so nodes can rely on them: the caller's identity (`user_id`,
`role`) and ingress (`channel`, `client_agent`) travel with the request, and the retrieved schema is computed once and
shared (`schema_context`, RUN-03).

Two inputs a caller may set on the request state to point the same pipeline at another database (the Spider benchmark
does; the web routes never do, and neither is reachable from an HTTP request):
  * `schema_context`: the schema text to show the model. When it is given, retrieval is skipped.
  * `executor`: what runs an allowed statement (see `run` in app/governance.py). Default: the configured database.

How a request ends (nothing here raises, RUN-02):
  * `outcome` is set when SQL was written: the `Outcome` of `run` (app/governance.py), which has already written the
    audit record and, for a held query, saved the review run;
  * `stopped` is set when the pipeline stopped before any SQL existed: `{"status": "blocked" | "error", "reason": ...}`
    (a command to change data, unusable model output, or the model or schema being unavailable). Nothing has been
    audited in that case; the caller records it (see app/answering.py).
"""

from functools import lru_cache, wraps
from types import SimpleNamespace
from typing import Any, Dict, Optional, TypedDict

from langgraph.graph import StateGraph, END

from app.agents.intent_agent import IntentAnalysisAgent, QueryIntent, detect_destructive_intent
from app.agents.candidate_voting import candidate_count, choose_candidate
from app.agents.router import use_intent_model
from app.agents.sql_generator import SQLGenerationAgent
from app.governance import BLOCKED, COMPLETED, ERROR, Executor, Outcome, judge, run
from app.observability.tracing import span
from app.rag.schema_retriever import get_rag_schema_context
from app.utils.logger import get_logger
from app.settings import settings

logger = get_logger("sentinel.pipeline")

DEFAULT_CHANNEL = "WEB_UI"
DEFAULT_CLIENT_AGENT = "sentinel-agent"
VOTING_CLIENT_AGENT = "sentinel-candidate-vote"     # audit rows of candidate executions (SQL_CANDIDATES > 1)


class AgentState(TypedDict, total=False):
    user_query: str
    user_id: Optional[str]
    role: Optional[str]
    channel: Optional[str]               # audit ingress column; DEFAULT_CHANNEL when absent
    client_agent: Optional[str]          # audit client column; DEFAULT_CLIENT_AGENT when absent
    started: Optional[float]             # time.perf_counter() when the request began, so the audit time covers the model calls too
    schema_context: Optional[str]        # input or computed: given by the caller, else retrieved once in the intent node
    executor: Optional[Executor]         # input: runs an allowed statement instead of the configured database
    extra_context: Optional[str]         # notes from the analyst agent's schema exploration, appended to the schema shown to the model
    intent: Optional[Dict[str, Any]]
    intent_used: bool                    # False on the direct path (INTENT_MODE): SQL was written from the question without an intent model call
    sql: Optional[str]                   # the generated SQL; after the governed run, the SQL that ran (sanitized)
    candidate_vote: Optional[Dict[str, Any]]   # SQL_CANDIDATES > 1: how the candidates voted (see app/agents/candidate_voting.py)
    outcome: Optional[Outcome]           # the governed run of `sql` (the last one, when a repair ran it again)
    stopped: Optional[Dict[str, Any]]    # the pipeline stopped before any SQL existed: {"status", "reason"}
    results: Optional[Any]               # the masked results of a completed run (same as outcome.results)
    repair_attempts: int                 # how many times the SQL was sent back to the model after a database error
    last_error: Optional[str]            # the database error awaiting a repair attempt
    repair_failed: bool                  # the repair call itself failed; `outcome` is still the failed run


def _max_repairs() -> int:
    return settings.pipeline.sql_repair_attempts


def _stop(reason: str, *, error: bool = False) -> Dict[str, Any]:
    """Why the pipeline stopped before any SQL existed. `error=True` marks an infrastructure failure, not a refusal."""
    return {"status": ERROR if error else BLOCKED, "reason": reason}


def _actor(state: AgentState) -> SimpleNamespace:
    return SimpleNamespace(user_id=state.get("user_id") or "anonymous", role=state.get("role") or "guest")


def _governed(state: AgentState, sql: str, **kwargs: Any) -> Outcome:
    args = dict(actor=_actor(state), channel=state.get("channel") or DEFAULT_CHANNEL, question=state.get("user_query") or "",
                client_agent=state.get("client_agent") or DEFAULT_CLIENT_AGENT, started=state.get("started"),
                executor=state.get("executor"))
    args.update(kwargs)
    return run(sql, **args)


def _stage(name: str):
    """Times a graph node as one stage of the request's trace (app/observability/tracing.py); a no-op when no trace is open."""
    def decorate(fn):
        @wraps(fn)
        def wrapper(state):
            with span(name):
                return fn(state)
        return wrapper
    return decorate


def intent_node(state: AgentState) -> AgentState:
    try:
        # A schema the caller supplied is used as it is. Otherwise it is retrieved once here and reused by SQL
        # generation (it used to be retrieved twice per request).
        schema = state.get("schema_context")
        if not schema:
            with span("retrieval"):
                schema = get_rag_schema_context(state["user_query"])
        if state.get("extra_context"):
            schema = f"{schema}\n\n{state['extra_context']}"
        state["schema_context"] = schema
        with span("router"):
            through_intent_model = use_intent_model(state["user_query"])
        state["intent_used"] = through_intent_model      # set before the model call, so a refusal still says which path it took
        if through_intent_model:
            with span("intent"):
                intent = IntentAnalysisAgent().analyze(state["user_query"], schema)
            state["intent"] = intent.model_dump()
        else:
            # Direct path: no intent model call. The same command filter still runs, and SQL is written from the question itself.
            word = detect_destructive_intent(state["user_query"])
            if word:
                raise ValueError(f"Destructive intent detected: '{word}'")
            state["intent"] = {}
    except ValueError as e:  # destructive intent, or the model's output was unusable
        state["intent"] = None
        state["stopped"] = _stop(str(e))
    except Exception as e:  # LLM unavailable, schema unavailable, ...
        logger.error("Intent stage failed: %s: %s", type(e).__name__, e)
        state["intent"] = None
        state["stopped"] = _stop(str(e) or "The request could not be processed", error=True)
    return state


def _candidate_rows(state: AgentState, sql: str):
    """Runs one voting candidate through the governed sequence, so it is audited like every other execution."""
    outcome = _governed(state, sql, client_agent=VOTING_CLIENT_AGENT, when_held="refuse", started=None)
    if outcome.status != COMPLETED:
        raise RuntimeError(outcome.reason)
    return outcome.results["rows"]


@_stage("generation")
def sql_generation_node(state: AgentState) -> AgentState:
    if state.get("intent") is None:
        return state

    try:
        request = dict(
            intent=QueryIntent(**state["intent"]) if state.get("intent_used", True) else None,
            schema_context=state.get("schema_context") or get_rag_schema_context(state["user_query"]),
            user_query=state.get("user_query") or "",
        )
        n = candidate_count()
        if n > 1:
            # SQL_CANDIDATES: several candidates, the policy gate screens each one, the allowed ones are run read-only
            # (each through `run`, so each is audited) and the most agreed-on result wins. The winner still goes
            # through the governed run below.
            candidates = SQLGenerationAgent().generate_candidates(n=n, **request)
            sql, state["candidate_vote"] = choose_candidate(
                candidates,
                validate=lambda s: judge(s, question=state["user_query"]),
                execute=lambda s: _candidate_rows(state, s),
            )
        else:
            sql = SQLGenerationAgent().generate_sql(**request)
        state["sql"] = sql
    except ValueError as e:
        state["sql"] = None
        state["stopped"] = _stop(f"SQL Generation Failed: {e}")
    except Exception as e:
        logger.error("SQL generation stage failed: %s: %s", type(e).__name__, e)
        state["sql"] = None
        state["stopped"] = _stop(str(e) or "SQL could not be generated", error=True)
    return state


def governed_run_node(state: AgentState) -> AgentState:
    """
    Policy verdict, review hold, read-only execution and the audit record: all of it is `run`. Its trace stages are
    `gate` and `execution`, opened inside `run`.
    """
    if not state.get("sql"):
        return state

    outcome = _governed(state, state["sql"])
    state["outcome"] = outcome
    state["sql"] = outcome.sql
    state["results"] = outcome.results
    # The model wrote SQL the database rejected (typically an invented column): give it the error once.
    retry = outcome.status == ERROR and outcome.repairable and state.get("repair_attempts", 0) < _max_repairs()
    state["last_error"] = outcome.reason if retry else None
    return state


@_stage("repair")
def repair_node(state: AgentState) -> AgentState:
    """Asks the model to correct the SQL the database rejected. The result goes back through the governed run, never straight to execution."""
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
        logger.info("SQL repair attempt %d produced a candidate for the governed run", state["repair_attempts"])
    except Exception as e:  # the repair call failed or returned nothing usable: the failed run stays the outcome
        logger.warning("SQL repair failed (%s: %s)", type(e).__name__, e)
        state["repair_failed"] = True
    return state


def route_after_intent(state: AgentState) -> str:
    # Destructive intent or an infrastructure failure: stop immediately
    if state.get("intent") is None and state.get("stopped"):
        return END
    return "sql_gen"


def route_after_sql_generation(state: AgentState) -> str:
    return "run" if state.get("sql") else END


def route_after_run(state: AgentState) -> str:
    # A repairable database error is waiting: one repair attempt, then the governed run again
    return "repair" if state.get("last_error") else END


def route_after_repair(state: AgentState) -> str:
    return END if state.get("repair_failed") else "run"


@lru_cache(maxsize=1)
def build_graph():
    """Compiled once and reused: the topology never changes between requests (RUN-03)."""
    graph = StateGraph(AgentState)

    graph.add_node("intent", intent_node)
    graph.add_node("sql_gen", sql_generation_node)
    graph.add_node("run", governed_run_node)
    graph.add_node("repair", repair_node)

    graph.set_entry_point("intent")

    graph.add_conditional_edges("intent", route_after_intent, {"sql_gen": "sql_gen", END: END})
    graph.add_conditional_edges("sql_gen", route_after_sql_generation, {"run": "run", END: END})
    graph.add_conditional_edges("run", route_after_run, {"repair": "repair", END: END})
    graph.add_conditional_edges("repair", route_after_repair, {"run": "run", END: END})

    return graph.compile()
