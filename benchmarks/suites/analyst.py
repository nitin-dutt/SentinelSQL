"""
Analyst chat suite (live model): do follow-ups and clarifying questions move a number?

  follow-ups   Each conversation's first question and its follow-ups are sent to POST /chat (with memory). The SAME
               follow-up text is also sent to POST /query on its own (no memory), which is what the product did before the
               chat existed. Both are scored by execution against the gold query for that turn. The difference is the
               value of the conversation memory; if it is small, the feature is not earning its extra model call.
  clarifying   Labelled ambiguous and clear questions are sent as the first message of a new conversation. Reported:
               how many ambiguous questions got a clarifying question (recall), how many clear ones were answered
               without one (specificity), and the extra planner latency on clear questions.
  exploration  (ANALYST_BENCH_EXPLORE=1) the follow-up arm is repeated with ANALYST_EXPLORE=true to see whether letting
               the agent look at the database first changes accuracy, and how many model calls it adds.

Every answer still goes through the policy gate; a turn that is blocked or held for review counts as not answered.
Responses are cached, so an interrupted run resumes. Result: benchmarks/results/analyst.json.
"""

import os
import time
from typing import Any, Dict, List, Optional

from benchmarks.lib.meta import run_metadata
from benchmarks.lib.sqlcompare import execution_match
from benchmarks.lib.stats import mcnemar_exact, proportion
from benchmarks.suites.common import EMBEDDING_CACHE, EMPLOYEE, LLM_CACHE, progress, save_result, workspace

DATA_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "analyst_conversations.py")


def _score(db_file: str, gold_sql: str, body: Dict[str, Any]) -> Dict[str, Any]:
    out = {"status": body.get("status"), "sql": body.get("sql"), "correct": False}
    if body.get("status") == "completed" and body.get("sql"):
        try:
            out["correct"], out["error"] = execution_match(db_file, gold_sql, body["sql"])
        except Exception as e:
            out["error"] = f"{type(e).__name__}: {str(e)[:80]}"
    return out


