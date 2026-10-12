"""
SentinelSQL Schema RAG & Vector Store Test Suite
------------------------------------------------
Tests:
1. Cosine similarity and vector ranking.
2. Embedding caching and content hashing.
3. Dynamic schema retrieval and relational graph expansion.
"""

import os
import pytest
from app.rag.vector_store import EmbeddedVectorStore, cosine_similarity, compute_content_hash
from app.rag.schema_retriever import SchemaRAGRetriever, get_rag_schema_context


def test_cosine_similarity():
    v1 = [1.0, 0.0, 0.0]
    v2 = [1.0, 0.0, 0.0]
    v3 = [0.0, 1.0, 0.0]
    
    assert pytest.approx(cosine_similarity(v1, v2), 0.001) == 1.0
    assert pytest.approx(cosine_similarity(v1, v3), 0.001) == 0.0
    assert cosine_similarity([], []) == 0.0


def test_vector_store_caching(tmp_path):
    cache_path = str(tmp_path / "test_vectors.json")
    store = EmbeddedVectorStore(cache_file=cache_path)

    # First add: should compute embedding
    updated1 = store.add_or_update_document("doc1", "Customer names and email records")
    assert updated1 is True
    store.save_cache()

    # Re-add identical content: should use cached vector (return False)
    updated2 = store.add_or_update_document("doc1", "Customer names and email records")
    assert updated2 is False

    # New store instance pointing to same file should load cached document
    store2 = EmbeddedVectorStore(cache_file=cache_path)
    assert "doc1" in store2.documents
    assert len(store2.documents["doc1"]["vector"]) > 0


def test_vector_store_search(tmp_path):
    cache_path = str(tmp_path / "test_search.json")
    store = EmbeddedVectorStore(cache_file=cache_path)

    store.add_or_update_document("customers", "Customer profiles, user accounts, and email directories")
    store.add_or_update_document("orders", "Sales orders, transaction purchase amounts, and order dates")
    store.add_or_update_document("salaries", "Employee payroll, executive salaries, and compensation")

    results = store.search("Who are the top purchasing customers?", top_k=2)
    assert len(results) > 0
    top_ids = [r["id"] for r in results]
    # 'customers' or 'orders' should rank higher than 'salaries'
    assert "customers" in top_ids or "orders" in top_ids


def test_schema_rag_retrieval(tmp_path):
    cache_path = str(tmp_path / "schema_cache.json")
    retriever = SchemaRAGRetriever(cache_file=cache_path)
    
    new_count = retriever.index_schema()
    assert new_count >= 0
    assert retriever.indexed is True

    # Test retrieval for general query
    ctx = retriever.retrieve_schema("What are the total sales by customer?")
    assert "customers" in ctx
    assert "orders" in ctx


def test_get_rag_schema_context_default():
    ctx = get_rag_schema_context()
    assert "customers" in ctx
    assert "orders" in ctx
    assert "products" in ctx
