"""
Human-in-the-Loop review decisions.

This router owns who may decide and when (admin only, decide once, separation of duties) and the reviewer's rejection.
Running an approved or edited query is `run` in app/governance.py with an `Approval`: the SQL is judged again, executed,
the review run is updated and the audit record names the approver.
"""

from fastapi import APIRouter, Depends, HTTPException

from app.api.schemas import HITLRequest, HITLResponse
from app.auth import Actor, require_roles, self_approval_allowed, ROLE_ADMIN
from app.governance import BLOCKED, COMPLETED, Approval, record, run
from app.hitl.web_store import get_run, update_run
from app.utils.logger import get_logger

logger = get_logger("sentinel.hitl")
router = APIRouter()

# Review decisions have always been recorded under these ingress columns.
_CHANNEL, _CLIENT_AGENT = "WEB_UI", "sentinel-internal"


@router.post("/hitl/decision", response_model=HITLResponse)
def hitl_decision(req: HITLRequest, actor: Actor = Depends(require_roles(ROLE_ADMIN))):
    approver_id = actor.user_id
    try:
        state = get_run(req.run_id)
    except KeyError:
        return HITLResponse(status="rejected", reason="Invalid or expired run_id")

    # The audit records of a review are filed under the person who asked, with the reviewer in `approved_by`.
    submitter_id = state.get("user_id")
    submitter = Actor(user_id=submitter_id or approver_id, role=state.get("role") or actor.role)

    def note(status: str) -> None:
        record(actor=submitter, channel=_CHANNEL, client_agent=_CLIENT_AGENT, question=state.get("user_query"), status=status,
               sql=state.get("sql"), score=0, risk_level="HIGH", run_id=req.run_id, approved_by=approver_id)

    # A run can be decided exactly once; re-approving would silently re-execute it.
    run_status = state.get("status", "pending")
    if run_status != "pending":
        return HITLResponse(status="rejected", reason=f"Run already decided (status: {run_status})")

    # Separation of duties: the person who asked may not approve (or edit and run) their own request.
    # Withdrawing it by rejecting is always allowed: it can only stop a query, never run one.
    if req.decision != "reject" and submitter_id and submitter_id == approver_id and not self_approval_allowed():
        note("hitl_self_approval_blocked")
        raise HTTPException(status_code=403, detail="You cannot approve a query you submitted; another administrator must review it.")

    # Human rejects the query
    if req.decision == "reject":
        update_run(req.run_id, status="rejected", reason="Query rejected by human reviewer")
        note("hitl_rejected")
        return HITLResponse(status="rejected", reason="Query rejected by human reviewer")

    # Human edits SQL
    if req.decision == "edit":
        if not req.edited_sql:
            return HITLResponse(status="rejected", reason="Edited SQL must be provided")
        sql_to_execute = req.edited_sql
    else:
        # Human approves original SQL
        sql_to_execute = state.get("sql")

    # Approval clears the human-review requirement, never the hard rules: whatever the reviewer approves or types still
    # has to pass the policy (single SELECT, no DDL/DML, no Cartesian joins, LIMIT enforcement), and the sanitized SQL is
    # what runs. The reviewer sees salary-class values; the copy stored for the submitter has the normal masking.
    outcome = run(sql_to_execute or "", actor=submitter, channel=_CHANNEL, client_agent=_CLIENT_AGENT,
                  question=state.get("user_query") or "", approval=Approval(run_id=req.run_id, approved_by=approver_id))
    if outcome.status == BLOCKED:
        return HITLResponse(status="rejected", reason=f"Blocked by policy: {outcome.reason}")
    if outcome.status != COMPLETED:
        return HITLResponse(status="rejected", reason=outcome.reason)
    sql_to_execute = outcome.sql

    # Explicit Golden Query Template opt-in (admin-only route; SQL already passed the policy gate in `run`)
    if req.save_as_template and state.get("user_query") and sql_to_execute:
        from app.rag.golden_store import save_golden_query
        try:
            save_golden_query(nl_query=state.get("user_query"), sql=sql_to_execute, created_by=approver_id)
            logger.info("Saved verified template: %r -> %r", state.get("user_query"), sql_to_execute)
            from app.cache.semantic_cache import get_semantic_cache
            get_semantic_cache().clear()  # a new template can change how similar questions are answered
        except Exception as e:
            logger.warning("Failed to save golden template: %s", e)

    return HITLResponse(status="completed", sql=sql_to_execute, results=outcome.results)
