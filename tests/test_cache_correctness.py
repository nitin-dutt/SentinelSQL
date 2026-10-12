"""
Regression tests for alignment_plan G7 (CAC-01 .. CAC-04): the semantic cache must not serve a
different question's answer, must never mix embedding backends silently, must stop serving
answers after a policy or data change, and must not do slow work while holding its lock.
"""

import os
import re
import threading
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.cache import semantic_cache as sc
from app.cache.constraints import extract_constraints
from app.cache.semantic_cache import SemanticCache
from app.main import app
from app.rag.vector_store import EmbeddedVectorStore, EmbeddingEngine, OFFLINE_BACKEND, _offline_embedding
from tests.helpers import ADMIN, EMPLOYEE

client = TestClient(app)
RESULTS = {"columns": ["n"], "rows": [{"n": 1}]}


class StubEngine:
    """Deterministic embeddings that, like real ones, barely notice digits: '5' and '10' look alike."""

    def __init__(self, backend="stub-a"):
        self.backend_id = backend
        self.calls = 0

    def embed(self, text):
        self.calls += 1
        words = re.sub(r"\d+", "", text.lower())
        return _offline_embedding(words), self.backend_id

    def embed_text(self, text):
        return self.embed(text)[0]

    def status(self):
        return {"provider": "stub", "backend": self.backend_id, "degraded": False, "fallback_calls": 0}


@pytest.fixture
def cache(tmp_path):
    return SemanticCache(cache_file=str(tmp_path / "c.json"), threshold=0.9, engine=StubEngine(),
                         version_provider=lambda: "v1")


@pytest.fixture
def permissive(tmp_path):
    """Similarity threshold 0: every cached entry is a candidate, so only the safety guards decide."""
    return SemanticCache(cache_file=str(tmp_path / "p.json"), threshold=0.0, engine=StubEngine(),
                         version_provider=lambda: "v1")


def store(cache, question, sql="SELECT n FROM t LIMIT 5;", role="employee"):
    return cache.set(question, sql, RESULTS, role=role)


# ---------------------------------------------------------------- CAC-01: constraints

@pytest.mark.parametrize("a,b,same", [
    ("Top 5 customers by revenue", "Top 10 customers by revenue", False),
    ("Top five customers by revenue", "top 5 customers by revenue", True),
    ("Orders placed in 2024", "Orders placed in 2025", False),
    ("Show orders for last month", "Show orders for this month", False),
    ("Customers with more than 3 orders", "Customers with fewer than 3 orders", False),
    ("Total revenue per customer", "Customer total revenue amounts", True),
    ("Average order amount", "Total order amount", False),
    ("How many customers are in North?", "How many customers are in South?", False),
    ("Revenue in January", "Revenue in March", False),
    ("Revenue in Q1", "Revenue in Q2", False),
    ("Orders not shipped", "Orders shipped", False),
    ("Show customers and their regions", "List every customer with the region", True),
])
def test_constraint_extraction(a, b, same):
    assert (extract_constraints(a) == extract_constraints(b)) is same


@pytest.mark.parametrize("cached_q,new_q", [
    ("Top 5 customers by revenue", "Top 10 customers by revenue"),
    ("Orders placed in 2024", "Orders placed in 2025"),
    ("Show orders for last month", "Show orders for this month"),
    ("Customers with more than 3 orders", "Customers with fewer than 3 orders"),
    ("Average order amount per customer", "Total order amount per customer"),
    ("How many customers are in North", "How many customers are in South"),
])
def test_similar_looking_but_different_questions_do_not_share_answers(permissive, cached_q, new_q):
    store(permissive, cached_q)
    assert permissive.get(new_q, role="employee") is None      # even when similarity alone would allow it
    assert permissive.get_stats()["semantic_rejected"] == 1


def test_the_stub_really_would_have_matched_without_the_check(cache):
    """Guards the tests above: similarity alone is high enough to hit."""
    v1, _ = cache.engine.embed("top 5 customers by revenue")
    v2, _ = cache.engine.embed("top 10 customers by revenue")
    from app.rag.vector_store import cosine_similarity
    assert cosine_similarity(v1, v2) > 0.99


def test_equivalent_phrasings_with_identical_constraints_still_hit(permissive):
    store(permissive, "Top five customers by revenue")
    hit = permissive.get("top 5 customers by revenue", role="employee")
    assert hit is not None and hit["match_type"] == "semantic"


def test_a_lowercase_name_cannot_borrow_another_names_answer(permissive):
    """Entity detection needs capitals; the SQL-literal guard covers typed-lowercase names."""
    store(permissive, "orders for alice", sql="SELECT n FROM orders WHERE customer = 'alice' LIMIT 100;")
    assert permissive.get("orders for bob", role="employee") is None
    assert permissive.get("orders for alice", role="employee")["match_type"] == "exact"


