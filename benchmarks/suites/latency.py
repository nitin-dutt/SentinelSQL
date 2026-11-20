"""
Latency suite: what a user actually waits for.

  cold      the full HTTP path (POST /query) with the cache off and the real model: intent check -> SQL generation
            -> policy validation -> execution -> masking. Requests are spaced so the free-tier rate limit never
            makes the client wait (no benchmark harness or response cache is involved: every number is a live call).
            Per-stage times come from wrapping the four graph nodes.
  cache L1  the exact-match cache path, 500 trials through the HTTP endpoint.
  cache L2  the similarity path (one live embedding per lookup) on paraphrase pairs taken from the cache-safety benchmark.

Speedups are computed against the *measured* cold median, never a typed constant. Time spent by the model is
reported separately from time spent in SentinelSQL's own code (validation, execution, masking).
"""

import os
import time
from typing import Any, Dict, List, Optional

from benchmarks.data.domain_qa import QUESTIONS
from benchmarks.lib.meta import run_metadata
from benchmarks.lib.stats import latency_summary
from benchmarks.suites.common import EMPLOYEE, progress, save_result, workspace

COLD_N = int(os.getenv("LATENCY_COLD_N", "30"))
SPACING_S = float(os.getenv("LATENCY_SPACING_S", "9"))
L1_TRIALS = 500
L2_PAIRS = 12                                       # similarity-path lookups to time (two live embeddings each)
# LATENCY_COLD_ONLY=1 measures only the cold path and writes latency_cold_intent_<INTENT_MODE>.json (with a _vote<N> suffix when
# SQL_CANDIDATES > 1), so the routing modes can be compared on the same questions without touching latency.json.
COLD_ONLY = os.getenv("LATENCY_COLD_ONLY", "") == "1"


def l2_pairs(limit: int = L2_PAIRS):
    """Paraphrase pairs that the cache-safety benchmark measured as legitimate similarity hits (cosine >= 0.92, guards agree)."""
    from benchmarks.data.cache_pairs import PAIRS
    from benchmarks.suites.common import load_result
    scored = {p["id"]: p for p in (load_result("cache_safety") or {}).get("pairs", [])}
    chosen = []
    for pid, kind, qa, qb, should_hit, sql_a in PAIRS:
        m = scored.get(pid)
        if should_hit and m and m["similarity"] >= 0.92 and m["guards_ok"]:
            chosen.append((qa, qb, sql_a or "SELECT name FROM customers LIMIT 5"))
    return chosen[:limit]


class StageTimer:
    """
    Wraps the pipeline's stage functions so each request reports how long every stage took. The policy gate and the
    execution now happen inside `run` (app/governance.py), so those two are timed there; they keep the result keys
    `validation_node` and `execution_node` so new results compare with the ones recorded before that change.
    """

    # (module, function, key in the results)
    STAGES = (("app.graph.workflow", "intent_node", "intent_node"),
              ("app.graph.workflow", "sql_generation_node", "sql_generation_node"),
              ("app.governance", "judge", "validation_node"),
              ("app.governance", "execute_readonly_query", "execution_node"))

    def __init__(self):
        self.current: Dict[str, float] = {}

    def install(self):
        import importlib
        from app.graph import workflow
        self._originals = []
        for module_name, name, key in self.STAGES:
            module = importlib.import_module(module_name)
            fn = getattr(module, name)
            self._originals.append((module, name, fn))

            def wrapped(*args, _fn=fn, _key=key, **kwargs):
                t = time.perf_counter()
                try:
                    return _fn(*args, **kwargs)
                finally:
                    self.current[_key] = self.current.get(_key, 0.0) + (time.perf_counter() - t) * 1000
            setattr(module, name, wrapped)
        workflow.build_graph.cache_clear()

    def restore(self):
        from app.graph import workflow
        for module, name, fn in self._originals:
            setattr(module, name, fn)
        workflow.build_graph.cache_clear()


