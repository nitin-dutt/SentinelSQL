"""
Execution-guided candidate voting (SQL_CANDIDATES, off by default).

With SQL_CANDIDATES=N (2 to 5) the generator writes N candidate queries for one question. Each candidate goes through the
policy validator; the ones the policy allows are run read-only, and the candidates are grouped by the rows they return.
The query whose result the most candidates agree on wins (a tie goes to the earliest candidate).

Agents propose, the gate decides:
  * a candidate the policy blocks is never executed and cannot win;
  * if any candidate needs human review, voting stops and that candidate is returned, so the review hold stands;
  * the winner is only a proposal: the pipeline validates it again and executes it through the normal path.

Cost: N model calls instead of one, plus one read-only execution per distinct candidate.
"""

from collections import Counter
from typing import Any, Callable, Dict, Iterable, List, Tuple

from app.governance import Verdict
from app.settings import settings

MAX_CANDIDATES = 5


def candidate_count() -> int:
    """SQL_CANDIDATES, clamped to 1..5. 1 (the default) means no voting."""
    return min(MAX_CANDIDATES, max(1, settings.pipeline.sql_candidates))


def _cell(value: Any) -> str:
    if isinstance(value, bool) or value is None:
        return repr(value)
    if isinstance(value, (int, float)):
        return repr(round(float(value), 6))        # 3 and 3.0 are the same answer
    return str(value)


def result_signature(rows: Iterable[Any]) -> Tuple:
    """A hashable summary of a result set that ignores row order and column order."""
    counted = Counter(tuple(sorted(_cell(v) for v in (row.values() if isinstance(row, dict) else row))) for row in rows)
    return tuple(sorted(counted.items()))


def choose_candidate(candidates: List[str], validate: Callable[[str], Verdict],
                     execute: Callable[[str], Iterable[Any]]) -> Tuple[str, Dict[str, Any]]:
    """
    Returns (chosen SQL, vote details). `validate` is the policy gate (`judge` in app/governance.py); `execute` runs an allowed query read-only and
    returns its rows (it may raise). The chosen SQL is one of `candidates`, unchanged.
    """
    info: Dict[str, Any] = {"candidates": len(candidates), "statuses": [], "picked": 0, "votes": 0, "distinct_results": 0, "reason": "single"}
    if len(candidates) <= 1:
        return (candidates[0] if candidates else ""), info

    signatures: Dict[int, Tuple] = {}
    seen: Dict[str, Any] = {}                       # identical SQL text is validated and run once
    for i, sql in enumerate(candidates):
        if sql in seen:
            status, sig = seen[sql]
        else:
            verdict = validate(sql)
            if verdict.blocked:
                status, sig = "blocked", None
            elif verdict.held:
                info["statuses"].append("review")
                info.update(picked=i, reason="review_hold")
                return sql, info
            else:
                try:
                    status, sig = "ok", result_signature(execute(verdict.sanitized_sql or sql))
                except Exception:
                    status, sig = "error", None
            seen[sql] = (status, sig)
        info["statuses"].append(status)
        if sig is not None:
            signatures[i] = sig

    if not signatures:
        info["reason"] = "none_executed"            # the first candidate goes on; the normal path reports its block or error
        return candidates[0], info
    tally = Counter(signatures.values())
    best = max(tally.values())
    winner = next(i for i, sig in signatures.items() if tally[sig] == best)
    info.update(picked=winner, votes=best, distinct_results=len(tally), reason="majority" if best > 1 else "no_agreement")
    return candidates[winner], info
