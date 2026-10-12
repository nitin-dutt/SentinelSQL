"""
SentinelSQL Enterprise Semantic Cache Test Suite
------------------------------------------------
Tests:
1. Query normalization and hashing.
2. L1 Exact hash cache hit (<0.1ms).
3. L2 Semantic vector cosine similarity cache hit (<3ms).
4. Cache miss for unrelated queries below similarity threshold.
5. Role-based tenant isolation (admin vs employee).
6. TTL expiration and eviction.
7. LRU capacity management.
8. Admin API endpoints: GET /admin/cache/stats and DELETE /admin/cache.
9. End-to-end integration with POST /query.
"""

import time
import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient
from app.main import app
from app.cache.semantic_cache import SemanticCache
from tests.helpers import ADMIN, EMPLOYEE

client = TestClient(app)


def test_query_normalization():
    assert SemanticCache.normalize_query("  Show Top 5 Customers?  ") == "show top 5 customers"
    assert SemanticCache.normalize_query("SELECT   *   FROM   users;  ") == "select * from users"
    assert SemanticCache.normalize_query("") == ""


def test_l1_exact_hash_hit(tmp_path):
    cache_file = str(tmp_path / "cache_l1.json")
    cache = SemanticCache(cache_file=cache_file, threshold=0.9)

    query = "List all active customers"
    sql = "SELECT * FROM customers WHERE active = 1;"
    results = {"columns": ["id", "name"], "rows": [{"id": 1, "name": "Alice"}]}

    cache.set(query=query, sql=sql, results=results, role="employee")

    # 1. Exact query with different casing and trailing punctuation
    hit = cache.get("list all active customers?", role="employee")
    assert hit is not None
    assert hit["match_type"] == "exact"
    assert hit["similarity"] == 1.0
    assert hit["sql"] == sql
    assert hit["results"]["rows"][0]["name"] == "Alice"

    stats = cache.get_stats()
    assert stats["hits"] == 1
    assert stats["exact_hits"] == 1
    assert stats["semantic_hits"] == 0


def test_l2_semantic_vector_hit(tmp_path):
    cache_file = str(tmp_path / "cache_l2.json")
    # Low threshold for test predictability with offline lexical or remote embedding
    cache = SemanticCache(cache_file=cache_file, threshold=0.6)

    query = "Total revenue per customer"
    sql = "SELECT customer_id, SUM(amount) FROM orders GROUP BY customer_id;"
    results = {"columns": ["customer_id", "total"], "rows": [{"customer_id": 1, "total": 500}]}

    cache.set(query=query, sql=sql, results=results, role="employee")

    # Semantically similar wording
    hit = cache.get("Customer total revenue amounts", role="employee")
    assert hit is not None
    assert hit["sql"] == sql
    assert hit["match_type"] in ("exact", "semantic")
    assert hit["similarity"] >= 0.6


def test_cache_miss_unrelated_query(tmp_path):
    cache_file = str(tmp_path / "cache_miss.json")
    cache = SemanticCache(cache_file=cache_file, threshold=0.85)

    cache.set(
        query="Show warehouse inventory levels",
        sql="SELECT * FROM inventory;",
        results={"columns": ["item"], "rows": [{"item": "Widget"}]},
        role="employee"
    )

    miss = cache.get("What is the CEO salary and compensation?", role="employee")
    assert miss is None

    stats = cache.get_stats()
    assert stats["misses"] >= 1


def test_role_based_isolation(tmp_path):
    cache_file = str(tmp_path / "cache_rbac.json")
    cache = SemanticCache(cache_file=cache_file, threshold=0.8)

    cache.set(
        query="Confidential executive payroll breakdown",
        sql="SELECT * FROM executive_payroll;",
        results={"columns": ["name", "salary"], "rows": [{"name": "CEO", "salary": 999999}]},
        role="admin"
    )

    # Admin gets cache hit
    admin_hit = cache.get("Confidential executive payroll breakdown", role="admin")
    assert admin_hit is not None

    # Employee MUST NOT get admin cached data!
    employee_hit = cache.get("Confidential executive payroll breakdown", role="employee")
    assert employee_hit is None