def test_semantic_hit_requires_the_cached_sql_values_to_be_in_the_question(permissive):
    store(permissive, "show customers in the north region", sql="SELECT n FROM c WHERE region = 'north' LIMIT 100;")
    assert permissive.get("show customers in the south region", role="employee") is None
    assert permissive.get("show the customers in the north region", role="employee") is not None


def test_semantic_hits_report_how_they_matched(cache):
    store(cache, "show the total revenue by customer")
    hit = cache.get("show total revenue by the customer", role="employee")
    assert hit["match_type"] == "semantic" and hit["original_query"] == "show the total revenue by customer"
    assert 0.9 <= hit["similarity"] <= 1.0


def test_api_exposes_match_provenance(tmp_path, monkeypatch):
    local = SemanticCache(cache_file=str(tmp_path / "c.json"), threshold=0.9, engine=StubEngine(), version_provider=lambda: "v1")
    monkeypatch.setattr(sc, "_GLOBAL_SEMANTIC_CACHE", local)
    local.set("show the total revenue by customer", "SELECT n FROM t LIMIT 5;", RESULTS, role="employee")
    r = client.post("/query", json={"query": "show total revenue by the customer"}, headers=EMPLOYEE).json()
    assert r["cached"] is True and r["cache_match_type"] == "semantic"
    assert r["cache_original_query"] == "show the total revenue by customer" and r["cache_similarity"] >= 0.9


# ---------------------------------------------------------------- CAC-02: embedding backends

class FlakyRemote:
    def __init__(self):
        self.fail = True
        self.calls = 0

    def embed_query(self, text):
        self.calls += 1
        if self.fail:
            raise ConnectionError("provider down")
        return [0.1] * 8


