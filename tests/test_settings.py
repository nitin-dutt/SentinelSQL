"""
phase4_redesign_plan.md sections 5, 6 and 7.

5: one settings module is the only reader of the environment, and one function resets process-wide state.
6: a cold question costs one remote embedding call, and the engine's vector cache is bounded.
7: the four internal SQLite stores share one connect helper.
"""

import os
import re
import sqlite3

import app
from app.runtime import reset_runtime_state
from app.settings import settings

APP_DIR = os.path.dirname(app.__file__)
ROOT = os.path.dirname(APP_DIR)


def _sources():
    for folder, _dirs, files in os.walk(APP_DIR):
        for name in files:
            if name.endswith(".py"):
                path = os.path.join(folder, name)
                yield os.path.relpath(path, APP_DIR).replace(os.sep, "/"), open(path, encoding="utf-8").read()


# ---------------------------------------------------------------- section 5: settings

def test_only_the_settings_module_reads_the_environment():
    readers = sorted(name for name, src in _sources() if re.search(r"os\.getenv|os\.environ", src))
    assert readers == ["settings.py"]
    assert sorted(name for name, src in _sources() if "load_dotenv(" in src) == ["__init__.py"]


def test_env_example_and_the_settings_module_list_the_same_variables():
    in_settings = set(re.findall(r'_(?:get|raw|yes|int_or)\("([A-Z][A-Z0-9_]*)"', open(os.path.join(APP_DIR, "settings.py"), encoding="utf-8").read()))
    in_example = set(re.findall(r"^#?\s?([A-Z][A-Z0-9_]*)=", open(os.path.join(ROOT, ".env.example"), encoding="utf-8").read(), flags=re.M))
    assert in_settings - in_example == set(), "read by the app but missing from .env.example"
    assert in_example - in_settings == set(), "documented in .env.example but read by nothing"


def test_settings_are_read_when_asked_for_and_keep_their_defaults(monkeypatch):
    for name in ("QUERY_TIMEOUT", "SQL_REPAIR_ATTEMPTS", "TRACE_MAX_ROWS", "ANALYST_CLARIFY", "TRACING_ENABLED", "EMBEDDING_CACHE_SIZE"):
        monkeypatch.delenv(name, raising=False)
    assert settings.limits.query_timeout == 10.0 and settings.pipeline.sql_repair_attempts == 1
    assert settings.tracing.max_rows == 5000 and settings.analyst.clarify is True and settings.tracing.enabled is True
    assert settings.embeddings.cache_size == 2048
    monkeypatch.setenv("QUERY_TIMEOUT", "3")
    monkeypatch.setenv("SQL_REPAIR_ATTEMPTS", "not a number")     # a bad value falls back, as it always did
    monkeypatch.setenv("TRACE_MAX_ROWS", "7")                      # below the floor
    monkeypatch.setenv("TRACING_ENABLED", "no")
    assert settings.limits.query_timeout == 3.0 and settings.pipeline.sql_repair_attempts == 1
    assert settings.tracing.max_rows == 100 and settings.tracing.enabled is False


def test_an_empty_database_url_is_not_the_same_as_a_missing_one(monkeypatch):
    from app.db.urls import resolve_urls
    monkeypatch.setenv("ACTIVE_DB", "sqlite")
    monkeypatch.setenv("DATABASE_URL_SQLITE", "")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert resolve_urls()[1] == "sqlite:///sentinel.db"
    monkeypatch.setenv("DATABASE_URL_SQLITE", "sqlite:///other.db")
    assert resolve_urls()[1] == "sqlite:///other.db"


def test_reset_runtime_state_clears_every_singleton():
    from app.agents import conversation_store, router
    from app.cache import semantic_cache
    from app.db import database
    from app.graph import workflow
    from app.hitl import web_store
    from app.observability import tracing
    from app.rag import schema_retriever, vector_store
    from app.utils import audit_logger, schemacontext

    database.get_engine()
    graph = workflow.build_graph()
    schemacontext.introspect_schema()
    engine = vector_store.get_embedding_engine()
    cache = semantic_cache.get_semantic_cache()
    retriever = schema_retriever._GLOBAL_RETRIEVER
    router._decision_cache["q"] = True
    for store in (audit_logger._STORE, web_store._STORE, tracing._STORE, conversation_store._STORE):
        store.connect().close()
        assert store.initialized

    reset_runtime_state()

    assert "engine" not in database._state and schemacontext._CACHED_SCHEMA is None
    assert workflow.build_graph() is not graph
    assert vector_store.get_embedding_engine() is not engine and semantic_cache.get_semantic_cache() is not cache
    assert schema_retriever._GLOBAL_RETRIEVER is not retriever and router._decision_cache == {}
    assert not any(s.initialized for s in (audit_logger._STORE, web_store._STORE, tracing._STORE, conversation_store._STORE))


def test_reset_runtime_state_knows_every_module_level_singleton():
    """A new process-wide cache must be added to app/runtime.py: this finds the usual shapes and checks each is mentioned there."""
    runtime = open(os.path.join(APP_DIR, "runtime.py"), encoding="utf-8").read()
    missing = []
    for name, src in _sources():
        module = name[:-3].split("/")[-1]
        for var in re.findall(r"^(_(?:GLOBAL_[A-Z_]+|ENGINE|VECTOR_STORE|STORE|CACHED_SCHEMA|decision_cache|state|data_version_memo))\b.*=", src, flags=re.M):
            covered = f"{module}.{var}" in runtime or (var in ("_state", "_CACHED_SCHEMA") and f"{module}." in runtime)
            if not covered:
                missing.append(f"{name}: {var}")
    assert missing == []


