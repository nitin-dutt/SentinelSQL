"""
Text-to-SQL accuracy on the Yale Spider dev set (live model).

Two arms answer the SAME stratified sample of questions with the SAME model:

  RAW       zero-shot: the full CREATE TABLE schema + question -> SQL. This is what you get by pointing an
            LLM straight at a database. It is the baseline.
  GOVERNED  SentinelSQL's own components, in order: intent agent -> SQL generator (its production prompt)
            -> policy engine (AST rules, LIMIT enforcement, sensitive-table review) -> read-only execution.
            The domain glossary / golden-query memory are switched off: they describe the demo company, not
            the Spider databases.

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
import time
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import sqlglot
from sqlglot import exp

from benchmarks.lib import spider as sp
from benchmarks.lib.meta import run_metadata
from benchmarks.lib.sqlcompare import gold_has_order_by, rows_match, run_sqlite
from benchmarks.lib.stats import mcnemar_exact, proportion
from benchmarks.suites.common import LLM_CACHE, load_result, progress, save_result

TIERS = ["easy", "medium", "hard", "extra"]
OFFICIAL_MIX = {"easy": 248, "medium": 446, "hard": 174, "extra": 166}      # official dev distribution
PER_TIER = int(os.getenv("SPIDER_PER_TIER", "50"))
SEED = 42

RAW_PROMPT = """You are an expert SQL engineer. Given this SQLite schema:

{schema}

Write one SQLite SELECT query that answers the question. Output only the SQL query.

Question: {question}
"""


# ---------------------------------------------------------------------------- data

def sample_questions(per_tier: int = PER_TIER, seed: int = SEED) -> List[Dict[str, Any]]:
    dev = sp.load_dev()
    by_tier: Dict[str, List[int]] = defaultdict(list)
    for i, ex in enumerate(dev):
        by_tier[sp.eval_hardness(ex["sql"])].append(i)
    rng = random.Random(seed)
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
    text = get_llm("sql").invoke(RAW_PROMPT.format(schema=sp.ddl(item["db_id"]), question=item["question"]))
    sql = extract(text)
    return {"sql": sql, **judge(db_path, item["gold"], gold_rows, sql)}


def run_governed(item, db_path, gold_rows, ctx) -> Dict[str, Any]:
    from app.agents.intent_agent import IntentAnalysisAgent
    from app.agents.sql_generator import SQLGenerationAgent
    from app.agents.sql_validator import SQLValidationAgent

    out: Dict[str, Any] = {"sql": None, "outcome": None, "correct": False, "correct_limit_adjusted": False}
    try:
        intent = IntentAnalysisAgent().analyze(item["question"], ctx)
    except ValueError as e:
        out.update(outcome="intent_rejected", detail=str(e)[:100])
        return out
    try:
        with patch("app.rag.golden_store.format_few_shot_examples", return_value=""), \
             patch("app.rag.data_dictionary.format_dictionary_context", return_value=""):
            raw_sql = SQLGenerationAgent().generate_sql(intent=intent, schema_context=ctx, user_query=item["question"])
    except ValueError as e:
        out.update(outcome="generation_failed", detail=str(e)[:100])
        return out

    verdict = SQLValidationAgent().validate(raw_sql, item["question"])
    out["generated_sql"] = raw_sql
    out["policy_reason"] = (verdict.get("reason") or "")[:120]
    if not verdict["safe"]:
        out.update(outcome="blocked_by_policy")
        return out
    if verdict.get("requires_human"):
        out.update(outcome="held_for_review", tables=verdict.get("tables"))
        return out

    executed_sql = verdict.get("sanitized_sql") or raw_sql
    out["sql"] = executed_sql.rstrip(";")
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
        "paired_raw_vs_governed_as_shipped": {"raw_only_correct": b, "governed_only_correct": c, "mcnemar_p": round(mcnemar_exact(b, c), 5)},
        "raw_correct_but_governed_lost": dict(lost),
        "top_policy_reasons": reasons.most_common(8),
        "tables_that_triggered_review": review_tables.most_common(8),
        "raw_failure_modes": dict(Counter("no_sql" if r["raw"].get("error") == "no_sql" else ("exec_error" if r["raw"].get("error") else "wrong_result")
                                          for r in records if not raw(r))),
    }


# ---------------------------------------------------------------------------- driver

def run(llm: bool = True, limit: Optional[int] = None) -> Dict[str, Any]:
    from benchmarks.lib.llm import DailyQuotaExceeded, harness

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
            with harness(LLM_CACHE) as u:
                for n, (q, path, gold_rows) in enumerate(prepared, 1):
                    progress("spider", n, len(prepared), started)
                    ctx = product_schema_context(q["db_id"], tables_meta)
                    records.append({**q, "raw": run_raw(q, path, gold_rows), "governed": run_governed(q, path, gold_rows, ctx)})
                    if n % 20 == 0:
                        save_result("spider.partial", {"records": records})
                usage = u.snapshot()
        except DailyQuotaExceeded as e:
            status = "paused: daily quota (progress cached; re-run to resume)"
            print("  " + status, flush=True)

    result = {
        "meta": run_metadata([sp.DEV_JSON], {"status": status, "seed": SEED, "per_tier": PER_TIER, "usage": usage,
                                             "duration_s": round(time.time() - started)}),
        "comparator_controls": {**controls, "n": len(prepared)},
        "analysis": analyse(records) if records else None,
        "records": records,
    }
    previous = load_result("spider")
    if not records and previous:
        return previous
    print("saved", save_result("spider", result))
    return result
