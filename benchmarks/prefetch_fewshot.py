"""
Pre-loads everything the few-shot variants need that costs EMBEDDING quota but no generation quota: the 500-example pool in the golden
store, and the question vectors of the hold-out and main samples. Safe to stop and re-run (every vector is cached).

    python -m benchmarks.prefetch_fewshot
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from benchmarks.suites.common import EMBEDDING_CACHE, WORK_DIR  # noqa: E402


def main() -> int:
    pool_dir = os.path.join(WORK_DIR, "spider_pool")
    os.makedirs(pool_dir, exist_ok=True)
    os.environ["GOLDEN_DB_PATH"] = os.path.join(pool_dir, "golden.db")
    os.environ["GOLDEN_VECTOR_CACHE"] = os.path.join(pool_dir, "golden_vectors.json")

    import app  # noqa: F401  (loads .env)
    from app.rag.vector_store import EmbeddingUnavailableError, get_embedding_engine
    from benchmarks.lib import fewshot
    from benchmarks.lib.llm import DailyQuotaExceeded, embedding_harness
    from benchmarks.suites import spider as spider_suite

    pool = fewshot.build_pool()
    fewshot.assert_disjoint_from_dev(pool)
    try:
        with embedding_harness(EMBEDDING_CACHE) as usage:
            print(f"pool: {len(pool)} examples; newly loaded: {fewshot.seed_golden_store(pool)}", flush=True)
            engine = get_embedding_engine()
            for holdout in (True, False):
                spider_suite.HOLDOUT = holdout
                spider_suite.SEED = 43 if holdout else 42
                spider_suite.PER_TIER = 25 if holdout else 50
                questions = spider_suite.sample_questions(per_tier=spider_suite.PER_TIER, seed=spider_suite.SEED)
                for n, q in enumerate(questions, 1):
                    engine.embed(q["question"])
                    if n % 25 == 0:
                        print(f"  {'hold-out' if holdout else 'main'} question vectors: {n}/{len(questions)}", flush=True)
            print("done.", usage.snapshot(), flush=True)
    except (DailyQuotaExceeded, EmbeddingUnavailableError) as e:
        print(f"paused (embedding quota): {str(e)[:160]}; re-run to resume.", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
