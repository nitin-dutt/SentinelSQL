"""
Analyst chat: multi-turn questions, one clarifying question when needed, optional schema exploration.

POST /chat is POST /query with a memory. Every answer still comes from `answer_question` (cache, pipeline, policy gate,
audit), so the chat adds convenience and no new way to reach data. See app/answering.py and app/agents/analyst.py.
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

from app import demo
from app.agents import analyst, conversation_store
from app.agents.intent_agent import detect_destructive_intent
from app.answering import Pipeline, answer_question, get_pipeline
from app.api.schemas import QueryResponse
from app.auth import Actor, require_roles, ROLE_ADMIN, ROLE_EMPLOYEE
from app.governance import record
from app.observability.tracing import current_trace_id, set_attr, set_status, span, start_trace
from app.utils.logger import get_logger

logger = get_logger("sentinel.chat")
router = APIRouter()
_callers = require_roles(ROLE_EMPLOYEE, ROLE_ADMIN)


class ChatRequest(BaseModel):
    message: str
    conversation_id: Optional[str] = None

    @field_validator("message")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("The message must not be empty")
        return value


class ChatResponse(QueryResponse):
    # status adds "needs_clarification" to the values of QueryResponse
    conversation_id: str
    standalone_question: Optional[str] = None      # what the message was understood as (differs from it on a follow-up)
    clarification: Optional[Dict[str, Any]] = None  # {"question": str, "options": [str]} when status is needs_clarification
    agent_steps: List[Dict[str, Any]] = []          # schema-exploration tool calls, when ANALYST_EXPLORE is on


def _schema_for(question: str) -> str:
    from app.rag.schema_retriever import get_rag_schema_context
    try:
        return get_rag_schema_context(question)
    except Exception as e:                           # the pipeline reports schema problems properly; the planner just goes without
        logger.warning("Schema context unavailable for the planner: %s", type(e).__name__)
        return ""


def _glossary_for(question: str) -> str:
    try:
        from app.rag.data_dictionary import format_dictionary_context
        return format_dictionary_context(question, top_k=3) or ""
    except Exception:
        return ""


@router.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, actor: Actor = Depends(_callers), pipeline: Pipeline = Depends(get_pipeline)):
    demo.check_question_length(req.message)
    message = req.message.strip()

    if req.conversation_id:
        try:
            history = conversation_store.get_turns(req.conversation_id, actor.user_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Conversation not found or expired. Start a new one.")
        conversation_id = req.conversation_id
    else:
        conversation_id, history = conversation_store.create_conversation(actor.user_id), []

    with start_trace("WEB_CHAT", actor.user_id, actor.role, message):
        set_attr("conversation_id", conversation_id)
        set_attr("turn", len(history) + 1)

        # A plain command to change data never reaches the planner: the pipeline's own filter refuses and audits it.
        plan = analyst.Plan(standalone_question=message)
        schemas: Dict[str, str] = {}                 # schema text already retrieved in this request, by question

        def schema_for(text: str) -> str:
            if text not in schemas:
                schemas[text] = _schema_for(text)
            return schemas[text]
        if not detect_destructive_intent(message):
            pending = bool(history) and history[-1].get("status") == "needs_clarification"
            allow_clarify = analyst.clarify_enabled() and not pending
            if history or allow_clarify:
                demo.charge_llm_call()               # public demo only: the planner is a model call too
                with span("planner"):
                    plan = analyst.plan_turn(message, history, schema_for(message), _glossary_for(message), allow_clarify)
        question = plan.standalone_question
        set_attr("rewritten", question != message)

        if plan.needs_clarification:
            clarification = {"question": plan.clarifying_question, "options": plan.options}
            conversation_store.add_turn(conversation_id, message=message, standalone_question=question,
                                        status="needs_clarification", clarification=clarification)
            record(actor=actor, channel="WEB_CHAT", client_agent="sentinel-analyst", question=message, status="needs_clarification")
            set_status("needs_clarification")
            return ChatResponse(status="needs_clarification", conversation_id=conversation_id, standalone_question=question,
                                clarification=clarification, reason=plan.clarifying_question, trace_id=current_trace_id())

        notes, steps = "", []
        if analyst.explore_enabled() and not detect_destructive_intent(question):
            with span("explore"):
                notes, steps = analyst.explore(question, schema_for(question), actor)
            set_attr("explore_steps", len(steps))

        answer = answer_question(question, actor, channel="WEB_CHAT", client_agent="sentinel-analyst",
                                 extra_context=notes or None, pipeline=pipeline,
                                 # retrieved above for this same question (when it was): the pipeline does not retrieve it again
                                 schema_context=schemas.get(question) or None)
        results = answer.results or {}
        conversation_store.add_turn(conversation_id, message=message, standalone_question=question, status=answer.status,
                                    sql=answer.sql, columns=results.get("columns"),
                                    row_count=len(results.get("rows", [])) if answer.results else None, reason=answer.reason)
        return ChatResponse(**answer.model_dump(exclude={"trace_id"}), conversation_id=conversation_id,
                            standalone_question=question, agent_steps=steps, trace_id=current_trace_id())


@router.get("/chat/{conversation_id}")
def get_conversation(conversation_id: str, actor: Actor = Depends(_callers)):
    """The turns of one of the caller's own conversations (question, outcome, SQL and answer shape; never result rows)."""
    try:
        return {"conversation_id": conversation_id, "turns": conversation_store.get_turns(conversation_id, actor.user_id)}
    except KeyError:
        raise HTTPException(status_code=404, detail="Conversation not found or expired")


@router.delete("/chat/{conversation_id}")
def delete_conversation(conversation_id: str, actor: Actor = Depends(_callers)):
    if not conversation_store.delete_conversation(conversation_id, actor.user_id):
        raise HTTPException(status_code=404, detail="Conversation not found or expired")
    return {"status": "success"}
