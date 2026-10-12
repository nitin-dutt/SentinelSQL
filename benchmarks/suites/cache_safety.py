"""
Semantic-cache safety suite.

Question answered: how often does the similarity cache serve the WRONG answer, and what does preventing
that cost in legitimate hits?

For ~62 labelled question pairs (paraphrases that should hit, near-identical questions that must not) the
suite embeds both questions with the production embedding model, then evaluates two cache policies on the
very same similarity scores, across a sweep of similarity thresholds:

  similarity_only   the first cache design: hit when cosine similarity >= threshold
  guarded           the current design: additionally requires equal constraints (numbers, dates, ranking /
                    comparison / aggregation / negation words, named entities) and that every string value
                    in the cached SQL is mentioned in the new question

A final end-to-end check runs the real `SemanticCache` class at the production threshold (0.92) and
verifies it behaves exactly like the analytical `guarded` policy.

Metrics: wrong-hit rate = share of must-not-hit pairs served from the cache (lower is better);
legitimate-hit rate = share of paraphrase pairs served (higher is better).
"""

import os
import tempfile
from collections import defaultdict
from typing import Any, Dict, List, Optional

from benchmarks.lib.meta import run_metadata
from benchmarks.lib.stats import proportion
from benchmarks.suites.common import EMBEDDING_CACHE, save_result, workspace

THRESHOLDS = [0.80, 0.85, 0.88, 0.90, 0.92, 0.94, 0.96, 0.98]
PRODUCTION_THRESHOLD = 0.92