def remote_engine(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    engine = EmbeddingEngine()
    engine.provider, engine.backend_id, engine.client = "gemini", "gemini:test-model", FlakyRemote()
    return engine


def test_offline_engine_is_reported_as_offline_not_degraded(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    status = EmbeddingEngine().status()
    assert status["backend"] == OFFLINE_BACKEND and status["degraded"] is False and status["configured_remote"] is False


def test_remote_failure_is_visible_tagged_and_backed_off(monkeypatch):
    engine = remote_engine(monkeypatch)
    vec, backend = engine.embed("hello world")
    assert backend == OFFLINE_BACKEND and len(vec) == 128            # tagged as the fallback it is
    status = engine.status()
    assert status["degraded"] is True and status["fallback_calls"] == 1 and "provider down" not in (status["last_error"] or "x")
    calls_after_first = engine.client.calls
    engine.embed("another question")
    assert engine.client.calls == calls_after_first                  # cool-down: no failing call per request


def test_engine_recovers_and_resumes_the_real_backend(monkeypatch):
    engine = remote_engine(monkeypatch)
    engine.embed("q")
    engine.client.fail = False
    engine._remote_blocked_until = 0.0
    vec, backend = engine.embed("q2")
    assert backend == "gemini:test-model" and len(vec) == 8 and engine.status()["degraded"] is False


def test_documents_are_re_embedded_when_the_backend_changes(tmp_path):
    store_ = EmbeddedVectorStore(cache_file=str(tmp_path / "v.json"), engine=StubEngine("a"))
    assert store_.add_or_update_document("d", "customer emails") is True
    assert store_.add_or_update_document("d", "customer emails") is False        # same content + backend: reuse
    store_._engine = StubEngine("b")
    assert store_.add_or_update_document("d", "customer emails") is True         # same content, new backend: re-embed
    assert store_.documents["d"]["backend"] == "b"


def test_legacy_documents_without_a_backend_tag_are_re_embedded(tmp_path):
    store_ = EmbeddedVectorStore(cache_file=str(tmp_path / "v.json"), engine=StubEngine("a"))
    store_.add_or_update_document("d", "x")
    del store_.documents["d"]["backend"]
    assert store_.add_or_update_document("d", "x") is True


def test_search_never_compares_vectors_from_different_backends(tmp_path):
    store_ = EmbeddedVectorStore(cache_file=str(tmp_path / "v.json"), engine=StubEngine("a"))
    store_.add_or_update_document("d1", "customer emails")
    store_.add_or_update_document("d2", "order totals")
    store_._engine = StubEngine("b")
    assert store_.search("customer emails") == []
    assert store_.last_search_info["skipped_incompatible"] == 2
    store_.add_or_update_document("d1", "customer emails")                        # re-embedded under b
    assert [r["id"] for r in store_.search("customer emails")] == ["d1"]
    assert store_.status()["stale_documents"] == 1


def test_cache_does_not_semantically_match_across_backends(cache):
    store(cache, "show the total revenue by customer")
    cache._engine = StubEngine("stub-b")                                           # provider switched / degraded
    assert cache.get("show total revenue by the customer", role="employee") is None
    assert cache.get("show the total revenue by customer", role="employee")["match_type"] == "exact"
    assert cache.get_stats()["entries_with_other_embedding_backend"] == 1


def test_admin_embeddings_endpoint(monkeypatch):
    assert client.get("/admin/embeddings", headers=EMPLOYEE).status_code == 403
    body = client.get("/admin/embeddings", headers=ADMIN).json()
    assert {"engine", "indexes", "semantic_cache"} <= set(body)
    assert {"backend", "degraded", "fallback_calls"} <= set(body["engine"])
    assert {"schema", "golden_queries", "data_dictionary"} == set(body["indexes"])
    assert "embedding" in client.get("/admin/cache/stats", headers=ADMIN).json()


# ---------------------------------------------------------------- CAC-03: invalidation

def test_entries_stop_being_served_when_the_cache_version_changes(tmp_path):
    version = {"v": "policy1|data1"}
    cache = SemanticCache(cache_file=str(tmp_path / "c.json"), engine=StubEngine(), version_provider=lambda: version["v"])
    store(cache, "list products")
    assert cache.get("list products", role="employee") is not None
    version["v"] = "policy1|data2"                                 # a write happened
    assert cache.get("list products", role="employee") is None
    assert cache.get_stats()["stale_evictions"] == 1 and cache.get_stats()["total_entries"] == 0


def test_stale_entries_are_not_served_semantically_either(tmp_path):
    version = {"v": "1"}
    cache = SemanticCache(cache_file=str(tmp_path / "c.json"), threshold=0.9, engine=StubEngine(), version_provider=lambda: version["v"])
    store(cache, "show the total revenue by customer")
    version["v"] = "2"
    assert cache.get("show total revenue by the customer", role="employee") is None


def test_tightening_the_policy_invalidates_cached_answers(tmp_path, monkeypatch):
    policy = tmp_path / "policy.json"
    policy.write_text('{"sensitive_tables": ["salaries"]}')
    monkeypatch.setenv("SENSITIVITY_CONFIG", str(policy))
    cache = SemanticCache(cache_file=str(tmp_path / "c.json"), engine=StubEngine())   # default provider
    store(cache, "list customers")
    assert cache.get("list customers", role="employee") is not None
    policy.write_text('{"sensitive_tables": ["salaries", "customers"]}')               # customers is now sensitive
    os.utime(policy, (time.time() + 5, time.time() + 5))
    assert cache.get("list customers", role="employee") is None


def test_policy_fingerprint_tracks_the_policy_file(tmp_path, monkeypatch):
    from app.utils.sql_parser import get_policy_fingerprint
    policy = tmp_path / "p.json"
    policy.write_text('{"sensitive_tables": ["a"]}')
    monkeypatch.setenv("SENSITIVITY_CONFIG", str(policy))
    before = get_policy_fingerprint()
    assert before == get_policy_fingerprint()
    policy.write_text('{"sensitive_tables": ["a", "b"]}')
    os.utime(policy, (time.time() + 5, time.time() + 5))
    assert get_policy_fingerprint() != before


def test_data_version_changes_when_the_database_file_is_written(tmp_path, monkeypatch):
    import sqlite3
    from app.db import database as db
    path = tmp_path / "d.db"
    sqlite3.connect(path).executescript("CREATE TABLE t (x INTEGER);")
    monkeypatch.setenv("ACTIVE_DB", "sqlite")
    monkeypatch.setenv("DATABASE_URL_SQLITE", f"sqlite:///{path.as_posix()}")
    monkeypatch.setenv("DATA_VERSION_CHECK_SECONDS", "0")
    before = db.get_data_version()
    time.sleep(0.05)
    con = sqlite3.connect(path)
    con.execute("INSERT INTO t VALUES (1)")
    con.commit()
    con.close()
    assert db.get_data_version() != before
    monkeypatch.setenv("ACTIVE_DB", "postgres")
    monkeypatch.setenv("DATABASE_URL_POSTGRES", "postgresql+psycopg2://u:p@h/db")
    assert db.get_data_version() == "untracked"


def test_admin_changes_clear_the_cache(tmp_path, monkeypatch):
    local = SemanticCache(cache_file=str(tmp_path / "c.json"), engine=StubEngine(), version_provider=lambda: "v1")
    monkeypatch.setattr(sc, "_GLOBAL_SEMANTIC_CACHE", local)
    store(local, "list products")
    created = client.post("/admin/dictionary", headers=ADMIN, json={
        "term": "Cache Probe Term", "definition": "d", "formula_or_rule": "COUNT(orders.order_id)",
        "related_tables": "orders", "category": "metric"}).json()
    assert local.get_stats()["total_entries"] == 0
    store(local, "list products")
    client.delete(f"/admin/dictionary/{created['id']}", headers=ADMIN)
    assert local.get_stats()["total_entries"] == 0


def test_roles_are_still_partitioned(cache):
    store(cache, "executive payroll breakdown", role="admin")
    assert cache.get("executive payroll breakdown", role="employee") is None


# ---------------------------------------------------------------- CAC-04: locking and performance

def test_embedding_never_happens_while_the_lock_is_held(tmp_path):
    observed = []

    class Watching(StubEngine):
        def embed(self, text):
            observed.append(cache.lock.locked())
            return super().embed(text)

    cache = SemanticCache(cache_file=str(tmp_path / "c.json"), engine=Watching(), version_provider=lambda: "v1")
    store(cache, "list products")
    cache.get("list products please", role="employee")     # L1 miss -> embeds
    cache.get("something else entirely", role="employee")
    assert observed and not any(observed)


def test_disk_writes_never_happen_while_the_lock_is_held(tmp_path):
    cache = SemanticCache(cache_file=str(tmp_path / "c.json"), engine=StubEngine(), version_provider=lambda: "v1")
    held = []
    original = cache._write_snapshot
    cache._write_snapshot = lambda snap: (held.append(cache.lock.locked()), original(snap))[1]
    store(cache, "list products")
    cache.clear()
    assert held and not any(held)


def test_a_slow_embedding_does_not_block_other_requests(tmp_path):
    class Slow(StubEngine):
        def embed(self, text):
            if "slow" in text:
                time.sleep(0.6)
            return super().embed(text)

    cache = SemanticCache(cache_file=str(tmp_path / "c.json"), engine=Slow(), version_provider=lambda: "v1")
    store(cache, "list products")
    slow = threading.Thread(target=lambda: cache.get("a slow unrelated question", role="employee"))
    slow.start()
    time.sleep(0.1)
    started = time.perf_counter()
    assert cache.get("list products", role="employee") is not None     # L1 hit while the other thread is embedding
    assert time.perf_counter() - started < 0.3
    slow.join()


def test_saves_are_throttled_flushable_and_atomic(tmp_path, monkeypatch):
    monkeypatch.setenv("SEMANTIC_CACHE_SAVE_INTERVAL_SECONDS", "60")
    path = tmp_path / "c.json"
    cache = SemanticCache(cache_file=str(path), engine=StubEngine(), version_provider=lambda: "v1")
    store(cache, "first question")
    store(cache, "second question")
    on_disk = SemanticCache(cache_file=str(path), engine=StubEngine(), version_provider=lambda: "v1")
    assert on_disk.get_stats()["total_entries"] == 1                    # second write deferred
    cache.flush()
    reloaded = SemanticCache(cache_file=str(path), engine=StubEngine(), version_provider=lambda: "v1")
    assert reloaded.get_stats()["total_entries"] == 2
    assert not os.path.exists(f"{path}.tmp")
    assert reloaded.get("second question", role="employee")["sql"]


def test_numpy_and_pure_python_scans_agree(tmp_path, monkeypatch):
    cache = SemanticCache(cache_file=str(tmp_path / "c.json"), threshold=0.9, engine=StubEngine(), version_provider=lambda: "v1")
    for q in ("show the total revenue by customer", "list all products", "count open tickets"):
        store(cache, q)
    with_numpy = cache.get("show total revenue by the customer", role="employee")
    cache._matrices.clear()
    monkeypatch.setattr(sc, "np", None)
    without_numpy = cache.get("show total revenue by the customer", role="employee")
    assert with_numpy["original_query"] == without_numpy["original_query"]
    assert with_numpy["similarity"] == pytest.approx(without_numpy["similarity"], abs=1e-3)


def test_lookup_stays_fast_with_a_full_cache_of_realistic_vectors(tmp_path):
    import random

    class Wide(StubEngine):
        def embed(self, text):
            rng = random.Random(text)
            return [rng.uniform(-1, 1) for _ in range(768)], self.backend_id

    cache = SemanticCache(cache_file=str(tmp_path / "c.json"), max_entries=500, engine=Wide(), version_provider=lambda: "v1")
    for i in range(500):
        store(cache, f"distinct question number {i} about topic {i * 7}")
    cache.get("warm up the matrix", role="employee")
    started = time.perf_counter()
    for _ in range(20):
        cache.get("a brand new question that matches nothing", role="employee")
    per_lookup_ms = (time.perf_counter() - started) / 20 * 1000
    assert per_lookup_ms < 25, f"{per_lookup_ms:.1f} ms per lookup"
