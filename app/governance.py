"""
The policy gate and governed execution: one verdict type, one function that runs SQL under the controls.

`run(sql, actor=..., channel=..., question=..., client_agent=...)` is the whole governed sequence for one SQL statement:
judge it, hold it for human review if the policy says so, otherwise execute it read-only with masking, and write exactly
one audit record. It returns an `Outcome` whose `status` is `completed`, `held`, `blocked` or `error`. `error` is an
infrastructure failure (the database rejected or could not run the statement); it is not a policy decision.
Rule: every statement sent to the database goes through `run`, so every one has an audit record.

`record(...)` writes the audit record for the few events that have no SQL execution to hang it on (a cache hit, a
request refused before any SQL existed, a clarifying question, a reviewer's rejection). `run` and `record` are the
only callers of the audit logger.

`judge(sql)` asks the AST policy engine (`parse_and_validate_ast` in app/utils/sql_parser.py) about one SQL statement
and returns a `Verdict` whose `outcome` is exactly one of:

  * `allowed`: the sanitized SQL may run without a person looking at it;
  * `held`:    the SQL is acceptable but needs human review before it runs;
  * `blocked`: the policy refuses it.

Every ingress (the web pipeline, MCP, the analyst tool, reviewer edits, candidate voting, the benchmarks) calls `judge`,
so they cannot drift apart. A verdict is a policy decision only: a model or database failure is not a verdict.

Callers of `run`: the web pipeline (app/graph/workflow.py, including candidate voting), MCP (app/mcp_server.py),
reviewer decisions (app/routers/hitl.py) and the analyst's sample-values tool (app/agents/analyst.py). The Spider
benchmark runs the web pipeline with its own `executor`, so it goes through `run` too.

Inside the app a verdict is always the typed object. `Verdict.as_dict()` exists only to store it on a review run
(the review queue shows its reason).
"""

import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, List, Optional

from app.db.database import QueryTimeoutError, execute_readonly_query, execute_readonly_query_for_review
from app.hitl.web_store import save_run, update_run
from app.observability.tracing import span
from app.utils.audit_logger import log_audit_event
from app.utils.logger import get_logger
from app.utils.sql_parser import parse_and_validate_ast

logger = get_logger("sentinel.governance")

ALLOWED = "allowed"
HELD = "held"
BLOCKED = "blocked"


@dataclass(frozen=True)
class Verdict:
    outcome: str                                  # ALLOWED | HELD | BLOCKED
    sanitized_sql: str = ""                       # the SQL to run (LIMIT injected or capped); the input SQL when blocked
    score: int = 100                              # the policy engine's confidence, 0-100
    risk_level: str = "LOW"                       # LOW | MEDIUM | HIGH
    reason: str = ""
    tables: List[str] = field(default_factory=list)
    breakdown: List[str] = field(default_factory=list)   # the scoring rules that fired
    limit_applied: Optional[int] = None           # the LIMIT the gate added or lowered to (None: the query's own LIMIT stands)

    def __post_init__(self):
        if self.outcome not in (ALLOWED, HELD, BLOCKED):
            raise ValueError(f"Unknown verdict outcome: {self.outcome!r}")

    @property
    def allowed(self) -> bool:
        return self.outcome == ALLOWED

    @property
    def held(self) -> bool:
        return self.outcome == HELD

    @property
    def blocked(self) -> bool:
        return self.outcome == BLOCKED

    def as_dict(self) -> Dict[str, Any]:
        """The JSON form stored on a review run (app/hitl/web_store.py); the review queue shows `reason`."""
        return {
            "outcome": self.outcome,
            "reason": self.reason,
            "risk_level": self.risk_level,
            "score": self.score,
            "sanitized_sql": self.sanitized_sql,
            "tables": list(self.tables),
            "breakdown": list(self.breakdown),
        }


def judge(sql: str, *, question: Optional[str] = None) -> Verdict:
    """
    The policy decision for one SQL statement. Deterministic: the same SQL always gets the same verdict.

    `question` is used only by the experimental VALIDATION_MODE=llm grading (see below) and goes away with it.
    """
    res = parse_and_validate_ast(sql)
    verdict = Verdict(
        outcome=BLOCKED if not res.is_valid else HELD if res.requires_human else ALLOWED,
        sanitized_sql=res.sanitized_sql,
        score=res.confidence_score,
        risk_level=res.risk_level,
        reason=res.reason,
        tables=list(res.tables_accessed),
        breakdown=list(res.score_breakdown),
        limit_applied=res.limit_applied,
    )
    logger.info("[policy] %s | confidence %s%% | risk %s | tables %s | joins %s | has limit %s",
                verdict.outcome, verdict.score, verdict.risk_level, verdict.tables, res.join_count, res.has_limit)
    for rule in verdict.breakdown:
        logger.info("Rule: %s", rule)
    if not verdict.allowed:
        logger.info("[policy] %s: %s", verdict.outcome, verdict.reason)

    # ---- EXPERIMENTAL, off by default (VALIDATION_MODE=llm). Kept only until the post-redesign benchmark run.
    # To remove: delete these four lines, the `question` parameter, `replace` from the imports above,
    # app/experimental_llm_grading.py, its tests in tests/test_governance.py and VALIDATION_MODE in .env.example.
    from app.experimental_llm_grading import llm_grade, llm_grading_enabled
    if verdict.allowed and llm_grading_enabled():
        verdict = replace(verdict, **llm_grade(sql, question or ""))
    # ---- end of experimental section

    return verdict


