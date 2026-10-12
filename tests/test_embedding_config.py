"""EMBEDDING_PROVIDER / EMBEDDING_STRICT / GEMINI_EMBEDDING_DIM behaviour."""

import pytest

from app.rag.vector_store import EmbeddingEngine, EmbeddingUnavailableError, OFFLINE_BACKEND, get_embedding_engine


class Failing:
    def embed_query(self, text):
        raise ConnectionError("down")

    def embed_documents(self, texts):
        raise ConnectionError("down")


def remote(monkeypatch, **env):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "auto")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    engine = EmbeddingEngine()
    engine.client = Failing()
    return engine


def test_tests_run_with_the_offline_provider():
    assert get_embedding_engine().backend_id == OFFLINE_BACKEND


def test_provider_can_be_forced_offline_even_with_a_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "offline")
    status = EmbeddingEngine().status()
    assert status["backend"] == OFFLINE_BACKEND and status["configured_remote"] is False


def test_backend_identity_includes_the_vector_dimension(monkeypatch):
    assert remote(monkeypatch).backend_id.endswith("@768")                      # default: 4x smaller than 3072
    assert remote(monkeypatch, GEMINI_EMBEDDING_DIM="1536").backend_id.endswith("@1536")


def test_changing_the_dimension_changes_the_engine(monkeypatch):
    monkeypatch.setenv("EMBEDDING_PROVIDER", "offline")
    first = get_embedding_engine()
    monkeypatch.setenv("GEMINI_EMBEDDING_DIM", "512")
    assert get_embedding_engine() is not first


def test_strict_mode_raises_instead_of_falling_back(monkeypatch):
    engine = remote(monkeypatch, EMBEDDING_STRICT="true")
    with pytest.raises(EmbeddingUnavailableError):
        engine.embed("hello")
    with pytest.raises(EmbeddingUnavailableError):
        engine.embed_many(["a", "b"])
    assert engine.fallback_calls == 0                       # nothing silently degraded


def test_default_mode_still_falls_back_visibly(monkeypatch):
    engine = remote(monkeypatch)
    vec, backend = engine.embed("hello")
    assert backend == OFFLINE_BACKEND and engine.status()["degraded"] is True


def test_embed_many_batches_and_caches(monkeypatch):
    calls = []

    class Batcher:
        def embed_documents(self, texts):
            calls.append(list(texts))
            return [[float(i), 1.0] for i, _ in enumerate(texts)]

    engine = remote(monkeypatch)
    engine.client = Batcher()
    out = engine.embed_many(["a", "b", "c"], batch_size=2)
    assert [b for _, b in out] == [engine.backend_id] * 3 and [len(c) for c in calls] == [2, 1]
    engine.embed_many(["a", "b", "c"])
    assert len(calls) == 2                                   # second time: served from the in-memory cache
