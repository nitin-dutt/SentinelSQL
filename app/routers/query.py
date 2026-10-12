"""Natural-language query endpoint and run-status polling."""

import time

from fastapi import APIRouter, Depends, HTTPException

from app.api.schemas import QueryRequest, QueryResponse
from app.auth import Actor, get_actor, require_roles, ROLE_ADMIN, ROLE_EMPLOYEE
from app.cache.semantic_cache import get_semantic_cache
from app.graph.workflow import build_graph
from app.hitl.web_store import get_run, save_run
from app.utils.audit_logger import log_audit_event
from app.utils.logger import get_logger

logger = get_logger("sentinel.query")
router = APIRouter()


def _rows_in(results) -> int:
    return len(results.get("rows", [])) if results and isinstance(results, dict) else 0


@router.post("/query", response_model=QueryResponse)
def run_query(req: QueryRequest, actor: Actor = Depends(require_roles(ROLE_EMPLOYEE, ROLE_ADMIN))):
    user_id = actor.user_id
    role = actor.role
    start_time = time.perf_counter()
    semantic_cache = get_semantic_cache()

    # Step 1: enterprise semantic cache (L1 exact + L2 vector similarity)
    cached_entry = semantic_cache.get(query=req.query, role=role)
    if cached_entry:
        latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
        score = cached_entry.get("confidence_score", 100)
        results = cached_entry.get("results")
        logger.info("Cache HIT (%s, sim=%s) in %sms", str(cached_entry.get("match_type", "semantic")).upper(),
                    cached_entry.get("similarity", 1.0), latency_ms)
        log_audit_event(
            run_id=None, user_id=user_id, role=role, user_query=req.query,
            generated_sql=cached_entry.get("sql"), confidence_score=score,
            execution_status="cached", ingress_channel="WEB_UI",
            client_agent="sentinel-agent", risk_level="LOW",
            execution_time_ms=latency_ms, masked_rows_count=_rows_in(results)
        )
        return QueryResponse(
            status="completed", sql=cached_entry.get("sql"), results=results,
            confidence_score=score, cached=True, latency_ms=latency_ms,
            cache_match_type=cached_entry.get("match_type"),
            cache_similarity=cached_entry.get("similarity"),
            cache_original_query=cached_entry.get("original_query"),
        )

    state = {
        "user_query": req.query,
        "intent": None,
        "sql": None,
        "validation": None,
        "results": None,
        "user_id": user_id,
        "role": role,
    }

    final_state = build_graph().invoke(state)
    validation = final_state.get("validation") or {}
    latency_ms = round((time.perf_counter() - start_time) * 1000, 2)

    score = 0
    if "score" in validation:
        score = validation["score"]
    elif validation.get("safe"):
        score = 100

    risk_level = validation.get("risk_level", "LOW")

    if validation.get("requires_human"):
        # Always record who submitted the run; it drives ownership and separation-of-duties checks.
        run_id = save_run({**final_state, "user_id": user_id, "role": role})
        logger.info("HITL triggered: run %s | confidence %s%% | risk %s | %sms | %s",
                    run_id, score, risk_level, latency_ms, validation.get("reason"))
        log_audit_event(
            run_id=run_id, user_id=user_id, role=role, user_query=req.query,
            generated_sql=final_state.get("sql"), confidence_score=score,
            execution_status="requires_human", ingress_channel="WEB_UI",
            client_agent="sentinel-agent", risk_level=risk_level,
            execution_time_ms=latency_ms, masked_rows_count=0
        )
        return QueryResponse(
            status="requires_human", run_id=run_id, sql=final_state.get("sql"),
            reason=validation.get("reason"), confidence_score=score, latency_ms=latency_ms
        )

    if not validation.get("safe"):
        # `error` means the system could not complete the request (model down, database error);
        # it is reported and audited separately from a policy rejection.
        is_error = bool(validation.get("error"))
        logger.info("Query %s: confidence %s%% | risk %s | %sms | %s",
                    "failed" if is_error else "rejected", score, risk_level, latency_ms, validation.get("reason"))
        log_audit_event(
            run_id=None, user_id=user_id, role=role, user_query=req.query,
            generated_sql=final_state.get("sql"), confidence_score=score,
            execution_status="error" if is_error else "rejected", ingress_channel="WEB_UI",
            client_agent="sentinel-agent", risk_level=risk_level if is_error else "HIGH",
            execution_time_ms=latency_ms, masked_rows_count=0
        )
        return QueryResponse(
            status="error" if is_error else "rejected", reason=validation.get("reason"),
            confidence_score=score, latency_ms=latency_ms
        )

    results = final_state.get("results")
    rows_count = _rows_in(results)

    # Populate the semantic cache on a successful completed query
    if results is not None:
        semantic_cache.set(query=req.query, sql=final_state.get("sql"), results=results, role=role, confidence_score=score)

    logger.info("Completed: %d rows | confidence %s%% | risk %s | %sms", rows_count, score, risk_level, latency_ms)
    log_audit_event(
        run_id=None, user_id=user_id, role=role, user_query=req.query,
        generated_sql=final_state.get("sql"), confidence_score=score,
        execution_status="completed", ingress_channel="WEB_UI",
        client_agent="sentinel-agent", risk_level="LOW",
        execution_time_ms=latency_ms, masked_rows_count=rows_count
    )
    return QueryResponse(
        status="completed", sql=final_state.get("sql"), results=results,
        confidence_score=score, cached=False, latency_ms=latency_ms
    )


@router.get("/query/status/{run_id}")
def check_query_status_endpoint(run_id: str, actor: Actor = Depends(get_actor)):
    try:
        state = get_run(run_id)
        # Only the submitter or an admin may read a run; everyone else sees "not found".
        if actor.role != ROLE_ADMIN and state.get("user_id") != actor.user_id:
            raise KeyError(run_id)
        return {
            "run_id": run_id,
            "status": state.get("status", "pending"),
            "sql": state.get("sql"),
            "results": state.get("results"),
            "reason": state.get("reason"),
        }
    except KeyError:
        raise HTTPException(status_code=404, detail="Run ID not found or expired")
