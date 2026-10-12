"""
Architectural ablation on the demo company database (21 tables).

Question answered: which of SentinelSQL's context components actually improve generated SQL, by how much,
and for which kinds of question?

Configurations (the product's own pipeline, with one component switched on at a time):
  baseline   no schema retrieval (the whole 21-table schema in the prompt), no glossary, no golden memory
  +rag       Schema RAG (top-3 tables + foreign-key closure)
  +glossary  business glossary injection
  +golden    golden-query memory (12 verified question/SQL pairs for *different* questions)
  full       all three

Every configuration answers the same 30 questions, twice each (independent samples: the model is not
deterministic), through the production intent agent and SQL generator, using the real glossary and golden
stores and the real retriever. Only the components are toggled, by replacing the function that supplies
that block of the prompt. Generated SQL is executed on the raw database (no masking, which would hide
differences) and compared with the gold result by the Spider-style comparator.

Statistics: accuracy per configuration with Wilson intervals over question-run trials, and a paired bootstrap
over questions for each configuration versus baseline (questions are resampled, not trials, so the
interval is honest about there being only 30 questions). Subgroups: glossary-dependent questions,
three-or-more-table joins, and the rest.
"""

import json
import os
import sqlite3
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional
from unittest.mock import patch

from benchmarks.data.domain_qa import GOLDEN_EXAMPLES, QUESTIONS
from benchmarks.lib.meta import run_metadata
from benchmarks.lib.sqlcompare import gold_has_order_by, rows_match
from benchmarks.lib.stats import paired_bootstrap_diff, proportion
from benchmarks.suites.common import EMBEDDING_CACHE, LLM_CACHE, progress, save_result, workspace
from benchmarks.suites.spider import strip_limit

REPEATS = int(os.getenv("ABLATION_REPEATS", "2"))
CONFIGS = [
    {"id": "baseline", "rag": False, "glossary": False, "golden": False},
    {"id": "+rag", "rag": True, "glossary": False, "golden": False},
    {"id": "+glossary", "rag": False, "glossary": True, "golden": False},
    {"id": "+golden", "rag": False, "glossary": False, "golden": True},
    {"id": "full", "rag": True, "glossary": True, "golden": True},
]


def run_sql(con: sqlite3.Connection, sql: str):
    try:
        return True, [tuple(r) for r in con.execute(sql).fetchall()], ""
    except Exception as e:
        return False, [], f"{type(e).__name__}: {e}"


def generate(question: str, config: Dict[str, Any], full_schema: str) -> Dict[str, Any]:
    """One pass of the product pipeline up to (and including) SQL generation, with components toggled."""
    from app.graph import workflow
    from app.rag.schema_retriever import get_rag_schema_context

    state: Dict[str, Any] = {"user_query": question, "intent": None, "sql": None, "validation": None, "results": None}
    patches = [
        patch("app.graph.workflow.get_rag_schema_context", get_rag_schema_context if config["rag"] else (lambda q, *a, **k: full_schema)),
    ]
    if not config["glossary"]:
        patches.append(patch("app.rag.data_dictionary.format_dictionary_context", lambda *a, **k: ""))
    if not config["golden"]:
        patches.append(patch("app.rag.golden_store.format_few_shot_examples", lambda *a, **k: ""))
    for p in patches:
        p.start()
    try:
        state = workflow.intent_node(state)
        state = workflow.sql_generation_node(state)
    finally:
        for p in patches:
            p.stop()
    return state