def run(llm: bool = True, limit: Optional[int] = None) -> Dict[str, Any]:
    if not llm:
        return {}
    from fastapi.testclient import TestClient
    from app.main import app

    n_cold = min(COLD_N, limit) if limit else COLD_N
    started = time.time()
    out: Dict[str, Any] = {}

    # ---------------------------------------------------------------- cold, cache off
    cold: List[Dict[str, Any]] = []
    with workspace("latency_cold", semantic_cache=False):
        timer = StageTimer()
        timer.install()
        try:
            with TestClient(app) as client:
                for i, (qid, tier, question, gold, needs) in enumerate(QUESTIONS[:n_cold], 1):
                    progress("latency-cold", i, n_cold, started)
                    timer.current = {}
                    t = time.perf_counter()
                    resp = client.post("/query", json={"query": question}, headers=EMPLOYEE)
                    wall = (time.perf_counter() - t) * 1000
                    body = resp.json() if resp.status_code == 200 else {}
                    stages = dict(timer.current)
                    model_ms = stages.get("intent_node", 0.0) + stages.get("sql_generation_node", 0.0)
                    cold.append({"id": qid, "tier": tier, "http": resp.status_code, "status": body.get("status"),
                                 "total_ms": round(wall, 1), "model_ms": round(model_ms, 1),
                                 "gateway_ms": round(wall - model_ms, 1),
                                 "stages_ms": {k: round(v, 1) for k, v in stages.items()}})
                    time.sleep(SPACING_S)
        finally:
            timer.restore()

    ok = [c for c in cold if c["status"] in ("completed", "requires_human")]
    cold_total = latency_summary([c["total_ms"] for c in ok])
    out["cold"] = {
        "n_requests": len(cold), "n_answered": len(ok),
        "statuses": {s: sum(c["status"] == s for c in cold) for s in {c["status"] for c in cold}},
        "total_ms": cold_total,
        "model_ms": latency_summary([c["model_ms"] for c in ok]),
        "gateway_ms": latency_summary([c["gateway_ms"] for c in ok]),
        "per_stage_median_ms": {k: latency_summary([c["stages_ms"].get(k, 0.0) for c in ok])["p50_ms"]
                                for k in ("intent_node", "sql_generation_node", "validation_node", "execution_node")} if ok else {},
        "requests": cold,
    }
    cold_p50 = cold_total.get("p50_ms") if ok else None

    from app.agents.candidate_voting import candidate_count
    from app.agents.router import intent_mode
    settings = {"intent_mode": intent_mode(), "sql_candidates": candidate_count()}
    if COLD_ONLY:
        name = f"latency_cold_intent_{settings['intent_mode']}" + (f"_vote{settings['sql_candidates']}" if settings["sql_candidates"] > 1 else "")
        result = {"meta": run_metadata([os.path.join(os.path.dirname(__file__), "..", "data", "domain_qa.py")],
                                       {"status": "complete", "cold_n": n_cold, "spacing_s": SPACING_S, **settings,
                                        "duration_s": round(time.time() - started)}), **out}
        print("saved", save_result(name, result))
        return result

    # ---------------------------------------------------------------- cache paths
    with workspace("latency_cache", semantic_cache=True):
        with TestClient(app) as client:
            from app.cache.semantic_cache import get_semantic_cache
            cache = get_semantic_cache()
            question = QUESTIONS[0][2]
            first = client.post("/query", json={"query": question}, headers=EMPLOYEE).json()
            if first.get("status") != "completed":
                out["cache_error"] = f"seed query not completed: {first.get('status')}"
            else:
                l1 = []
                for _ in range(L1_TRIALS):
                    t = time.perf_counter()
                    body = client.post("/query", json={"query": question}, headers=EMPLOYEE).json()
                    l1.append({"ms": (time.perf_counter() - t) * 1000, "hit": bool(body.get("cached"))})
                out["cache_l1_http"] = {"trials": L1_TRIALS, "hit_rate_pct": round(100 * sum(x["hit"] for x in l1) / L1_TRIALS, 1),
                                        **latency_summary([x["ms"] for x in l1 if x["hit"]])}

                # Similarity path: seed the cache directly with a paraphrase pair's first question (no model call), then
                # ask the second question through the real endpoint. Each lookup embeds the new question live.
                l2, hits = [], 0
                pairs = l2_pairs()
                for qa, qb, sql in pairs:
                    cache.set(qa, sql, {"columns": ["name"], "rows": [{"name": "x"}]}, role="employee")
                    time.sleep(1.0)
                    t = time.perf_counter()
                    r2 = client.post("/query", json={"query": qb}, headers=EMPLOYEE).json()
                    ms = (time.perf_counter() - t) * 1000
                    if r2.get("cached") and r2.get("cache_match_type") == "semantic":
                        hits += 1
                        l2.append(ms)
                out["cache_l2_http"] = {"pairs_tried": len(pairs), "similarity_hits": hits,
                                        **(latency_summary(l2) if l2 else {})}
                out["cache_stats"] = cache.get_stats()

    if cold_p50:
        for key in ("cache_l1_http", "cache_l2_http"):
            if key in out and out[key].get("p50_ms"):
                out[key]["speedup_vs_measured_cold_p50"] = round(cold_p50 / out[key]["p50_ms"], 1)

    result = {"meta": run_metadata([os.path.join(os.path.dirname(__file__), "..", "data", "domain_qa.py")],
                                   {"status": "complete", "cold_n": n_cold, "spacing_s": SPACING_S, **settings,
                                    "duration_s": round(time.time() - started)}), **out}
    print("saved", save_result("latency", result))
    return result
