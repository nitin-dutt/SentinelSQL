"""
Shared plumbing for the benchmark suites.

`workspace()` builds an isolated, freshly seeded copy of the demo environment (database, audit log, HITL
store, glossary and golden stores, vector caches) in benchmarks/.work/, points the application at it,
and restores the previous environment afterwards. Nothing a benchmark does can touch your real
sentinel.db, audit.db, caches or .env state.
"""

import contextlib
import json
import os
import shutil
import sys
import time
from typing import Any, Dict, Iterator, Optional

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

BENCH_DIR = os.path.join(ROOT, "benchmarks")
RESULTS_DIR = os.path.join(BENCH_DIR, "results")
CACHE_DIR = os.path.join(BENCH_DIR, "cache")            # LLM + embedding response caches: commit these for offline reproduction
WORK_DIR = os.path.join(BENCH_DIR, ".work")             # throwaway databases and indexes (git-ignored)
# BENCH_LLM_CACHE / BENCH_EMBEDDING_CACHE redirect the caches (used by tests and stub dry-runs so fake responses never reach the real ones)
LLM_CACHE = os.getenv("BENCH_LLM_CACHE", os.path.join(CACHE_DIR, "llm_responses.jsonl"))
EMBEDDING_CACHE = os.getenv("BENCH_EMBEDDING_CACHE", os.path.join(CACHE_DIR, "embeddings.jsonl"))

BENCH_TOKENS = "bench-employee:bench-emp:employee,bench-admin:bench-admin:admin,bench-agent:bench-agent:external_ai"
EMPLOYEE = {"Authorization": "Bearer bench-employee"}
ADMIN = {"Authorization": "Bearer bench-admin"}
AGENT = {"Authorization": "Bearer bench-agent"}

_MANAGED_ENV = (
    "ACTIVE_DB", "DATABASE_URL_SQLITE", "DATABASE_URL", "READONLY_DATABASE_URL", "AUDIT_DB_PATH", "HITL_DB_PATH",
    "DICTIONARY_DB_PATH", "GOLDEN_DB_PATH", "DICTIONARY_VECTOR_CACHE", "GOLDEN_VECTOR_CACHE", "AUTH_TOKENS",
    "SEMANTIC_CACHE_ENABLED", "SEED_DEMO_DATA", "LLM_MAX_RETRIES",
)


def ensure_dirs() -> None:
    for d in (RESULTS_DIR, CACHE_DIR, WORK_DIR):
        os.makedirs(d, exist_ok=True)


@contextlib.contextmanager
def workspace(name: str, semantic_cache: bool = False, seed: bool = True) -> Iterator[str]:
    """Yields the path of an isolated demo database; the application is pointed at it for the block."""
    ensure_dirs()
    folder = os.path.join(WORK_DIR, name)
    shutil.rmtree(folder, ignore_errors=True)
    os.makedirs(folder)
    db_file = os.path.join(folder, "demo.db")

    saved = {k: os.environ.get(k) for k in _MANAGED_ENV}
    os.environ.update({
        "ACTIVE_DB": "sqlite",
        "DATABASE_URL_SQLITE": f"sqlite:///{db_file.replace(os.sep, '/')}",
        "AUDIT_DB_PATH": os.path.join(folder, "audit.db"),
        "HITL_DB_PATH": os.path.join(folder, "hitl.db"),
        "DICTIONARY_VECTOR_CACHE": os.path.join(folder, "dictionary_vectors.json"),
        "GOLDEN_VECTOR_CACHE": os.path.join(folder, "golden_vectors.json"),
        "AUTH_TOKENS": BENCH_TOKENS,
        "SEMANTIC_CACHE_ENABLED": "true" if semantic_cache else "false",
        "SEED_DEMO_DATA": "true" if seed else "false",
    })
    for k in ("DATABASE_URL", "READONLY_DATABASE_URL", "DICTIONARY_DB_PATH", "GOLDEN_DB_PATH"):
        os.environ.pop(k, None)

    _reset_app_state()
    try:
        if seed:
            from app.db.database import init_db
            init_db()
        yield db_file
    finally:
        _reset_app_state()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _reset_app_state() -> None:
    """Forget every process-wide singleton that caches the previous environment."""
    from app.db import database
    from app.graph import workflow
    from app.rag import data_dictionary, golden_store, schema_retriever, vector_store
    from app.cache import semantic_cache
    from app.utils.schemacontext import clear_schema_cache

    database.reset_engines()
    clear_schema_cache()
    workflow.build_graph.cache_clear()
    data_dictionary._INITIALIZED.clear(); data_dictionary._VECTOR_STORE = None
    golden_store._INITIALIZED.clear(); golden_store._VECTOR_STORE = None
    vector_store._ENGINE = None; vector_store._ENGINE_SIGNATURE = None
    semantic_cache._GLOBAL_SEMANTIC_CACHE = None
    # the global schema retriever indexes whatever database was configured when it was created
    schema_retriever._GLOBAL_RETRIEVER = schema_retriever.SchemaRAGRetriever(
        cache_file=os.path.join(WORK_DIR, "schema_vectors.json"))


def save_result(name: str, payload: Dict[str, Any]) -> str:
    ensure_dirs()
    path = os.path.join(RESULTS_DIR, f"{name}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, default=str)
    return path


def load_result(name: str) -> Optional[Dict[str, Any]]:
    path = os.path.join(RESULTS_DIR, f"{name}.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def progress(label: str, i: int, n: int, started: float) -> None:
    if i == 1 or i % 10 == 0 or i == n:
        elapsed = time.time() - started
        print(f"  [{label}] {i}/{n}  ({elapsed:.0f}s)", flush=True)
