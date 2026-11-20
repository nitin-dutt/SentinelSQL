"""
Text-to-SQL accuracy on the Yale Spider dev set (live model).

Two arms answer the SAME stratified sample of questions with the SAME model:

  RAW       zero-shot: the full CREATE TABLE schema + question -> SQL. This is what you get by pointing an
            LLM straight at a database. It is the baseline.
  GOVERNED  SentinelSQL's production pipeline itself (the LangGraph in app/graph/workflow.py): intent -> SQL
            generation -> governed run (policy gate, review hold, read-only execution with masking, audit record)
            -> repair. The pipeline is given two things from outside: the Spider schema text (so retrieval is
            skipped) and an executor for the Spider database file. Until phase 4 section 4 this arm was a
            hand-written copy of those steps; `run_governed` now fails (`PipelineBypassed`) if an answer has no
            governed run behind it. The domain glossary / golden-query memory are switched off: they describe the
            demo company, not the Spider databases. Audit rows and review holds go to benchmarks/.work/spider/.

Scoring is execution accuracy with the Spider-style comparator (benchmarks/lib/sqlcompare.py): same rows,
column order ignored, row order only if the gold query orders. Hardness tiers are Spider's official ones.

What the comparison is for: governance is not free. The report decomposes every question the raw model
got right but the governed pipeline did not into its cause (policy block, human-review hold, row limit,
model drift between the two prompts) so the cost of the controls is measured rather than asserted.

Sampling: stratified by hardness, equal count per tier (so every tier has a usable confidence interval),
seed 42. The headline number is re-weighted to Spider's official tier mix so it is comparable to
published dev-set figures; per-tier numbers are reported unweighted.
"""

import json
import os
import random
import re
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import sqlglot
from sqlglot import exp

from benchmarks.lib import spider as sp
from benchmarks.lib.meta import run_metadata
from benchmarks.lib.sqlcompare import gold_has_order_by, rows_match, run_sqlite
from benchmarks.lib.stats import mcnemar_exact, proportion
from benchmarks.suites.common import EMBEDDING_CACHE, LLM_CACHE, WORK_DIR, load_result, progress, save_result, workspace

TIERS = ["easy", "medium", "hard", "extra"]
OFFICIAL_MIX = {"easy": 248, "medium": 446, "hard": 174, "extra": 166}      # official dev distribution
# SPIDER_HOLDOUT=1 draws a fresh sample that excludes every question of the main 200-question sample (seed 42). The main sample
# was read while diagnosing failures and tuning prompts, so only the hold-out gives an unbiased accuracy for the tuned system.
HOLDOUT = os.getenv("SPIDER_HOLDOUT", "") == "1"
PER_TIER = int(os.getenv("SPIDER_PER_TIER", "25" if HOLDOUT else "50"))
SEED = 43 if HOLDOUT else 42
RESULT_NAME = "spider_holdout" if HOLDOUT else "spider"
# SPIDER_VARIANT switches on the Plan A features for the GOVERNED arm only (the raw arm and its cache are untouched):
#   "fewshot"         3 verified examples retrieved from a pool of Spider TRAIN questions (the product's golden-query mechanism)
#   "repair"          one repair attempt when the database rejects the SQL (the product's repair step)
#   "fewshot_repair"  both
# Each variant writes its own result file (e.g. spider_holdout_fewshot.json) so it can be compared with the baseline question by question.
VARIANT = os.getenv("SPIDER_VARIANT", "").strip().lower()
FEWSHOT = "fewshot" in VARIANT
REPAIR = "repair" in VARIANT
FEWSHOT_K = 3                                    # fixed in advance, deliberately not tuned
if VARIANT:
    RESULT_NAME += "_" + VARIANT
# INTENT_MODE=direct|auto|decision|decision_keywords routes the governed arm (see app/agents/router.py); each mode gets its own result file.
# In the decision modes the Decision 1 answers go through the on-disk cache of the router_eval suite, so a re-run is reproducible and free.
INTENT_MODE = os.getenv("INTENT_MODE", "llm").strip().lower()
if INTENT_MODE in ("direct", "auto", "decision", "decision_keywords"):
    RESULT_NAME += "_intent_" + INTENT_MODE


