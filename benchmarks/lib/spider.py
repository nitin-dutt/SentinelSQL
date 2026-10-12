"""
Access to the Yale Spider dev set plus the official hardness classification.

`eval_hardness` is a port of the classifier in Spider's `evaluation.py`, operating on the parsed `sql`
structure that ships with every dev.json example. `tests/test_benchmark_lib.py` verifies the port by
checking that it reproduces the official dev-set distribution (easy 248 / medium 446 / hard 174 /
extra 166 = 1,034), which is exactly how the published split is defined.
"""

import json
import os
import sqlite3
from typing import Any, Dict, List

import sqlglot
from sqlglot import exp

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SPIDER_DIR = os.path.join(ROOT, "benchmarks", "data", "spider", "spider")
DEV_JSON = os.path.join(SPIDER_DIR, "dev.json")
TABLES_JSON = os.path.join(SPIDER_DIR, "tables.json")
DB_DIR = os.path.join(SPIDER_DIR, "database")

WHERE_OPS = ("not", "between", "=", ">", "<", ">=", "<=", "!=", "in", "like", "is", "exists")
AGG_NONE = 0


def load_dev() -> List[Dict[str, Any]]:
    with open(DEV_JSON, "r", encoding="utf-8") as f:
        return json.load(f)


def db_path(db_id: str) -> str:
    return os.path.join(DB_DIR, db_id, f"{db_id}.sqlite")


def ddl(db_id: str) -> str:
    """CREATE TABLE statements of a Spider database, straight from the SQLite file."""
    conn = sqlite3.connect(f"file:{db_path(db_id)}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
    finally:
        conn.close()
    return "\n\n".join(r[0] for r in rows if r[0])


def gold_tables(gold_sql: str) -> List[str]:
    """Lower-cased physical table names referenced by a gold query (CTE names excluded)."""
    tree = sqlglot.parse_one(gold_sql, read="sqlite")
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    return sorted({t.name.lower() for t in tree.find_all(exp.Table) if t.name and t.name.lower() not in ctes})


# ---------------------------------------------------------------- official hardness (port)

def _nested(sql: dict) -> list:
    nested = []
    for cond_unit in sql["from"]["conds"][::2] + sql["where"][::2] + sql["having"][::2]:
        if isinstance(cond_unit[3], dict):
            nested.append(cond_unit[3])
        if isinstance(cond_unit[4], dict):
            nested.append(cond_unit[4])
    for key in ("intersect", "except", "union"):
        if sql[key] is not None:
            nested.append(sql[key])
    return nested


def _count_agg(units: list) -> int:
    return len([u for u in units if u[0] != AGG_NONE])


def _component1(sql: dict) -> int:
    count = 0
    if len(sql["where"]) > 0:
        count += 1
    if len(sql["groupBy"]) > 0:
        count += 1
    if len(sql["orderBy"]) > 0:
        count += 1
    if sql["limit"] is not None:
        count += 1
    if len(sql["from"]["table_units"]) > 0:
        count += len(sql["from"]["table_units"]) - 1
    ao = sql["from"]["conds"][1::2] + sql["where"][1::2] + sql["having"][1::2]
    count += len([t for t in ao if t == "or"])
    cond_units = sql["from"]["conds"][::2] + sql["where"][::2] + sql["having"][::2]
    count += len([c for c in cond_units if c[1] == WHERE_OPS.index("like")])
    return count


def _others(sql: dict) -> int:
    count = 0
    agg_count = _count_agg(sql["select"][1])
    agg_count += _count_agg(sql["where"][::2])
    agg_count += _count_agg(sql["groupBy"])
    if len(sql["orderBy"]) > 0:
        agg_count += _count_agg([u[1] for u in sql["orderBy"][1] if u[1]] + [u[2] for u in sql["orderBy"][1] if u[2]])
    agg_count += _count_agg(sql["having"])
    if agg_count > 1:
        count += 1
    if len(sql["select"][1]) > 1:
        count += 1
    if len(sql["where"]) > 1:
        count += 1
    if len(sql["groupBy"]) > 1:
        count += 1
    return count


def eval_hardness(sql: dict) -> str:
    c1, c2, c3 = _component1(sql), len(_nested(sql)), _others(sql)
    if c1 <= 1 and c3 == 0 and c2 == 0:
        return "easy"
    if (c3 <= 2 and c1 <= 1 and c2 == 0) or (c1 <= 2 and c3 < 2 and c2 == 0):
        return "medium"
    if (c3 > 2 and c1 <= 2 and c2 == 0) or (2 < c1 <= 3 and c3 <= 2 and c2 == 0) or (c1 <= 1 and c3 == 0 and c2 <= 1):
        return "hard"
    return "extra"