# ---------------------------------------------------------------- section 6: embedding calls

class _CountingClient:
    """Stands in for the remote embedding client: counts calls, returns a vector that depends on the text."""

    def __init__(self):
        self.queries = []

    def embed_query(self, text):
        self.queries.append(text)
        return [float(len(text)), 1.0, float(sum(map(ord, text)) % 97)]

    def embed_documents(self, texts):
        return [[float(len(t)), 1.0, 0.0] for t in texts]


def _remote_engine(monkeypatch, cache_size=None):
    from app.rag.vector_store import EmbeddingEngine
    monkeypatch.setenv("EMBEDDING_PROVIDER", "offline")
    if cache_size is not None:
        monkeypatch.setenv("EMBEDDING_CACHE_SIZE", str(cache_size))
    engine = EmbeddingEngine()
    engine.client, engine.provider, engine.backend_id = _CountingClient(), "gemini", "gemini:test@3"
    return engine


def test_a_cold_question_costs_one_remote_embedding_call(monkeypatch, tmp_path):
    from app.cache.semantic_cache import SemanticCache
    from app.rag.vector_store import EmbeddedVectorStore
    engine = _remote_engine(monkeypatch)
    tables = EmbeddedVectorStore(cache_file=str(tmp_path / "tables.json"), engine=engine)
    tables.add_or_update_documents([("customers", "Table Name: customers", None), ("orders", "Table Name: orders", None)])
    cache = SemanticCache(cache_file=str(tmp_path / "cache.json"), enabled=True, version_provider=lambda: "v", engine=engine)
    question = "Which Customers placed the most orders?"

    assert cache.get(question, role="employee") is None       # the cache embeds the question ...
    tables.search(question)                                   # ... and retrieval embeds the same text: served from memory
    cache.set(question, "SELECT 1 LIMIT 1;", {"columns": ["x"], "rows": []}, role="employee")
    assert engine.client.queries == [question]                # it was two calls: one normalized, one as typed

    assert cache.get("which customers placed the most orders", role="employee")["match_type"] == "exact"   # tier 1 is unchanged


def test_the_vector_cache_is_bounded_and_drops_the_least_recently_used(monkeypatch):
    engine = _remote_engine(monkeypatch, cache_size=16)
    for i in range(16):
        engine.embed(f"question {i}")
    engine.embed("question 0")                                 # used again: it must survive
    engine.embed("one more")                                   # 17th distinct text: "question 1" is the oldest now
    assert engine.status()["cached_vectors"] == 16 and engine.status()["cache_size"] == 16
    calls = len(engine.client.queries)
    engine.embed("question 0")
    assert len(engine.client.queries) == calls                 # still cached
    engine.embed("question 1")
    assert len(engine.client.queries) == calls + 1             # was evicted, so it is asked for again


def test_chat_hands_the_planners_schema_to_the_pipeline_when_the_question_did_not_change():
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from app.agents import analyst
    from app.main import app as fastapi_app
    from app.routers import chat
    from tests.helpers import EMPLOYEE, pipeline_writing, use_pipeline

    plan = analyst.Plan(standalone_question="List the customer names")
    with patch.object(chat, "_schema_for", return_value="customers(name)") as retrieve, \
         patch.object(analyst, "plan_turn", return_value=plan), use_pipeline(pipeline_writing("SELECT name FROM customers")) as pipeline:
        out = TestClient(fastapi_app).post("/chat", json={"message": "List the customer names"}, headers=EMPLOYEE).json()
    assert out["status"] == "completed" and retrieve.call_count == 1
    assert pipeline.states[-1]["schema_context"] == "customers(name)"


# ---------------------------------------------------------------- section 7: the stores share one helper

def test_the_four_stores_use_the_shared_helper_and_keep_their_own_files(tmp_path, monkeypatch):
    from app.agents import conversation_store
    from app.hitl import web_store
    from app.observability import tracing
    from app.utils import audit_logger
    from app.utils.sqlite_store import SqliteStore

    stores = {"AUDIT_DB_PATH": audit_logger._STORE, "HITL_DB_PATH": web_store._STORE, "TRACE_DB_PATH": tracing._STORE,
              "CONVERSATION_DB_PATH": conversation_store._STORE}
    tables = {"AUDIT_DB_PATH": "audit_logs", "HITL_DB_PATH": "hitl_runs", "TRACE_DB_PATH": "traces", "CONVERSATION_DB_PATH": "turns"}
    for env, store in stores.items():
        assert isinstance(store, SqliteStore)
        path = str(tmp_path / f"{env.lower()}.db")
        monkeypatch.setenv(env, path)                          # the location is asked for on every connect
        assert store.path() == path
        store.connect().close()
        found = {r[0] for r in sqlite3.connect(path).execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert tables[env] in found and len(found - {"sqlite_sequence"}) <= 2      # only its own tables
    for name, src in _sources():
        if name not in ("utils/sqlite_store.py", "rag/golden_store.py", "rag/data_dictionary.py"):
            assert "sqlite3.connect(" not in src, name


def test_delete_older_than_only_removes_what_is_past_the_limit():
    import time
    from app.utils.sqlite_store import delete_older_than
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (name TEXT, status TEXT, at REAL)")
    now = time.time()
    conn.executemany("INSERT INTO t VALUES (?, ?, ?)", [("old-done", "done", now - 100), ("old-pending", "pending", now - 100), ("new-done", "done", now)])
    delete_older_than(conn, "t", "at", 50, where="status != 'pending'")
    assert sorted(r[0] for r in conn.execute("SELECT name FROM t")) == ["new-done", "old-pending"]