# SPIDER_CANDIDATES=N (2..5) gives BOTH arms N model calls per question and picks by execution-guided voting
# (app/agents/candidate_voting.py). The raw arm votes on what its candidates return; the governed arm votes only among
# candidates the policy allows. Candidate 0 is the single-candidate run's answer (same cache entry), so runs are paired.
CANDIDATES = min(5, max(1, int(os.getenv("SPIDER_CANDIDATES", "1") or 1)))
if CANDIDATES > 1:
    RESULT_NAME += f"_vote{CANDIDATES}"
# MAX_ROWS is the product's injected row cap (default 100). A run with another value gets its own file, so the accuracy cost of
# the cap can be measured with the same cached model answers (the cap is applied after generation; no new model calls).
ROW_CAP = int(os.getenv("MAX_ROWS", "100") or 100)
if ROW_CAP != 100:
    RESULT_NAME += f"_maxrows{ROW_CAP}"


def _ask_n(llm, prompt: str, n: int) -> List[str]:
    """n replies to one prompt, each its own cached sample; replies without usable SQL are dropped."""
    from benchmarks.lib.llm import run_tag
    replies = []
    for i in range(n):
        if i == 0:
            text = llm.invoke(prompt)
        else:
            with run_tag(f"cand{i}"):
                text = llm.invoke(prompt)
        sql = extract(text)
        if sql:
            replies.append(sql)
    return replies


def _sqlite_rows(db_path: str, sql: str):
    ok, rows, err = run_sqlite(db_path, sql)
    if not ok:
        raise RuntimeError(err)
    return rows


def _cached_decision_flags():
    """A drop-in for app.agents.router.decision_flags that reads and fills benchmarks/cache/decision_responses.jsonl."""
    from benchmarks.suites import router_eval
    cache = router_eval._load_cache()

    def flags(question: str):
        key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        text = (question or "").strip()
        if text not in cache and not key:
            raise SystemExit("INTENT_MODE=decision* needs GEMINI_API_KEY for questions missing from the decision cache")
        answers = router_eval.decide(text, cache, key)       # a provider failure aborts the run; nothing is silently re-routed
        return max(answers[k] for k in router_eval.QUESTIONS) >= router_eval.THRESHOLD
    return flags

RAW_PROMPT = """You are an expert SQL engineer. Given this SQLite schema:

{schema}

Write one SQLite SELECT query that answers the question. Output only the SQL query.

Question: {question}
"""


# ---------------------------------------------------------------------------- data

def _main_sample(dev):
    """The main 200-question sample (seed 42, 50 per tier), reproduced so the hold-out can exclude it."""
    by_tier: Dict[str, List[int]] = defaultdict(list)
    for i, ex in enumerate(dev):
        by_tier[sp.eval_hardness(ex["sql"])].append(i)
    rng = random.Random(42)
    return [{"index": i} for tier in TIERS for i in sorted(rng.sample(by_tier[tier], min(50, len(by_tier[tier]))))]


def sample_questions(per_tier: int = PER_TIER, seed: int = SEED) -> List[Dict[str, Any]]:
    dev = sp.load_dev()
    by_tier: Dict[str, List[int]] = defaultdict(list)
    for i, ex in enumerate(dev):
        by_tier[sp.eval_hardness(ex["sql"])].append(i)
    rng = random.Random(seed)
    if HOLDOUT:
        seen = {q["index"] for q in _main_sample(dev)}
        by_tier = {tier: [i for i in idx if i not in seen] for tier, idx in by_tier.items()}
    chosen = []
    for tier in TIERS:
        for i in sorted(rng.sample(by_tier[tier], min(per_tier, len(by_tier[tier])))):
            ex = dev[i]
            chosen.append({"index": i, "db_id": ex["db_id"], "hardness": tier, "question": ex["question"], "gold": ex["query"]})
    return chosen


def product_schema_context(db_id: str, tables_meta: Dict[str, Any]) -> str:
    """The schema text in exactly the shape SentinelSQL's introspection gives its prompts."""
    meta = tables_meta[db_id]
    tables = meta["table_names_original"]
    cols: Dict[int, List[str]] = defaultdict(list)
    names = {}
    for col_id, (t, c) in enumerate(meta["column_names_original"]):
        if t >= 0:
            cols[t].append(c)
            names[col_id] = (tables[t], c)
    lines = [f"{tables[t]}({', '.join(cols[t])})" for t in range(len(tables))]
    rels = [f"{names[a][0]}.{names[a][1]} -> {names[b][0]}.{names[b][1]}" for a, b in meta["foreign_keys"]]
    if rels:
        lines += ["", "Relationships:", *rels]
    return "\n".join(lines)