def run(llm: bool = True, limit: Optional[int] = None) -> Dict[str, Any]:
    from app.rag.vector_store import EmbeddingUnavailableError
    from benchmarks.lib.llm import DailyQuotaExceeded, embedding_harness, harness, run_tag

    questions = QUESTIONS[:limit] if limit else QUESTIONS
    trials: List[Dict[str, Any]] = []
    status, usage = "complete", {}
    started = time.time()

    with workspace("ablation") as db_file:
        from app.rag.data_dictionary import get_all_terms
        from app.rag.golden_store import save_golden_query
        from app.utils.schemacontext import introspect_schema

        if not llm:
            return {}
        con = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
        try:
            with harness(LLM_CACHE) as llm_usage, embedding_harness(EMBEDDING_CACHE) as emb_usage:
                get_all_terms()                                                  # seeds + indexes the glossary
                for question, sql in GOLDEN_EXAMPLES:
                    save_golden_query(question, sql, created_by="bench")
                full_schema = introspect_schema()["formatted_context"]
                gold = {}
                for qid, tier, question, gold_sql, needs in questions:
                    ok, rows, err = run_sql(con, gold_sql)
                    assert ok and rows, f"{qid}: gold query must run and return rows ({err})"
                    gold[qid] = rows

                total = len(CONFIGS) * len(questions) * REPEATS
                done = 0
                for rep in range(1, REPEATS + 1):
                    for config in CONFIGS:
                        with run_tag(f"abl-r{rep}"):
                            for qid, tier, question, gold_sql, needs in questions:
                                done += 1
                                progress("ablation", done, total, started)
                                state = generate(question, config, full_schema)
                                sql = state.get("sql")
                                rec = {"config": config["id"], "rep": rep, "id": qid, "tier": tier, "needs": sorted(needs),
                                       "question": question, "sql": sql, "reason": (state.get("validation") or {}).get("reason"),
                                       "correct": False, "correct_strict": False, "error": None}
                                if sql:
                                    s = sql.rstrip(";")
                                    ok, rows, err = run_sql(con, s)
                                    if ok:
                                        rec["correct_strict"] = rows_match(gold[qid], rows, gold_has_order_by(gold_sql))
                                        # logic score: ignore the row cap the generator adds (the gold queries have none)
                                        ok2, rows2, _ = run_sql(con, strip_limit(s))
                                        rec["correct"] = ok2 and rows_match(gold[qid], rows2, gold_has_order_by(gold_sql))
                                    else:
                                        rec["error"] = err[:100]
                                else:
                                    rec["error"] = "no_sql"
                                trials.append(rec)
                usage = {"llm": llm_usage.snapshot(), "embeddings": emb_usage.snapshot()}
        except (DailyQuotaExceeded, EmbeddingUnavailableError) as e:
            status = f"paused: {type(e).__name__} (progress cached; re-run to resume)"
            print("  " + status, flush=True)
        finally:
            con.close()

    result = {"meta": run_metadata([os.path.join(os.path.dirname(__file__), "..", "data", "domain_qa.py")],
                                   {"status": status, "repeats": REPEATS, "n_questions": len(questions), "usage": usage,
                                    "duration_s": round(time.time() - started)}),
              "analysis": analyse(trials, questions) if status == "complete" and trials else None, "trials": trials}
    if status == "complete":
        print("saved", save_result("ablation", result))
    return result


# ---------------------------------------------------------------------------- analysis

def per_question_rate(trials: List[Dict[str, Any]], config: str, key: str = "correct") -> Dict[str, float]:
    by_q: Dict[str, List[bool]] = defaultdict(list)
    for t in trials:
        if t["config"] == config:
            by_q[t["id"]].append(t[key])
    return {q: sum(v) / len(v) for q, v in by_q.items()}


def analyse(trials: List[Dict[str, Any]], questions) -> Dict[str, Any]:
    ids = [q[0] for q in questions]
    needs = {q[0]: q[4] for q in questions}
    groups = {
        "all": ids,
        "glossary_questions": [i for i in ids if "glossary" in needs[i]],
        "multi_table_joins": [i for i in ids if "joins3" in needs[i]],
        "other": [i for i in ids if not needs[i]],
    }
    base = per_question_rate(trials, "baseline")
    out: Dict[str, Any] = {"by_config": {}, "vs_baseline": {}}
    for cfg in (c["id"] for c in CONFIGS):
        cell = [t for t in trials if t["config"] == cfg]
        rate = per_question_rate(trials, cfg)
        out["by_config"][cfg] = {
            "trials": proportion(sum(t["correct"] for t in cell), len(cell)),
            "strict_with_row_cap": proportion(sum(t["correct_strict"] for t in cell), len(cell)),
            "by_group": {g: proportion(sum(t["correct"] for t in cell if t["id"] in members), len([t for t in cell if t["id"] in members]))
                         for g, members in groups.items()},
            "failures": [{"id": t["id"], "rep": t["rep"], "question": t["question"], "sql": t["sql"], "error": t["error"]}
                         for t in cell if not t["correct"]][:12],
        }
        if cfg != "baseline":
            a = [base[i] for i in ids]
            b = [rate[i] for i in ids]
            out["vs_baseline"][cfg] = {"all_questions": paired_bootstrap_diff(a, b)}
            for g in ("glossary_questions", "multi_table_joins", "other"):
                if groups[g]:
                    out["vs_baseline"][cfg][g] = paired_bootstrap_diff([base[i] for i in groups[g]], [rate[i] for i in groups[g]])
    # the questions that flip between baseline and full, as evidence of what each component actually fixed
    full = per_question_rate(trials, "full")
    out["fixed_by_full_pipeline"] = [i for i in ids if base[i] < 0.5 <= full[i]]
    out["broken_by_full_pipeline"] = [i for i in ids if full[i] < 0.5 <= base[i]]
    return out
