"""Natural-language query endpoint and run-status polling. The answer itself is `answer_question` in app/answering.py."""

from fastapi import APIRouter, Depends, HTTPException

from app import demo
from app.answering import Pipeline, answer_question, get_pipeline
from app.api.schemas import QueryRequest, QueryResponse
from app.auth import Actor, get_actor, require_roles, ROLE_ADMIN, ROLE_EMPLOYEE
from app.hitl.web_store import get_run
from app.observability.tracing import current_trace_id, start_trace

router = APIRouter()


@router.post("/query", response_model=QueryResponse)
def run_query(req: QueryRequest, actor: Actor = Depends(require_roles(ROLE_EMPLOYEE, ROLE_ADMIN)),
              pipeline: Pipeline = Depends(get_pipeline)):
    demo.check_question_length(req.query)
    with start_trace("WEB_UI", actor.user_id, actor.role, req.query):
        response = answer_question(req.query, actor, pipeline=pipeline)
        response.trace_id = current_trace_id()
        return response


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