def strip_limit(sql: str) -> str:
    """Removes the outermost LIMIT/OFFSET so a query is judged on its logic, not on a row cap."""
    try:
        tree = sqlglot.parse_one(sql.rstrip(";"), read="sqlite")
        tree.set("limit", None)
        tree.set("offset", None)
        return tree.sql(dialect="sqlite")
    except Exception:
        return re.sub(r"\s+limit\s+\d+(\s+offset\s+\d+)?\s*;?\s*$", "", sql, flags=re.I)


# ---------------------------------------------------------------------------- arms

def extract(text: str) -> Optional[str]:
    from app.agents.sql_generator import SQLGenerationAgent
    try:
        return SQLGenerationAgent.__new__(SQLGenerationAgent)._extract_sql(text).rstrip(";")
    except ValueError:
        return None


def judge(db_path: str, gold_sql: str, gold_rows, pred_sql: Optional[str]) -> Dict[str, Any]:
    if not pred_sql:
        return {"correct": False, "error": "no_sql"}
    ok, rows, err = run_sqlite(db_path, pred_sql)
    if not ok:
        return {"correct": False, "error": err[:120]}
    return {"correct": rows_match(gold_rows, rows, gold_has_order_by(gold_sql)), "error": None, "n_rows": len(rows)}


def run_raw(item, db_path, gold_rows) -> Dict[str, Any]:
    from app.utils.llm_factory import get_llm
    prompt = RAW_PROMPT.format(schema=sp.ddl(item["db_id"]), question=item["question"])
    if CANDIDATES > 1:
        from app.agents.candidate_voting import choose_candidate
        from app.governance import Verdict
        candidates = _ask_n(get_llm("sql"), prompt, CANDIDATES)
        sql, vote = choose_candidate(candidates, lambda s: Verdict("allowed"), lambda s: _sqlite_rows(db_path, s)) if candidates else (None, {})
        return {"sql": sql or None, "vote": vote, **judge(db_path, item["gold"], gold_rows, sql or None)}
    sql = extract(get_llm("sql").invoke(prompt))
    return {"sql": sql, **judge(db_path, item["gold"], gold_rows, sql)}


BENCH_USER = "bench-spider"
BENCH_CHANNEL = "BENCHMARK"
BENCH_CLIENT = "spider-benchmark"


class PipelineBypassed(RuntimeError):
    """The governed arm produced a record that the product pipeline cannot have produced. The run must stop."""


def spider_executor(db_path: str):
    """
    The product's own read-only execution (statement timeout, row cap, masking, SQLAlchemy errors) pointed at one Spider
    database file. `.calls` counts executions; `.failures` keeps the statements the database rejected.
    """
    import sqlite3

    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    from app.db.database import execute_readonly_query

    def connect():
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.text_factory = lambda b: b.decode("utf-8", errors="replace")   # Spider has a few corrupt UTF-8 rows (same as run_sqlite)
        return conn

    engine = create_engine("sqlite://", creator=connect, poolclass=NullPool)

    def execute(sql: str) -> Dict[str, Any]:
        execute.calls += 1
        try:
            return execute_readonly_query(sql, engine=engine)
        except Exception as e:
            execute.failures.append({"sql": sql, "error": f"{type(e).__name__}: {e}"[:200]})
            raise
    execute.calls = 0
    execute.failures = []
    return execute


@contextmanager
def _model_calls():
    """
    Watches the model calls of one question (stacked on the caching harness, if one is installed).

      * A provider failure is remembered. The pipeline turns every exception into a polite refusal, which is right for a
        user and wrong for a benchmark: an outage or an exhausted quota must stop the run, not be scored as a wrong answer.
      * The k-th repeat of the same prompt is tagged `cand{k}`, so the N candidates of SPIDER_CANDIDATES are N separate
        cached samples (the same keys the earlier runs used) instead of one cached answer returned N times.
    """
    from app.utils.llm_factory import UniversalLLM
    from benchmarks.lib.llm import run_tag

    inner = UniversalLLM.invoke
    watch = SimpleNamespace(failure=None, calls=0)
    seen: Counter = Counter()
    lock = threading.Lock()

    def invoke(self, prompt: str) -> str:
        with lock:
            repeat = seen[prompt]
            seen[prompt] += 1
            watch.calls += 1
        try:
            if repeat:
                with run_tag(f"cand{repeat}"):
                    return inner(self, prompt)
            return inner(self, prompt)
        except Exception as e:
            watch.failure = watch.failure or e
            raise

    UniversalLLM.invoke = invoke
    try:
        yield watch
    finally:
        UniversalLLM.invoke = inner