def _post(client, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    started = time.perf_counter()
    r = client.post(path, json=payload, headers=EMPLOYEE)
    body = r.json() if r.status_code == 200 else {"status": f"http_{r.status_code}", "reason": r.text[:120]}
    body["latency_s"] = round(time.perf_counter() - started, 2)
    return body


def run_followups(client, db_file: str, conversations, label: str) -> List[Dict[str, Any]]:
    records, started = [], time.time()
    for n, (cid, turns) in enumerate(conversations, 1):
        progress(f"analyst-{label}", n, len(conversations), started)
        conversation_id = None
        for index, (message, gold) in enumerate(turns):
            chat = _post(client, "/chat", {"message": message, **({"conversation_id": conversation_id} if conversation_id else {})})
            conversation_id = chat.get("conversation_id") or conversation_id
            record = {"conversation": cid, "turn": index + 1, "is_follow_up": index > 0, "message": message, "gold": gold,
                      "with_memory": {**_score(db_file, gold, chat), "standalone_question": chat.get("standalone_question"),
                                      "asked_clarification": chat.get("status") == "needs_clarification",
                                      "agent_steps": len(chat.get("agent_steps") or []), "latency_s": chat["latency_s"]}}
            if index > 0 and label == "memory":
                alone = _post(client, "/query", {"query": message})          # the same words with no conversation
                record["no_memory"] = {**_score(db_file, gold, alone), "latency_s": alone["latency_s"]}
            records.append(record)
    return records


def summarise_followups(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    first = [r for r in records if not r["is_follow_up"]]
    follow = [r for r in records if r["is_follow_up"]]
    paired = [r for r in follow if "no_memory" in r]
    b = sum(1 for r in paired if r["with_memory"]["correct"] and not r["no_memory"]["correct"])
    c = sum(1 for r in paired if r["no_memory"]["correct"] and not r["with_memory"]["correct"])
    out = {
        "first_turns_correct": proportion(sum(r["with_memory"]["correct"] for r in first), len(first)),
        "follow_ups_correct_with_memory": proportion(sum(r["with_memory"]["correct"] for r in follow), len(follow)),
        "follow_ups_where_a_clarifying_question_was_asked": sum(r["with_memory"]["asked_clarification"] for r in follow),
        "follow_up_outcomes": {s: sum(r["with_memory"]["status"] == s for r in follow) for s in sorted({str(r["with_memory"]["status"]) for r in follow})},
    }
    if paired:
        out["follow_ups_correct_without_memory"] = proportion(sum(r["no_memory"]["correct"] for r in paired), len(paired))
        out["paired"] = {"only_with_memory_correct": b, "only_without_memory_correct": c, "mcnemar_p": round(mcnemar_exact(b, c), 5)}
    return out


def run_clarify(client, items) -> Dict[str, Any]:
    records, started = [], time.time()
    for n, (qid, question, should_ask, why) in enumerate(items, 1):
        progress("analyst-clarify", n, len(items), started)
        body = _post(client, "/chat", {"message": question})
        records.append({"id": qid, "question": question, "should_ask": should_ask, "why": why, "status": body.get("status"),
                        "asked": body.get("status") == "needs_clarification",
                        "clarifying_question": (body.get("clarification") or {}).get("question"), "latency_s": body["latency_s"]})
    ambiguous = [r for r in records if r["should_ask"]]
    clear = [r for r in records if not r["should_ask"]]
    asked = [r for r in records if r["asked"]]
    return {
        "asked_when_ambiguous_recall": proportion(sum(r["asked"] for r in ambiguous), len(ambiguous)),
        "answered_directly_when_clear": proportion(sum(not r["asked"] for r in clear), len(clear)),
        "precision_of_asking": proportion(sum(r["should_ask"] for r in asked), len(asked)),
        "wrongly_asked": [{k: r[k] for k in ("id", "question", "clarifying_question")} for r in clear if r["asked"]],
        "missed": [{k: r[k] for k in ("id", "question", "status")} for r in ambiguous if not r["asked"]],
        "records": records,
    }


def run(llm: bool = True, limit: Optional[int] = None) -> Dict[str, Any]:
    if not llm:
        print("analyst: needs a live model; nothing to do with --no-llm")
        return {}
    from fastapi.testclient import TestClient
    from app.main import app
    from app.rag.vector_store import EmbeddingUnavailableError
    from benchmarks.data.analyst_conversations import CLARIFY, CONVERSATIONS
    from benchmarks.lib.llm import DailyQuotaExceeded, embedding_harness, harness, run_tag

    conversations = CONVERSATIONS[:limit] if limit else CONVERSATIONS
    clarify_items = CLARIFY[:limit] if limit else CLARIFY
    saved = {k: os.environ.get(k) for k in ("ANALYST_CLARIFY", "ANALYST_EXPLORE")}
    started, status, usage = time.time(), "complete", {}
    followups, explore_records, clarify = [], [], None
    try:
        with workspace("analyst", semantic_cache=False) as db_file:
            try:
                with harness(LLM_CACHE) as u, embedding_harness(EMBEDDING_CACHE) as eu, TestClient(app) as client:
                    os.environ["ANALYST_CLARIFY"], os.environ["ANALYST_EXPLORE"] = "true", "false"
                    followups = run_followups(client, db_file, conversations, "memory")
                    clarify = run_clarify(client, clarify_items)
                    if os.getenv("ANALYST_BENCH_EXPLORE", "") == "1":
                        os.environ["ANALYST_EXPLORE"] = "true"
                        with run_tag("explore"):
                            explore_records = run_followups(client, db_file, conversations, "explore")
                    usage = {"llm": u.snapshot(), "embeddings": eu.snapshot()}
            except (DailyQuotaExceeded, EmbeddingUnavailableError) as e:
                status = f"paused: {type(e).__name__} (progress is cached; re-run to resume)"
                print("  " + status, flush=True)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    result = {
        "meta": run_metadata([DATA_FILE], {"status": status, "conversations": len(conversations), "clarify_items": len(clarify_items),
                                           "duration_s": round(time.time() - started), "usage": usage}),
        "follow_ups": summarise_followups(followups) if followups and status == "complete" else None,
        "clarifying_questions": {k: v for k, v in clarify.items() if k != "records"} if clarify else None,
        "exploration": ({**summarise_followups(explore_records),
                         "mean_tool_steps_per_turn": round(sum(r["with_memory"]["agent_steps"] for r in explore_records) / len(explore_records), 2)}
                        if explore_records else None),
        "records": {"follow_ups": followups, "clarify": (clarify or {}).get("records", []), "exploration": explore_records},
    }
    if status != "complete":
        print("  not saved: a partial run is never written over a complete result")
        return result
    print("saved", save_result("analyst", result))
    return result
