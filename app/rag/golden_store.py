"""
SentinelSQL Golden Query Memory & Few-Shot Store
------------------------------------------------
Stores human-verified, enterprise-approved SQL templates with explicit admin opt-in.
Prevents context contamination by only storing queries when explicitly authorized.
Matches incoming natural language prompts via semantic vector similarity to inject
high-confidence few-shot examples into the SQL Generation Agent.
"""

import os
import sqlite3
import threading
from typing import List, Dict, Any, Optional
from app.rag.vector_store import EmbeddedVectorStore
from app.utils.logger import get_logger

logger = get_logger("sentinel.golden")

_VECTOR_STORE: Optional[EmbeddedVectorStore] = None
_INITIALIZED: set = set()
_init_lock = threading.Lock()


def _db_path() -> str:
    from app.db.database import internal_sqlite_path
    return internal_sqlite_path("GOLDEN_DB_PATH")


def _vector_store() -> EmbeddedVectorStore:
    """Dedicated persistent vector store for golden query templates (created on first use)."""
    global _VECTOR_STORE
    cache_file = os.getenv("GOLDEN_VECTOR_CACHE", ".golden_vectors.json")
    if _VECTOR_STORE is None or _VECTOR_STORE.cache_file != cache_file:
        _VECTOR_STORE = EmbeddedVectorStore(cache_file=cache_file)
    return _VECTOR_STORE


def get_vector_store() -> EmbeddedVectorStore:
    _ensure_init()
    return _vector_store()


def _ensure_init() -> None:
    """Creates the table on first use and drops vectors of templates that no longer exist. Idempotent."""
    path = _db_path()
    if path in _INITIALIZED:
        return
    with _init_lock:
        if path in _INITIALIZED:
            return
        try:
            with sqlite3.connect(path) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS golden_queries (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        natural_language_query TEXT NOT NULL,
                        verified_sql TEXT NOT NULL,
                        created_by TEXT DEFAULT 'admin',
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                live = {str(r[0]) for r in conn.execute("SELECT id FROM golden_queries")}
            store = _vector_store()
            stale = [doc_id for doc_id in store.documents if doc_id not in live]
            for doc_id in stale:
                del store.documents[doc_id]
            if stale:
                store.save_cache()
            _INITIALIZED.add(path)
        except Exception as e:
            logger.warning("Failed to initialise golden_queries table: %s", e)


def _connect() -> sqlite3.Connection:
    _ensure_init()
    return sqlite3.connect(_db_path())


def save_golden_query(nl_query: str, sql: str, created_by: str = "admin") -> int:
    """
    Saves an admin-approved SQL query as an enterprise golden template.
    Persists to SQLite and indexes into the vector store.
    """
    clean_nl = nl_query.strip()
    clean_sql = sql.strip()

    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO golden_queries (natural_language_query, verified_sql, created_by)
            VALUES (?, ?, ?)
        """, (clean_nl, clean_sql, created_by))
        conn.commit()
        query_id = cursor.lastrowid

    # Index into vector store for semantic matching
    _vector_store().add_or_update_document(
        doc_id=str(query_id),
        content=clean_nl,
        metadata={
            "id": query_id,
            "nl_query": clean_nl,
            "verified_sql": clean_sql,
            "created_by": created_by
        }
    )
    _vector_store().save_cache()
    return query_id


def get_all_golden_queries() -> List[Dict[str, Any]]:
    """
    Returns all stored golden query templates.
    """
    try:
        with _connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM golden_queries ORDER BY id DESC")
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
    except Exception as e:
        logger.warning(f"[Golden Store Warning] Failed to read golden_queries: {e}")
        return []


def delete_golden_query(query_id: int) -> bool:
    """
    Deletes a golden query template from database and vector index.
    """
    try:
        with _connect() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM golden_queries WHERE id = ?", (query_id,))
            conn.commit()
            deleted = cursor.rowcount > 0

        # Remove from vector index if present
        str_id = str(query_id)
        if str_id in _vector_store().documents:
            del _vector_store().documents[str_id]
            _vector_store().save_cache()

        return deleted
    except Exception as e:
        logger.warning(f"[Golden Store Warning] Failed to delete golden query {query_id}: {e}")
        return False


def find_matching_golden_queries(query: str, top_k: int = 2, threshold: float = 0.5) -> List[Dict[str, Any]]:
    """
    Finds semantically similar golden queries to inject as few-shot in-context examples.
    Returns empty list if no matches exceed similarity threshold.
    """
    if not query or not query.strip():
        return []

    matches = _vector_store().search(query=query, top_k=top_k, threshold=threshold)
    results = []
    for m in matches:
        meta = m.get("metadata", {})
        if meta.get("verified_sql"):
            results.append({
                "id": meta.get("id"),
                "natural_language_query": meta.get("nl_query", m.get("content")),
                "verified_sql": meta.get("verified_sql"),
                "similarity": m.get("score", 0.0)
            })
    return results


def format_few_shot_examples(query: str, top_k: int = 2) -> str:
    """
    Formats matching golden queries into a clean few-shot block for SQL generator prompts.
    """
    matches = find_matching_golden_queries(query=query, top_k=top_k)
    if not matches:
        return ""

    lines = ["Verified Enterprise Query Examples:"]
    for match in matches:
        lines.append(f"- User Question: \"{match['natural_language_query']}\"")
        lines.append(f"  Verified SQL: {match['verified_sql']}")
    return "\n".join(lines).strip()
