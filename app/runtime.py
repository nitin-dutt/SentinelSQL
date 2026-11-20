"""
One reset for everything the process remembers between requests.

Settings are read when they are asked for (app/settings.py), but several things are built once and kept: database
engines, the compiled pipeline, the schema, the embedding engine and its vectors, the semantic cache, the glossary and
golden-example indexes, model clients, and which internal SQLite files already have their tables. After pointing the
app at a different environment (another database, other store files, another provider) call `reset_runtime_state()`,
so nothing built for the old environment is used for the new one.

Tests and benchmarks call this instead of reaching into each module's private globals. A new process-wide cache must be
added here; tests/test_settings.py fails when a module-level singleton it knows about is not cleared.
"""

from typing import Optional


def reset_runtime_state(schema_vectors_file: Optional[str] = None) -> None:
    """
    Forgets every singleton and cache. Nothing on disk is deleted and no stored data is lost: the next use rebuilds
    each one from the current settings.

    `schema_vectors_file` makes the new schema retriever keep its vectors in that file instead of the default
    (benchmarks keep theirs in their work folder).
    """
    from app import demo, workspaces
    from app.agents import conversation_store, router
    from app.cache import semantic_cache
    from app.db import database
    from app.graph import workflow
    from app.hitl import web_store
    from app.observability import tracing
    from app.rag import data_dictionary, golden_store, schema_retriever, vector_store
    from app.utils import audit_logger, llm_factory, schemacontext

    # connected workspaces and launch-page sessions (their engines, schemas, retrievers and caches go with them)
    workspaces.reset()

    # the queried database and what was learned from it
    database.reset_engines()
    database._data_version_memo = (0.0, "")
    schemacontext.clear_schema_cache()

    # the pipeline and the model clients
    workflow.build_graph.cache_clear()
    llm_factory._build_llm.cache_clear()
    router._decision_cache.clear()

    # embeddings and everything indexed with them
    vector_store._ENGINE = None
    vector_store._ENGINE_SIGNATURE = None
    data_dictionary._INITIALIZED.clear()
    data_dictionary._VECTOR_STORE = None
    golden_store._INITIALIZED.clear()
    golden_store._VECTOR_STORE = None
    schema_retriever._GLOBAL_RETRIEVER = (schema_retriever.SchemaRAGRetriever(cache_file=schema_vectors_file)
                                          if schema_vectors_file else schema_retriever.SchemaRAGRetriever())
    semantic_cache._GLOBAL_SEMANTIC_CACHE = None

    # SentinelSQL's own SQLite files: create the tables again on next use (the file may be a different one now)
    for store in (audit_logger._STORE, web_store._STORE, tracing._STORE, conversation_store._STORE):
        store.forget()

    # the public demo's counters
    demo.budget.reset()
    demo.limiter.reset()
