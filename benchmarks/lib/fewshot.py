"""
Few-shot example pool drawn from the Spider TRAIN split.

SentinelSQL's "golden queries" are admin-verified question/SQL pairs that are retrieved by similarity and shown to the model as
examples. To measure how much verified examples help, this module builds a pool of Spider training questions and loads it into the
product's own golden store, so the retrieval and prompt block under test are the production ones, not a benchmark imitation.

Design choices fixed BEFORE any hold-out result was seen (so they are not tuned on it):
  * 500 examples, 125 per official hardness tier, drawn with a fixed seed;
  * 3 examples retrieved per question (FEWSHOT_K in the suite);
  * the examples come from the 140 training databases; the dev databases are disjoint by Spider's design, which
    `assert_disjoint_from_dev` re-checks, so no dev schema or answer can leak into a prompt.
Public benchmarks may also have been seen by the model during its own training; that cannot be controlled here.
"""

import json
import os
import random
import re
from typing import Any, Dict, List

from benchmarks.lib import spider as sp

TRAIN_JSON = os.path.join(sp.SPIDER_DIR, "train_spider.json")
POOL_SIZE = 500
POOL_SEED = 7
TIERS = ["easy", "medium", "hard", "extra"]


def normalise_sql(sql: str) -> str:
    """Spider's gold SQL has irregular spacing (`age  >  56`); collapse it so the example reads like ordinary SQL."""
    return re.sub(r"\s+", " ", sql).strip()


def load_train() -> List[Dict[str, Any]]:
    with open(TRAIN_JSON, "r", encoding="utf-8") as f:
        return json.load(f)


def build_pool(size: int = POOL_SIZE, seed: int = POOL_SEED, train: List[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """A stratified, reproducible sample of the training questions: `size // 4` per hardness tier."""
    train = train if train is not None else load_train()
    by_tier: Dict[str, List[int]] = {t: [] for t in TIERS}
    for i, ex in enumerate(train):
        by_tier[sp.eval_hardness(ex["sql"])].append(i)
    rng = random.Random(seed)
    per_tier = size // len(TIERS)
    pool = []
    for tier in TIERS:
        for i in sorted(rng.sample(by_tier[tier], min(per_tier, len(by_tier[tier])))):
            ex = train[i]
            pool.append({"index": i, "db_id": ex["db_id"], "hardness": tier,
                         "question": ex["question"].strip(), "sql": normalise_sql(ex["query"])})
    return pool


def assert_disjoint_from_dev(pool: List[Dict[str, Any]], dev: List[Dict[str, Any]] = None) -> None:
    dev = dev if dev is not None else sp.load_dev()
    shared = {e["db_id"] for e in pool} & {d["db_id"] for d in dev}
    assert not shared, f"few-shot pool shares databases with the dev set: {sorted(shared)}"


def seed_golden_store(pool: List[Dict[str, Any]]) -> int:
    """
    Loads the pool into the product's golden store (SQLite table + vector index). Idempotent: a complete pool is left alone, an
    incomplete one is rebuilt (embeddings come from the benchmark's embedding cache, so a rebuild is cheap). The caller must have
    pointed GOLDEN_DB_PATH and GOLDEN_VECTOR_CACHE at throwaway files and activated the embedding harness.
    """
    from app.rag import golden_store

    def reset_state():
        golden_store._INITIALIZED.clear()
        golden_store._VECTOR_STORE = None

    reset_state()
    if len(golden_store.get_all_golden_queries()) == len(pool):
        return 0
    # Empty the table through the store's own connection (Windows keeps the database file locked while a connection is open),
    # and drop the vector index so it is rebuilt together with the rows.
    conn = golden_store._connect()
    try:
        conn.execute("DELETE FROM golden_queries")
        conn.commit()
    finally:
        conn.close()
    if os.path.exists(os.environ["GOLDEN_VECTOR_CACHE"]):
        os.remove(os.environ["GOLDEN_VECTOR_CACHE"])
    reset_state()
    for ex in pool:
        golden_store.save_golden_query(ex["question"], ex["sql"], created_by="spider-train")
    return len(pool)
