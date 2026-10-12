"""Human-in-the-Loop review decisions."""

import time

from fastapi import APIRouter, Depends, HTTPException

from app.api.schemas import HITLRequest, HITLResponse
from app.auth import Actor, require_roles, self_approval_allowed, ROLE_ADMIN
from app.db.database import execute_readonly_query
from app.hitl.web_store import get_run, update_run
from app.utils.audit_logger import log_audit_event
from app.utils.logger import get_logger
from app.utils.sql_parser import parse_and_validate_ast

logger = get_logger("sentinel.hitl")
router = APIRouter()


@router.post("/hitl/decision", response_model=HITLResponse)
def hitl_decision(req: HITLRequest, actor: Actor = Depends(require_roles(ROLE_ADMIN))):
    approver_id = actor.user_id
    role = actor.role
    start_time = time.perf_counter()
    try:
        state = get_run(req.run_id)
    except KeyError:
        return HITLResponse(status="rejected", reason="Invalid or expired run_id")

    submitter_id = state.get("user_id")
    audit_user = submitter_id or approver_id

    # A run can be decided exactly once; re-approving would silently re-execute it.
    run_status = state.get("status", "pending")
    if run_status != "pending":
        return HITLResponse(status="rejected", reason=f"Run already decided (status: {run_status})")

    # Separation of duties: the person who asked may not approve their own request.
    if submitter_id and submitter_id == approver_id and not self_approval_allowed():
        log_audit_event(
            run_id=req.run_id, user_id=submitter_id, role=role, user_query=state.get("user_query"),
            generated_sql=state.get("sql"), confidence_score=0, execution_status="hitl_self_approval_blocked",
            approved_by=approver_id, risk_level="HIGH"
        )
        raise HTTPException(status_code=403, detail="You cannot approve a query you submitted; another administrator must review it.")

    # Human rejects the query
    if req.decision == "reject":
        update_run(req.run_id, status="rejected", reason="Query rejected by human reviewer")
        log_audit_event(
            run_id=req.run_id, user_id=audit_user, role=role, user_query=state.get("user_query"),
            generated_sql=state.get("sql"), confidence_score=0, execution_status="hitl_rejected",
            approved_by=approver_id, risk_level="HIGH"
        )
        return HITLResponse(status="rejected", reason="Query rejected by human reviewer")

    # Human edits SQL
    if req.decision == "edit":
        if not req.edited_sql:
            return HITLResponse(status="rejected", reason="Edited SQL must be provided")
        sql_to_execute = req.edited_sql
    else:
        # Human approves original SQL
        sql_to_execute = state.get("sql")

    # Approval clears the human-review requirement, never the hard rules: whatever the
    # reviewer approves or types still has to pass the AST policy (single SELECT, no
    # DDL/DML, no Cartesian joins, LIMIT enforcement). The validated, sanitized SQL is what runs.
    ast_res = parse_and_validate_ast(sql_to_execute or "")
    if not ast_res.is_valid:
        update_run(req.run_id, status="failed", reason=ast_res.reason)
        log_audit_event(
            run_id=req.run_id, user_id=audit_user, role=role, user_query=state.get("user_query"),
            generated_sql=sql_to_execute, confidence_score=0, execution_status="hitl_blocked_by_policy",
            approved_by=approver_id, risk_level="HIGH"
        )
        return HITLResponse(status="rejected", reason=f"Blocked by policy: {ast_res.reason}")
    sql_to_execute = ast_res.sanitized_sql

    try:
        results = execute_readonly_query(sql_to_execute)
    except Exception as e:
        update_run(req.run_id, status="failed", reason=str(e))
        log_audit_event(
            run_id=req.run_id, user_id=audit_user, role=role, user_query=state.get("user_query"),
            generated_sql=sql_to_execute, confidence_score=0, execution_status="hitl_execution_failed",
            approved_by=approver_id, risk_level="HIGH"
        )
        return HITLResponse(status="rejected", reason=str(e))

    latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
    rows_count = len(results.get("rows", [])) if results and isinstance(results, dict) else 0

    # Persist the SQL that actually ran (edited and/or LIMIT-sanitized) so pollers see it.
    update_run(req.run_id, status="completed", results=results, sql=sql_to_execute)
    log_audit_event(
        run_id=req.run_id, user_id=audit_user, role=role, user_query=state.get("user_query"),
        generated_sql=sql_to_execute, confidence_score=100, execution_status="hitl_approved",
        approved_by=approver_id, execution_time_ms=latency_ms, masked_rows_count=rows_count,
        risk_level="HIGH"
    )

    # Explicit Golden Query Template opt-in (admin-only route; SQL already passed the AST gate above)
    if req.save_as_template and state.get("user_query") and sql_to_execute:
        from app.rag.golden_store import save_golden_query
        try:
            save_golden_query(nl_query=state.get("user_query"), sql=sql_to_execute, created_by=approver_id)
            logger.info("Saved verified template: %r -> %r", state.get("user_query"), sql_to_execute)
            from app.cache.semantic_cache import get_semantic_cache
            get_semantic_cache().clear()  # a new template can change how similar questions are answered
        except Exception as e:
            logger.warning("Failed to save golden template: %s", e)

    return HITLResponse(status="completed", sql=sql_to_execute, results=results)