class _OneAtATime(ThreadPoolExecutor):
    """Candidates are asked one after another, in order, so candidate i always gets cache tag i (the product asks in parallel)."""

    def __init__(self, max_workers=None, **kwargs):
        super().__init__(max_workers=1, **kwargs)


def _last_audit_id() -> int:
    from app.utils.audit_logger import get_audit_logs
    rows = get_audit_logs(limit=1)
    return rows[0]["id"] if rows else 0


def run_governed(item, db_path, gold_rows, ctx, fewshot: bool = False, repair: bool = False) -> Dict[str, Any]:
    """
    One question through the product pipeline (the LangGraph of app/graph/workflow.py), given the Spider schema text and
    an executor for the Spider database. Nothing of the pipeline is repeated here: this function builds the request,
    switches off what describes the demo company, and translates the final state into a benchmark record.
    """
    from app.answering import default_pipeline
    from app.governance import BLOCKED, ERROR, HELD, Outcome

    out: Dict[str, Any] = {"sql": None, "outcome": None, "correct": False, "correct_limit_adjusted": False}
    execute = spider_executor(db_path)
    state: Dict[str, Any] = {
        "user_query": item["question"], "user_id": BENCH_USER, "role": "employee", "channel": BENCH_CHANNEL,
        "client_agent": BENCH_CLIENT, "schema_context": ctx, "executor": execute, "intent": None, "sql": None, "results": None,
    }
    audit_before = _last_audit_id()
    with ExitStack() as stack:
        # The variant flags are the product's own switches.
        stack.enter_context(patch.dict(os.environ, {"SQL_REPAIR_ATTEMPTS": "1" if repair else "0", "SQL_CANDIDATES": str(CANDIDATES)}))
        # The glossary and the schema index describe the demo company, not the Spider databases.
        stack.enter_context(patch("app.rag.data_dictionary.format_dictionary_context", return_value=""))
        stack.enter_context(patch("app.graph.workflow.get_rag_schema_context",
                                  side_effect=AssertionError("the benchmark supplies the schema; retrieval must not run")))
        if fewshot:
            from app.rag.golden_store import find_matching_golden_queries
            out["fewshot"] = [{"question": m["natural_language_query"], "similarity": round(m["similarity"], 3)}
                              for m in find_matching_golden_queries(item["question"], top_k=FEWSHOT_K)]
        else:
            stack.enter_context(patch("app.rag.golden_store.format_few_shot_examples", return_value=""))
        if CANDIDATES > 1:
            stack.enter_context(patch("app.agents.sql_generator.ThreadPoolExecutor", _OneAtATime))
        model = stack.enter_context(_model_calls())
        final = default_pipeline(state)
    if model.failure is not None:
        raise model.failure

    out["intent_path"] = bool(final.get("intent_used", True))
    outcome = final.get("outcome")
    audit_rows = _last_audit_id() - audit_before

    if outcome is None:
        stopped = final.get("stopped")
        if not stopped:
            raise PipelineBypassed(f"question {item['index']}: the final state has neither an outcome nor a stop reason")
        if stopped.get("status") == ERROR:
            raise RuntimeError(f"question {item['index']}: the pipeline failed, not the model's answer: {stopped.get('reason')}")
        if audit_rows or execute.calls:
            raise PipelineBypassed(f"question {item['index']}: stopped before any SQL, yet {audit_rows} audit rows and {execute.calls} executions")
        reason = stopped.get("reason") or ""
        if final.get("intent") is None:
            out.update(outcome="intent_rejected", detail=reason[:100])
        else:
            out.update(outcome="generation_failed", detail=reason.replace("SQL Generation Failed: ", "", 1)[:100])
        return out

    # The audit record is written by `run` in app/governance.py and by nothing else, so a row here means the governed run happened.
    if not isinstance(outcome, Outcome) or audit_rows < 1:
        raise PipelineBypassed(f"question {item['index']}: no governed run behind this answer ({audit_rows} audit rows)")
    out.update(policy_reason=outcome.verdict.reason[:120], audit_rows=audit_rows, executions=execute.calls)
    if final.get("candidate_vote"):
        out["vote"] = final["candidate_vote"]
    if final.get("repair_attempts"):
        out["repaired"] = True
    if final.get("repair_failed"):
        out["repair_failed"] = True
    if execute.failures:
        out["failed_executions"] = execute.failures[:5]

    if outcome.status == BLOCKED:
        out.update(outcome="blocked_by_policy", generated_sql=outcome.sql)
        return out
    if outcome.status == HELD:
        out.update(outcome="held_for_review", generated_sql=outcome.sql, tables=outcome.verdict.tables)
        return out
    if not execute.calls:
        raise PipelineBypassed(f"question {item['index']}: the pipeline reports an execution that did not use the Spider database")

    out["sql"] = outcome.sql.rstrip(";")
    if outcome.status == ERROR:
        # As shipped, the user got an error (after any repair attempt), whatever the comparator would say about the SQL.
        judged = {"correct": False, "error": outcome.reason[:120]}
    else:
        judged = judge(db_path, item["gold"], gold_rows, out["sql"])
    out.update(outcome="executed", correct=judged["correct"], error=judged["error"])
    # logic-only score: drop the row cap unless the gold query itself has one
    probe_sql = out["sql"] if re.search(r"\blimit\b", item["gold"], re.I) else strip_limit(out["sql"])
    out["correct_limit_adjusted"] = judge(db_path, item["gold"], gold_rows, probe_sql)["correct"]
    return out