# ---------------------------------------------------------------------------------------------- governed execution

COMPLETED = "completed"
ERROR = "error"
# Outcome.status also uses HELD and BLOCKED from above.

# What the audit log calls each outcome. The first set is what every ingress writes; the second is a reviewer's decision.
_AUDIT_STATUS = {COMPLETED: "completed", HELD: "requires_human", BLOCKED: "rejected", ERROR: "error"}
_REVIEW_AUDIT_STATUS = {COMPLETED: "hitl_approved", BLOCKED: "hitl_blocked_by_policy", ERROR: "hitl_execution_failed"}

# Database errors that rewriting the SQL cannot fix: permissions, read-only enforcement, timeouts.
_NOT_REPAIRABLE = ("permission denied", "access denied", "read-only", "read only", "readonly", "statement timeout", "interrupted")


def is_repairable(error: Exception) -> bool:
    """True for a statement the database rejected (unknown column, syntax, ambiguity), not for timeouts, connection loss or permissions."""
    from sqlalchemy.exc import DBAPIError
    if isinstance(error, QueryTimeoutError) or not isinstance(error, DBAPIError) or getattr(error, "connection_invalidated", False):
        return False
    text = str(error).lower()
    return not any(marker in text for marker in _NOT_REPAIRABLE)


Executor = Callable[[str], Dict[str, Any]]      # sanitized SQL in, masked {"columns", "rows"} out; raises on failure


def _mark_if_cut_by_gate(results: Optional[Dict[str, Any]], verdict: Verdict) -> Optional[Dict[str, Any]]:
    """
    When the gate added the LIMIT (the query had none) and the answer has exactly that many rows, the answer may have been cut
    by the gate, not by the question: say so, so the user is not shown a silently short list as if it were complete.
    A query that asked for its own LIMIT (a "top 5") is never marked. Exactly-N rows is indistinguishable from "more existed".
    """
    if not isinstance(results, dict) or not verdict.limit_applied:
        return results
    if len(results.get("rows", [])) >= verdict.limit_applied:
        return {**results, "truncated": True, "row_limit": verdict.limit_applied}
    return results


@dataclass(frozen=True)
class Approval:
    """A reviewer's decision to run a held query: which review run, and who approved it."""
    run_id: str
    approved_by: str


@dataclass(frozen=True)
class Outcome:
    status: str                              # COMPLETED | HELD | BLOCKED | ERROR
    sql: str                                 # completed / held / error: the sanitized SQL; blocked: the SQL as submitted
    verdict: Verdict
    results: Optional[Dict[str, Any]] = None # completed only: {"columns", "rows", "truncated"?}, already masked
    reason: str = ""                         # held / blocked: the policy's reason; error: the database's message
    run_id: Optional[str] = None             # the review run: created by a hold, or the one an approval decided
    repairable: bool = False                 # error only: the database rejected the statement and a rewrite could fix it
    elapsed_ms: float = 0.0

    @property
    def score(self) -> int:
        return self.verdict.score

    @property
    def risk_level(self) -> str:
        return self.verdict.risk_level

    @property
    def rows(self) -> int:
        return len(self.results.get("rows", [])) if isinstance(self.results, dict) else 0


def record(*, actor: Any, channel: str, client_agent: str, question: Optional[str], status: str, sql: Optional[str] = None,
           score: int = 0, risk_level: str = "LOW", run_id: Optional[str] = None, approved_by: Optional[str] = None,
           elapsed_ms: Optional[float] = None, rows: Optional[int] = None) -> None:
    """
    One audit record. `run` writes its own; call this only for an event with no SQL execution behind it
    (`status`: cached, rejected or error before any SQL existed, needs_clarification, hitl_rejected, ...).
    """
    log_audit_event(
        run_id=run_id, user_id=actor.user_id, role=actor.role, user_query=question, generated_sql=sql,
        confidence_score=score, execution_status=status, ingress_channel=channel, client_agent=client_agent,
        risk_level=risk_level, approved_by=approved_by, execution_time_ms=elapsed_ms, masked_rows_count=rows,
    )


