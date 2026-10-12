"""
Execution-accuracy (EX) comparison, in the style of the Spider test-suite evaluation:

  * a predicted query is correct when it returns the same *multiset of rows* as the gold query;
  * column order is ignored (SELECT a, b equals SELECT b, a), because the question rarely fixes it;
  * row order matters only when the gold query has an ORDER BY;
  * numbers compare numerically (1 == 1.0) with a tolerance of 1e-6.

Known limitation (documented in the report): this is single-database execution matching, not the
distilled test-suite variant, so two different queries that happen to agree on this data are counted
as equivalent. It is therefore an upper bound on true semantic accuracy.
"""

import itertools
import sqlite3
from collections import Counter
from typing import Any, List, Optional, Sequence, Tuple


def run_sqlite(db_path: str, sql: str, timeout_s: float = 10.0, max_rows: int = 100_000) -> Tuple[bool, List[tuple], str]:
    """Executes `sql` read-only with a wall-clock timeout. Returns (ok, rows, error)."""
    import time
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=timeout_s)
    conn.text_factory = lambda b: b.decode("utf-8", errors="replace")   # Spider has a few corrupt UTF-8 rows
    deadline = time.monotonic() + timeout_s
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10000)
    try:
        rows = conn.execute(sql).fetchmany(max_rows)
        return True, [tuple(r) for r in rows], ""
    except Exception as e:  # syntax error, missing column, interrupted by the timeout, ...
        return False, [], f"{type(e).__name__}: {e}"
    finally:
        conn.close()


def _norm(value: Any) -> Any:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return round(float(value), 6)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _norm_row(row: Sequence[Any]) -> tuple:
    return tuple(_norm(v) for v in row)


def _column_signature(rows: List[tuple], index: int) -> tuple:
    return tuple(sorted((repr(r[index]) for r in rows)))


def rows_match(gold: List[tuple], pred: List[tuple], order_matters: bool) -> bool:
    """True when `pred` equals `gold` up to column order (and row order unless `order_matters`)."""
    gold = [_norm_row(r) for r in gold]
    pred = [_norm_row(r) for r in pred]
    if len(gold) != len(pred):
        return False
    if not gold:
        return True
    if len(gold[0]) != len(pred[0]):
        return False
    width = len(gold[0])

    gold_sigs = [_column_signature(gold, i) for i in range(width)]
    pred_sigs = [_column_signature(pred, i) for i in range(width)]
    # each gold column may only map to predicted columns holding the same multiset of values
    candidates = [[j for j in range(width) if pred_sigs[j] == gold_sigs[i]] for i in range(width)]
    if any(not c for c in candidates):
        return False

    gold_key = Counter(gold)
    for assignment in _assignments(candidates):
        permuted = [tuple(row[j] for j in assignment) for row in pred]
        if order_matters:
            if permuted == gold:
                return True
        elif Counter(permuted) == gold_key:
            return True
    return False


def _assignments(candidates: List[List[int]], limit: int = 5040):
    """Injective column assignments (bounded: very wide ambiguous results are not worth enumerating)."""
    count = 0
    for combo in itertools.product(*candidates):
        if len(set(combo)) == len(combo):
            yield combo
            count += 1
            if count >= limit:
                return


def gold_has_order_by(gold_sql: str) -> bool:
    return "order by" in gold_sql.lower()


def execution_match(db_path: str, gold_sql: str, pred_sql: str) -> Tuple[bool, Optional[str]]:
    """(correct, error_if_pred_failed). A gold query that fails is a dataset problem and is raised."""
    ok_g, gold_rows, err_g = run_sqlite(db_path, gold_sql)
    if not ok_g:
        raise RuntimeError(f"gold query failed on {db_path}: {err_g}")
    ok_p, pred_rows, err_p = run_sqlite(db_path, pred_sql)
    if not ok_p:
        return False, err_p
    return rows_match(gold_rows, pred_rows, gold_has_order_by(gold_sql)), None