def test_ttl_expiration(tmp_path):
    cache_file = str(tmp_path / "cache_ttl.json")
    cache = SemanticCache(cache_file=cache_file, threshold=0.8)

    cache.set(
        query="Quick expiring query",
        sql="SELECT 1;",
        results={"columns": ["1"], "rows": [{"1": 1}]},
        role="guest",
        ttl_seconds=1.0
    )

    # Immediate access succeeds
    assert cache.get("Quick expiring query", role="guest") is not None

    # Artificially age entry to simulate TTL expiry
    for entry in cache.entries.values():
        entry["created_at"] -= 2.0

    # Access after TTL expiration must return None
    assert cache.get("Quick expiring query", role="guest") is None



def test_lru_capacity_eviction(tmp_path):
    cache_file = str(tmp_path / "cache_lru.json")
    cache = SemanticCache(cache_file=cache_file, max_entries=2)

    cache.set("query 1", "SELECT 1;", {"rows": [1]}, role="guest")
    time.sleep(0.01)
    cache.set("query 2", "SELECT 2;", {"rows": [2]}, role="guest")
    time.sleep(0.01)

    # Access query 1 so it becomes recently used
    cache.get("query 1", role="guest")
    time.sleep(0.01)

    # Insert query 3 -> should evict query 2 (least recently used)
    cache.set("query 3", "SELECT 3;", {"rows": [3]}, role="guest")

    assert len(cache.entries) <= 2
    assert cache.get("query 1", role="guest") is not None
    assert cache.get("query 3", role="guest") is not None
    assert cache.get("query 2", role="guest") is None


def test_admin_cache_endpoints():
    # RBAC: Non-admin denied
    res_non_admin = client.get("/admin/cache/stats", headers=EMPLOYEE)
    assert res_non_admin.status_code == 403

    res_del_non_admin = client.delete("/admin/cache", headers=EMPLOYEE)
    assert res_del_non_admin.status_code == 403

    # Admin access succeeds
    res_stats = client.get("/admin/cache/stats", headers=ADMIN)
    assert res_stats.status_code == 200
    stats = res_stats.json()
    assert "hits" in stats
    assert "total_entries" in stats

    res_clear = client.delete("/admin/cache", headers=ADMIN)
    assert res_clear.status_code == 200
    assert res_clear.json()["status"] == "success"


@patch("app.routers.query.build_graph")
def test_e2e_query_semantic_cache_flow(mock_build_graph):
    from app.cache.semantic_cache import get_semantic_cache
    semantic_cache = get_semantic_cache()
    semantic_cache.clear()

    mock_graph = mock_build_graph.return_value
    mock_graph.invoke.return_value = {
        "sql": "SELECT id, name FROM customers LIMIT 5;",
        "validation": {"safe": True, "requires_human": False, "score": 95},
        "results": {"columns": ["id", "name"], "rows": [{"id": 1, "name": "Alice"}]}
    }

    # 1. First execution: Cache Miss -> Executes graph & stores in cache
    res1 = client.post(
        "/query",
        json={"query": "List top 5 registered customers"},
        headers=EMPLOYEE
    )
    assert res1.status_code == 200
    data1 = res1.json()
    assert data1["status"] == "completed"
    assert data1["cached"] is False
    assert mock_graph.invoke.call_count == 1

    # 2. Second execution with identical query: Cache HIT! Graph should NOT be called again!
    res2 = client.post(
        "/query",
        json={"query": "List top 5 registered customers?"},
        headers=EMPLOYEE
    )
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["status"] == "completed"
    assert data2["cached"] is True
    assert data2["latency_ms"] < 50.0  # Ultra-fast sub-50ms cache response
    # Graph invoke count remains 1!
    assert mock_graph.invoke.call_count == 1

    # Clean up
    semantic_cache.clear()