def run(llm: bool = True, limit: Optional[int] = None) -> Dict[str, Any]:
    import numpy as np
    from app.cache.constraints import extract_constraints
    from app.cache.semantic_cache import SemanticCache, _string_literals
    from app.rag.vector_store import EmbeddingUnavailableError, get_embedding_engine
    from benchmarks.data.cache_pairs import PAIRS
    from benchmarks.lib.llm import DailyQuotaExceeded, embedding_harness

    pairs = PAIRS[:limit] if limit else PAIRS
    results: Dict[str, Any] = {}
    status = "complete"
    with workspace("cache_safety", seed=False):
        try:
            with embedding_harness(EMBEDDING_CACHE) as usage:
                engine = get_embedding_engine()
                texts = sorted({t for p in pairs for t in (p[2], p[3])})
                embedded = dict(zip(texts, engine.embed_many(texts)))
                backend = engine.backend_id

                def sim(a: str, b: str) -> float:
                    va, vb = np.asarray(embedded[a][0]), np.asarray(embedded[b][0])
                    return float(va @ vb / (np.linalg.norm(va) * np.linalg.norm(vb)))

                scored: List[Dict[str, Any]] = []
                for pid, kind, qa, qb, should_hit, sql_a in pairs:
                    literals = _string_literals(sql_a) if sql_a else []
                    guards_ok = (extract_constraints(qa) == extract_constraints(qb)
                                 and all(v in qb.lower() for v in literals))
                    scored.append({"id": pid, "kind": kind, "a": qa, "b": qb, "should_hit": should_hit,
                                   "similarity": round(sim(qa, qb), 4), "guards_ok": guards_ok})

                sweep = []
                for thr in THRESHOLDS:
                    row = {"threshold": thr}
                    for policy in ("similarity_only", "guarded"):
                        hits = [(p["similarity"] >= thr) and (policy == "similarity_only" or p["guards_ok"]) for p in scored]
                        neg = [h for h, p in zip(hits, scored) if not p["should_hit"]]
                        pos = [h for h, p in zip(hits, scored) if p["should_hit"]]
                        row[policy] = {"wrong_hit_rate": proportion(sum(neg), len(neg)), "legit_hit_rate": proportion(sum(pos), len(pos))}
                    sweep.append(row)

                at_prod = next(r for r in sweep if r["threshold"] == PRODUCTION_THRESHOLD)

                # end-to-end: the real cache class, same threshold, must agree with the analytical policy
                tmp = tempfile.mkdtemp(prefix="cache-e2e-")
                disagreements = []
                for p, (pid, kind, qa, qb, should_hit, sql_a) in zip(scored, pairs):
                    cache = SemanticCache(cache_file=os.path.join(tmp, f"{pid}.json"), threshold=PRODUCTION_THRESHOLD,
                                          version_provider=lambda: "bench")
                    cache.set(qa, sql_a or "SELECT 1 LIMIT 1;", {"columns": ["x"], "rows": [{"x": 1}]}, role="employee")
                    served = cache.get(qb, role="employee") is not None
                    expected = p["similarity"] >= PRODUCTION_THRESHOLD and p["guards_ok"]
                    if served != expected:
                        disagreements.append({"id": pid, "served": served, "expected": expected, "similarity": p["similarity"]})

                by_kind = defaultdict(list)
                for p in scored:
                    if not p["should_hit"]:
                        by_kind[p["kind"]].append(p)
                per_kind = {k: {"n": len(v),
                                "similarity_only_wrong_hits": sum(p["similarity"] >= PRODUCTION_THRESHOLD for p in v),
                                "guarded_wrong_hits": sum(p["similarity"] >= PRODUCTION_THRESHOLD and p["guards_ok"] for p in v),
                                "mean_similarity": round(sum(p["similarity"] for p in v) / len(v), 3)}
                            for k, v in sorted(by_kind.items())}

                results = {
                    "embedding_backend": backend,
                    "n_pairs": len(scored), "n_paraphrases": sum(p["should_hit"] for p in scored),
                    "n_must_not_hit": sum(not p["should_hit"] for p in scored),
                    "at_production_threshold": at_prod,
                    "threshold_sweep": sweep,
                    "wrong_hits_by_difference_kind": per_kind,
                    "paraphrases_lost_to_guards": [{"id": p["id"], "a": p["a"], "b": p["b"], "similarity": p["similarity"]}
                                                   for p in scored if p["should_hit"] and p["similarity"] >= PRODUCTION_THRESHOLD and not p["guards_ok"]],
                    "wrong_hits_with_similarity_only": [{"id": p["id"], "kind": p["kind"], "a": p["a"], "b": p["b"], "similarity": p["similarity"]}
                                                        for p in scored if not p["should_hit"] and p["similarity"] >= PRODUCTION_THRESHOLD],
                    "wrong_hits_with_guards": [{"id": p["id"], "kind": p["kind"], "a": p["a"], "b": p["b"], "similarity": p["similarity"]}
                                               for p in scored if not p["should_hit"] and p["similarity"] >= PRODUCTION_THRESHOLD and p["guards_ok"]],
                    "similarity_stats": {
                        "paraphrase_mean": round(sum(p["similarity"] for p in scored if p["should_hit"]) / max(1, sum(p["should_hit"] for p in scored)), 3),
                        "must_not_hit_mean": round(sum(p["similarity"] for p in scored if not p["should_hit"]) / max(1, sum(not p["should_hit"] for p in scored)), 3),
                    },
                    "end_to_end_agreement": {"checked": len(scored), "disagreements": disagreements},
                    "pairs": scored,
                    "usage": usage.snapshot(),
                }
        except (DailyQuotaExceeded, EmbeddingUnavailableError) as e:
            status = f"paused: {type(e).__name__}: {str(e)[:300]} (vectors cached; re-run to resume)"
            print("  " + status, flush=True)

    out = {"meta": run_metadata([os.path.join(os.path.dirname(__file__), "..", "data", "cache_pairs.py")],
                                {"status": status, "embedding_backend": results.get("embedding_backend")}), **results}
    if status == "complete":
        # offline-hash embeddings only prove the plumbing; they must never be mistaken for the real result
        name = "cache_safety" if str(results.get("embedding_backend", "")).startswith("gemini") else "cache_safety.dry_run_offline"
        print("saved", save_result(name, out))
    return out
