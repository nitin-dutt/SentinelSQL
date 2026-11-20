"""
One governed answer to a natural-language question.

`answer_question` is shared by POST /query and the analyst chat (POST /chat), so both ingress paths have exactly the
same controls: the semantic cache, then the pipeline (intent, SQL generation, and the governed run with its policy gate,
review hold, execution and audit record).

The pipeline is an argument. A pipeline is any callable that takes the request state and returns the final state, with
either `outcome` (an `Outcome` from `run` in app/governance.py, which has already been audited) or `stopped`
(`{"status": "blocked" | "error", "reason": ...}` when it stopped before any SQL existed). The default is the LangGraph
pipeline in app/graph/workflow.py. Routers take it through the `get_pipeline` dependency, so a test or a benchmark can
hand in a stand-in instead of patching module attributes.
"""

import time
from typing import Any, Callable, Dict, Optional

from app import demo
from app.api.schemas import QueryResponse
from app.auth import Actor
from app.cache.semantic_cache import get_semantic_cache
from app.governance import BLOCKED, ERROR, HELD, Outcome, record
from app.graph.workflow import build_graph
from app.observability.tracing import set_status, span
from app.utils.logger import get_logger

logger = get_logger("sentinel.query")

Pipeline = Callable[[Dict[str, Any]], Dict[str, Any]]


def default_pipeline(state: Dict[str, Any]) -> Dict[str, Any]:
    return build_graph().invoke(state)


def get_pipeline() -> Pipeline:
    """FastAPI dependency: the pipeline the routers hand to `answer_question`. Override it to substitute a stand-in."""
    return default_pipeline


def answer_question(question: str, actor: Actor, *, channel: str = "WEB_UI", client_agent: str = "sentinel-agent",
                    extra_context: Optional[str] = None, pipeline: Optional[Pipeline] = None,
                    schema_context: Optional[str] = None) -> QueryResponse:
    """
    `schema_context` is the schema text a caller already retrieved for exactly this question (the analyst chat does, for
    its planner); the pipeline then uses it instead of retrieving again. Callers never take it from a request body.


    Must be called inside a trace (see `start_trace`); stage timings are recorded by the pipeline.
    Never raises for a refusal or a failure of the model or the database: those are `rejected` and `error` responses.
    """
    start_time = time.perf_counter()
    semantic_cache = get_semantic_cache()

    def elapsed() -> float:
        return round((time.perf_counter() - start_time) * 1000, 2)

    # Step 1: enterprise semantic cache (L1 exact + L2 vector similarity)
    with span("cache"):
        cached_entry = semantic_cache.get(query=question, role=actor.role)
    if cached_entry:
        latency_ms = elapsed()
        score = cached_entry.get("confidence_score", 100)
        results = cached_entry.get("results")
        logger.info("Cache HIT (%s, sim=%s) in %sms", str(cached_entry.get("match_type", "semantic")).upper(),
                    cached_entry.get("similarity", 1.0), latency_ms)
        # Nothing reaches the database on a cache hit, so there is no governed run to write the record.
        record(actor=actor, channel=channel, client_agent=client_agent, question=question, status="cached",
               sql=cached_entry.get("sql"), score=score, risk_level="LOW", elapsed_ms=latency_ms,
               rows=len(results.get("rows", [])) if isinstance(results, dict) else 0)
        set_status("cached", match_type=cached_entry.get("match_type"))
        return QueryResponse(
            status="completed", sql=cached_entry.get("sql"), results=results,
            confidence_score=score, cached=True, latency_ms=latency_ms,
            cache_match_type=cached_entry.get("match_type"),
            cache_similarity=cached_entry.get("similarity"),
            cache_original_query=cached_entry.get("original_query"),
        )

    demo.charge_llm_call()  # public demo only: spends one unit of the day's language-model budget

    state: Dict[str, Any] = {
        "user_query": question, "user_id": actor.user_id, "role": actor.role,
        "channel": channel, "client_agent": client_agent, "started": start_time,
        "intent": None, "sql": None, "results": None,
    }
    if extra_context:
        state["extra_context"] = extra_context
    if schema_context:
        state["schema_context"] = schema_context

    final_state = (pipeline or default_pipeline)(state)
    outcome: Optional[Outcome] = final_state.get("outcome")
    latency_ms = elapsed()

    if outcome is None:
        # Stopped before any SQL existed (a command to change data, unusable model output, the model or schema down):
        # no governed run happened, so this is the one place the request is recorded.
        stopped = final_state.get("stopped") or {"status": ERROR, "reason": "The request could not be processed"}
        status = "error" if stopped.get("status") == ERROR else "rejected"
        logger.info("Query %s before SQL: %sms | %s", status, latency_ms, stopped.get("reason"))
        record(actor=actor, channel=channel, client_agent=client_agent, question=question, status=status,
               sql=final_state.get("sql"), score=0, risk_level="LOW" if status == "error" else "HIGH",
               elapsed_ms=latency_ms, rows=0)
        set_status(status, reason=(stopped.get("reason") or "")[:200])
        return QueryResponse(status=status, reason=stopped.get("reason"), confidence_score=0, latency_ms=latency_ms)

    # From here the governed run has already written the audit record (and saved the review run of a held query).
    if outcome.status == HELD:
        logger.info("HITL triggered: run %s | confidence %s%% | risk %s | %sms | %s",
                    outcome.run_id, outcome.score, outcome.risk_level, latency_ms, outcome.reason)
        set_status("requires_human", risk_level=outcome.risk_level)
        return QueryResponse(status="requires_human", run_id=outcome.run_id, sql=outcome.sql, reason=outcome.reason,
                             confidence_score=outcome.score, latency_ms=latency_ms)

    if outcome.status == BLOCKED:
        logger.info("Query rejected: confidence %s%% | risk %s | %sms | %s", outcome.score, outcome.risk_level, latency_ms, outcome.reason)
        set_status("rejected", reason=outcome.reason[:200])
        return QueryResponse(status="rejected", reason=outcome.reason, confidence_score=outcome.score, latency_ms=latency_ms)

    if outcome.status == ERROR:
        # The database could not run SQL the policy allowed (after any repair attempt).
        reason = f"Query could not be completed: {outcome.reason}"
        logger.info("Query failed: %sms | %s", latency_ms, reason)
        set_status("error", reason=reason[:200])
        return QueryResponse(status="error", reason=reason, confidence_score=0, latency_ms=latency_ms)

    # Populate the semantic cache on a successful completed query
    if outcome.results is not None:
        semantic_cache.set(query=question, sql=outcome.sql, results=outcome.results, role=actor.role, confidence_score=outcome.score)
    logger.info("Completed: %d rows | confidence %s%% | risk %s | %sms", outcome.rows, outcome.score, outcome.risk_level, latency_ms)
    set_status("completed", rows=outcome.rows, repair_attempts=final_state.get("repair_attempts") or None,
               intent_used=final_state.get("intent_used"), candidate_vote=final_state.get("candidate_vote"))
    return QueryResponse(status="completed", sql=outcome.sql, results=outcome.results,
                         confidence_score=outcome.score, cached=False, latency_ms=latency_ms)