def run(sql: str, *, actor: Any, channel: str, question: str, client_agent: str, when_held: str = "review",
        approval: Optional[Approval] = None, started: Optional[float] = None,
        executor: Optional[Executor] = None) -> Outcome:
    """
    The governed sequence for one SQL statement: policy verdict, review hold, read-only execution with masking, and
    exactly one audit record.

    `actor` is who is asking (anything with `user_id` and `role`, normally app.auth.Actor); `channel` and `client_agent`
    are the audit record's ingress columns; `question` is what the audit record and the review queue show as the request.

    `when_held` says what a `held` verdict becomes:
      * "review" (default): the run is saved for a human reviewer and the outcome is `held` with a `run_id`;
      * "refuse": the outcome is `blocked` and no review run is created (for callers with nobody to wait for a reviewer).

    `approval` is a reviewer's decision to run the SQL of a held run (`actor` is then the run's submitter). A `held`
    verdict executes, because the review is what it was waiting for; a `blocked` verdict is still refused, so approving
    or editing can never get past the hard rules. The reviewer's results (`Outcome.results`) keep salary-class values
    readable; the copy stored on the review run for the submitter has the normal masking. The review run is marked
    completed or failed, and the audit record names the approver.

    `started` is `time.perf_counter()` at the start of the request, when the audit record's time should cover more than
    this call (the web pipeline spends most of its time in the model before any SQL exists).

    `executor` is what runs an allowed statement: SQL in, `{"columns", "rows"}` out, already masked, raising on failure.
    The default is `execute_readonly_query` (the configured database). Everything around it stays the same: the verdict,
    the hold, the audit record. It exists so the pipeline can be pointed at another database without a copy of this
    sequence (the Spider benchmark does that). An approval always runs on the configured database, so the two cannot
    be combined.

    Never raises for a policy refusal or a database failure: those are `blocked` and `error` outcomes.
    """
    if when_held not in ("review", "refuse"):
        raise ValueError(f"Unknown when_held: {when_held!r}")
    if approval and executor:
        raise ValueError("An approval runs on the configured database; it cannot be given an executor")
    started = time.perf_counter() if started is None else started
    with span("gate"):
        verdict = judge(sql, question=question)

    run_id: Optional[str] = approval.run_id if approval else None
    results: Optional[Dict[str, Any]] = None
    stored_results: Optional[Dict[str, Any]] = None
    repairable = False
    reason = verdict.reason
    ran_sql = verdict.sanitized_sql or sql

    if verdict.blocked:
        status, ran_sql = BLOCKED, sql
    elif verdict.held and not approval and when_held == "refuse":
        status = BLOCKED
    elif verdict.held and not approval:
        status = HELD
        # The submitter is always recorded: it drives ownership and separation-of-duties checks on the review.
        run_id = save_run({"user_query": question, "sql": ran_sql, "user_id": actor.user_id, "role": actor.role,
                           "validation": verdict.as_dict()}, status="pending")
    else:
        try:
            with span("execution"):
                if approval:
                    results, stored_results = execute_readonly_query_for_review(ran_sql)
                else:
                    results = (executor or execute_readonly_query)(ran_sql)
            results = _mark_if_cut_by_gate(results, verdict)
            stored_results = _mark_if_cut_by_gate(stored_results, verdict)
            status = COMPLETED
        except Exception as e:
            logger.warning("Execution failed: %s: %s", type(e).__name__, e)
            status, reason, repairable = ERROR, str(e), is_repairable(e)

    outcome = Outcome(status=status, sql=ran_sql, verdict=verdict, results=results, reason=reason, run_id=run_id,
                      repairable=repairable, elapsed_ms=round((time.perf_counter() - started) * 1000, 2))
    if approval:
        # Pollers read the review run: it gets the SQL that actually ran (edited and/or LIMIT-sanitized) and the submitter's view.
        if status == COMPLETED:
            update_run(approval.run_id, status="completed", results=stored_results, sql=ran_sql)
        else:
            update_run(approval.run_id, status="failed", reason=reason)
    record(
        actor=actor, channel=channel, client_agent=client_agent, question=question, sql=ran_sql,
        status=(_REVIEW_AUDIT_STATUS if approval else _AUDIT_STATUS)[status], score=verdict.score,
        risk_level=verdict.risk_level, run_id=run_id, approved_by=approval.approved_by if approval else None,
        elapsed_ms=outcome.elapsed_ms, rows=outcome.rows,
    )
    return outcome