# ---------------------------------------------------------------------------- analysis

def weighted_rate(by_tier: Dict[str, List[bool]]) -> float:
    total = sum(OFFICIAL_MIX.values())
    return 100 * sum(OFFICIAL_MIX[t] / total * (sum(v) / len(v)) for t, v in by_tier.items() if v)


def tier_table(records: List[Dict[str, Any]], key) -> Dict[str, Any]:
    by_tier: Dict[str, List[bool]] = defaultdict(list)
    for r in records:
        by_tier[r["hardness"]].append(bool(key(r)))
    table = {t: proportion(sum(by_tier[t]), len(by_tier[t])) for t in TIERS if by_tier[t]}
    table["ALL_unweighted"] = proportion(sum(sum(v) for v in by_tier.values()), sum(len(v) for v in by_tier.values()))
    table["ALL_dev_weighted"] = round(weighted_rate(by_tier), 2)
    return table


def analyse(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    raw = lambda r: r["raw"]["correct"]
    gov_strict = lambda r: r["governed"]["correct"]
    gov_logic = lambda r: r["governed"]["correct_limit_adjusted"]
    b = sum(1 for r in records if raw(r) and not gov_strict(r))      # raw right, governed wrong
    c = sum(1 for r in records if gov_strict(r) and not raw(r))      # governed right, raw wrong

    lost = Counter()
    for r in records:
        if raw(r) and not gov_strict(r):
            g = r["governed"]
            if g["outcome"] in ("blocked_by_policy", "held_for_review", "intent_rejected", "generation_failed"):
                lost[g["outcome"]] += 1
            elif gov_logic(r):
                lost["row_limit_applied"] += 1
            else:
                lost["different_sql_from_two_stage_prompt"] += 1
    reasons = Counter(re.sub(r"'[^']*'", "'..'", r["governed"].get("policy_reason", ""))[:70]
                      for r in records if r["governed"]["outcome"] in ("blocked_by_policy", "held_for_review"))
    review_tables = Counter(t for r in records if r["governed"]["outcome"] == "held_for_review" for t in (r["governed"].get("tables") or []))

    return {
        "n": len(records),
        "raw_model": tier_table(records, raw),
        "governed_as_shipped": tier_table(records, gov_strict),
        "governed_logic_only": tier_table(records, gov_logic),
        "governed_outcomes": dict(Counter(r["governed"]["outcome"] for r in records)),
        "sent_through_intent_model": sum(1 for r in records if r["governed"].get("intent_path", True)),
        "paired_raw_vs_governed_as_shipped": {"raw_only_correct": b, "governed_only_correct": c, "mcnemar_p": round(mcnemar_exact(b, c), 5)},
        "raw_correct_but_governed_lost": dict(lost),
        "top_policy_reasons": reasons.most_common(8),
        "tables_that_triggered_review": review_tables.most_common(8),
        "raw_failure_modes": dict(Counter("no_sql" if r["raw"].get("error") == "no_sql" else ("exec_error" if r["raw"].get("error") else "wrong_result")
                                          for r in records if not raw(r))),
    }


# ---------------------------------------------------------------------------- driver

def run(llm: bool = True, limit: Optional[int] = None) -> Dict[str, Any]:
    from app.rag.vector_store import EmbeddingUnavailableError
    from benchmarks.lib import fewshot
    from benchmarks.lib.llm import DailyQuotaExceeded, embedding_harness, harness

    questions = sample_questions()
    if limit:
        questions = questions[:limit]
    tables_meta = {t["db_id"]: t for t in json.load(open(sp.TABLES_JSON, encoding="utf-8"))}

    # controls: the comparator must score gold-vs-gold at 100% and an obviously wrong query at 0%
    controls = {"gold_vs_gold": 0, "select_1": 0}
    prepared = []
    for q in questions:
        path = sp.db_path(q["db_id"])
        ok, gold_rows, err = run_sqlite(path, q["gold"])
        assert ok, f"gold query failed for {q['index']}: {err}"
        controls["gold_vs_gold"] += rows_match(gold_rows, gold_rows, gold_has_order_by(q["gold"]))
        controls["select_1"] += judge(path, q["gold"], gold_rows, "SELECT 1")["correct"]
        prepared.append((q, path, gold_rows))
    print(f"Spider: {len(prepared)} questions; comparator controls gold==gold {controls['gold_vs_gold']}/{len(prepared)}, "
          f"SELECT 1 correct {controls['select_1']}/{len(prepared)}", flush=True)

    records, status, usage = [], "complete", {}
    started = time.time()
    if llm:
        try:
            with ExitStack() as stack:
                # The governed arm writes audit rows and review holds like the product does: keep them out of the real stores.
                stack.enter_context(workspace("spider", seed=False))
                u = stack.enter_context(harness(LLM_CACHE))
                if INTENT_MODE in ("decision", "decision_keywords"):
                    stack.enter_context(patch("app.agents.router.decision_flags", _cached_decision_flags()))
                if FEWSHOT:
                    eu = stack.enter_context(embedding_harness(EMBEDDING_CACHE))
                    pool_dir = os.path.join(WORK_DIR, "spider_pool")
                    os.makedirs(pool_dir, exist_ok=True)
                    os.environ["GOLDEN_DB_PATH"] = os.path.join(pool_dir, "golden.db")
                    os.environ["GOLDEN_VECTOR_CACHE"] = os.path.join(pool_dir, "golden_vectors.json")
                    os.environ["FEWSHOT_TOP_K"] = str(FEWSHOT_K)
                    pool = fewshot.build_pool()
                    fewshot.assert_disjoint_from_dev(pool)
                    print(f"  few-shot pool: {len(pool)} Spider train examples, {fewshot.seed_golden_store(pool)} newly loaded", flush=True)
                for n, (q, path, gold_rows) in enumerate(prepared, 1):
                    progress("spider", n, len(prepared), started)
                    ctx = product_schema_context(q["db_id"], tables_meta)
                    records.append({**q, "raw": run_raw(q, path, gold_rows),
                                    "governed": run_governed(q, path, gold_rows, ctx, fewshot=FEWSHOT, repair=REPAIR)})
                    if n % 20 == 0:
                        save_result(RESULT_NAME + ".partial", {"records": records})
                usage = {"llm": u.snapshot(), **({"embeddings": eu.snapshot()} if FEWSHOT else {})}
        except (DailyQuotaExceeded, EmbeddingUnavailableError) as e:
            status = "paused: daily quota (progress cached; re-run to resume)"
            print("  " + status, flush=True)

    result = {
        "meta": run_metadata([sp.DEV_JSON], {"status": status, "seed": SEED, "per_tier": PER_TIER, "variant": VARIANT or "baseline", "intent_mode": INTENT_MODE, "candidates": CANDIDATES, "row_cap": ROW_CAP,
                                             "fewshot_k": FEWSHOT_K if FEWSHOT else 0, "pool_size": 500 if FEWSHOT else 0, "usage": usage,
                                             "duration_s": round(time.time() - started)}),
        "comparator_controls": {**controls, "n": len(prepared)},
        # every governed answer came out of the product graph: run_governed raises PipelineBypassed otherwise
        "pipeline_controls": {"governed_arm": "app.graph.workflow.build_graph", "questions": len(records),
                              "audit_rows": sum(r["governed"].get("audit_rows", 0) for r in records),
                              "executions": sum(r["governed"].get("executions", 0) for r in records)},
        "analysis": analyse(records) if records else None,
        "records": records,
    }
    previous = load_result(RESULT_NAME)
    if not records and previous:
        return previous
    print("saved", save_result(RESULT_NAME, result))
    return result
