"""
SentinelSQL Embedded Persistent Vector Store
--------------------------------------------
Lightweight, self-contained vector database implementation:
- Zero heavy C++ compiler dependencies (no hnswlib / native binary issues)
- Persistent JSON cache for instant startup without re-embedding
- Pluggable provider support (Google Gemini, Ollama, and an offline fallback)

Backend honesty (alignment_plan CAC-02): every vector is tagged with the backend that produced
it, and only vectors from the same backend are ever compared. When a configured remote provider
fails, the engine falls back to a crude offline hash embedding, but it says so: the engine reports
`degraded`, counts the fallbacks, backs off from the failing provider for a short cool-down, and
stores/compares the resulting vectors under the offline backend tag so they are re-embedded once
the provider recovers. Nothing mixes silently.
"""

import hashlib
import json
import math
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from app.utils.logger import get_logger

logger = get_logger("sentinel.vectors")

OFFLINE_BACKEND = "offline-hash-128"
GEMINI_EMBEDDING_MODEL = "models/gemini-embedding-001"
OLLAMA_EMBEDDING_MODEL = "nomic-embed-text"


class EmbeddingUnavailableError(RuntimeError):
    """A configured embedding provider failed and EMBEDDING_STRICT forbids falling back to offline vectors."""


def _gemini_dimension() -> int:
    """Gemini's default is 3,072 dimensions (41 KB per vector as JSON); 768 is 4x cheaper to store and scan."""
    return int(os.getenv("GEMINI_EMBEDDING_DIM", "768"))


def compute_content_hash(text: str) -> str:
    """Computes SHA-256 hash of text to detect schema changes."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def cosine_similarity(vec_a: List[float], vec_b: List[float]) -> float:
    """Computes cosine similarity between two float vectors."""
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return 0.0
    dot_product = sum(a * b for a, b in zip(vec_a, vec_b))
    norm_a = math.sqrt(sum(a * a for a in vec_a))
    norm_b = math.sqrt(sum(b * b for b in vec_b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot_product / (norm_a * norm_b)


def _offline_embedding(text: str, dim: int = 128) -> List[float]:
    """Deterministic lexical hash embedding. Works with no network or keys; semantically crude."""
    vec = [0.0] * dim
    words = text.lower().split()
    if not words:
        return vec
    for word in words:
        h = int(hashlib.md5(word.encode("utf-8")).hexdigest(), 16)
        vec[h % dim] += 1.0 if (h // dim) % 2 == 0 else -1.0
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm > 0 else vec


class EmbeddingEngine:
    """Embeds text via Google Gemini or Ollama, with a clearly-reported offline fallback."""

    def __init__(self):
        self.provider = "offline"
        self.backend_id = OFFLINE_BACKEND     # what a *healthy* call to this engine produces
        self.client = None
        self._embed_cache: Dict[Tuple[str, str], List[float]] = {}
        self.remote_calls = 0
        self.fallback_calls = 0
        self.degraded = False
        self.last_error: Optional[str] = None
        self.last_error_at: Optional[float] = None
        self.last_dimension: Optional[int] = None
        self._remote_blocked_until = 0.0
        self._init_client()

    def _init_client(self):
        gemini_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        explicit_provider = os.getenv("LLM_PROVIDER", "").strip().lower()
        forced = os.getenv("EMBEDDING_PROVIDER", "auto").strip().lower()   # auto | gemini | ollama | offline

        if forced == "offline":
            logger.info("EMBEDDING_PROVIDER=offline: using the offline hash embedding (%s)", OFFLINE_BACKEND)
            return
        if forced == "ollama":
            explicit_provider = "ollama"
        if forced == "gemini":
            explicit_provider = ""

        if gemini_key and explicit_provider != "ollama":
            try:
                from langchain_google_genai import GoogleGenerativeAIEmbeddings
                dim = _gemini_dimension()
                self.client = GoogleGenerativeAIEmbeddings(model=GEMINI_EMBEDDING_MODEL, google_api_key=gemini_key,
                                                           output_dimensionality=dim)
                self.provider = "gemini"
                self.backend_id = f"gemini:{GEMINI_EMBEDDING_MODEL}@{dim}"   # dimension is part of the identity
                return
            except Exception as e:
                self._record_failure(f"could not initialise Gemini embeddings: {type(e).__name__}", warn=True)
                self.provider = "gemini"            # configured, but unusable: that is degraded, not offline
                self.backend_id = f"gemini:{GEMINI_EMBEDDING_MODEL}@{_gemini_dimension()}"
                return

        if explicit_provider == "ollama":
            try:
                from langchain_ollama import OllamaEmbeddings
                self.client = OllamaEmbeddings(model=OLLAMA_EMBEDDING_MODEL)
                self.provider = "ollama"
                self.backend_id = f"ollama:{OLLAMA_EMBEDDING_MODEL}"
                return
            except Exception as e:
                self._record_failure(f"could not initialise Ollama embeddings: {type(e).__name__}", warn=True)
                self.provider = "ollama"
                self.backend_id = f"ollama:{OLLAMA_EMBEDDING_MODEL}"
                return

        logger.info("No embedding provider configured: using the offline hash embedding (%s)", OFFLINE_BACKEND)

    @property
    def strict(self) -> bool:
        """EMBEDDING_STRICT: never fall back to offline vectors; raise instead (benchmarks, fail-closed deployments)."""
        return os.getenv("EMBEDDING_STRICT", "").strip().lower() in ("1", "true", "yes")

    @property
    def configured_remote(self) -> bool:
        return self.provider in ("gemini", "ollama")

    def _record_failure(self, message: str, warn: bool = True) -> None:
        was_degraded = self.degraded
        self.degraded = True
        self.last_error = message
        self.last_error_at = time.time()
        if warn and not was_degraded:  # log the transition, not every request
            logger.warning("Embeddings degraded: %s. Falling back to %s until the provider recovers.", message, OFFLINE_BACKEND)

    def embed(self, text: str) -> Tuple[List[float], str]:
        """Returns (vector, backend that produced it). Callers must only compare vectors with equal backends."""
        if not text:
            return [0.0] * 128, OFFLINE_BACKEND

        if self.client and self.configured_remote and time.monotonic() >= self._remote_blocked_until:
            cached = self._embed_cache.get((self.backend_id, text))
            if cached is not None:
                return cached, self.backend_id
            try:
                vec = self.client.embed_query(text)
                self.remote_calls += 1
                self.last_dimension = len(vec)
                if self.degraded:
                    logger.info("Embeddings recovered: %s is responding again", self.backend_id)
                self.degraded = False
                self._embed_cache[(self.backend_id, text)] = vec
                return vec, self.backend_id
            except Exception as e:
                if self.strict:
                    raise EmbeddingUnavailableError(f"{type(e).__name__} from {self.backend_id}: {str(e)[:200]}") from e
                self._record_failure(f"{type(e).__name__} from {self.backend_id}")
                # Back off so a dead provider does not add a failing network call to every request
                self._remote_blocked_until = time.monotonic() + float(os.getenv("EMBEDDING_RETRY_SECONDS", "30"))

        if self.configured_remote and self.strict:
            raise EmbeddingUnavailableError(f"{self.backend_id} is unavailable and EMBEDDING_STRICT is set")
        if self.configured_remote:
            self.fallback_calls += 1
        key = (OFFLINE_BACKEND, text)
        if key not in self._embed_cache:
            self._embed_cache[key] = _offline_embedding(text)
        return self._embed_cache[key], OFFLINE_BACKEND

    def embed_text(self, text: str) -> List[float]:
        """Vector only (backend-agnostic callers). Prefer `embed` when vectors will be stored or compared."""
        return self.embed(text)[0]

    def embed_many(self, texts: List[str], batch_size: int = 100) -> List[Tuple[List[float], str]]:
        """
        Embeds many texts with one provider request per `batch_size` texts instead of one per text.
        Indexing a 24-table schema drops from 24 sequential round trips (~12 s) to one, and indexing
        hundreds of tables becomes practical. Same failure semantics as `embed`: a failed batch falls
        back (tagged) to the offline embedding for that batch only.
        """
        results: List[Optional[Tuple[List[float], str]]] = [None] * len(texts)
        pending = []
        for i, text in enumerate(texts):
            cached = self._embed_cache.get((self.backend_id, text)) if self.configured_remote else None
            if cached is not None:
                results[i] = (cached, self.backend_id)
            elif not text:
                results[i] = ([0.0] * 128, OFFLINE_BACKEND)
            else:
                pending.append(i)

        can_batch = (self.client is not None and self.configured_remote and hasattr(self.client, "embed_documents")
                     and time.monotonic() >= self._remote_blocked_until)
        for start in range(0, len(pending), batch_size):
            chunk = pending[start:start + batch_size]
            if can_batch:
                try:
                    vectors = self.client.embed_documents([texts[i] for i in chunk])
                    self.remote_calls += 1
                    if vectors:
                        self.last_dimension = len(vectors[0])
                    if self.degraded:
                        logger.info("Embeddings recovered: %s is responding again", self.backend_id)
                    self.degraded = False
                    for i, vec in zip(chunk, vectors):
                        self._embed_cache[(self.backend_id, texts[i])] = vec
                        results[i] = (vec, self.backend_id)
                    continue
                except Exception as e:
                    if self.strict:
                        raise EmbeddingUnavailableError(f"{type(e).__name__} from {self.backend_id}: {str(e)[:200]}") from e
                    self._record_failure(f"{type(e).__name__} from {self.backend_id}")
                    self._remote_blocked_until = time.monotonic() + float(os.getenv("EMBEDDING_RETRY_SECONDS", "30"))
                    can_batch = False
            for i in chunk:                      # single-text path handles fallback tagging and counters
                results[i] = self.embed(texts[i])
        return results  # type: ignore[return-value]

    def status(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "backend": self.backend_id,
            "configured_remote": self.configured_remote,
            "degraded": self.degraded,
            "remote_calls": self.remote_calls,
            "fallback_calls": self.fallback_calls,
            "dimension": self.last_dimension,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at,
        }


_ENGINE: Optional[EmbeddingEngine] = None
_ENGINE_SIGNATURE: Optional[tuple] = None


def get_embedding_engine() -> EmbeddingEngine:
    """
    One shared engine (one in-memory cache, one health status) for the whole process. It is rebuilt
    if the provider configuration changes, e.g. the API key is removed.
    """
    global _ENGINE, _ENGINE_SIGNATURE
    signature = (
        bool(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")),
        os.getenv("LLM_PROVIDER", "").strip().lower(),
        os.getenv("EMBEDDING_PROVIDER", "auto").strip().lower(),
        os.getenv("GEMINI_EMBEDDING_DIM", "768"),
    )
    if _ENGINE is None or signature != _ENGINE_SIGNATURE:
        _ENGINE = EmbeddingEngine()
        _ENGINE_SIGNATURE = signature
    return _ENGINE


class EmbeddedVectorStore:
    """
    Persistent embedded vector store storing document chunks, metadata, and vectors.
    Each document records the embedding backend that produced its vector.
    """
    def __init__(self, cache_file: str = ".schema_vectors.json", engine: Optional[EmbeddingEngine] = None):
        self.cache_file = cache_file
        self._engine = engine
        self.documents: Dict[str, Dict[str, Any]] = {}
        self.last_search_info: Dict[str, Any] = {}
        self.load_cache()

    @property
    def engine(self) -> EmbeddingEngine:
        return self._engine or get_embedding_engine()

    def load_cache(self) -> None:
        """Loads vectors and documents from disk cache if present."""
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    self.documents = json.load(f)
            except Exception as e:
                logger.warning("Could not read vector cache file %s: %s", self.cache_file, e)
                self.documents = {}

    def save_cache(self) -> None:
        """Saves current document index and vectors to disk cache (atomic replace)."""
        tmp = f"{self.cache_file}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.documents, f, indent=2)
            os.replace(tmp, self.cache_file)
        except Exception as e:
            logger.warning("Failed to persist vector cache %s: %s", self.cache_file, e)

    def add_or_update_document(self, doc_id: str, content: str, metadata: Optional[Dict[str, Any]] = None) -> bool:
        """
        Adds or updates a document. Computes a new embedding if the content changed OR if the stored
        vector came from a different backend than the engine now uses (so switching provider, or a
        recovered outage, re-embeds automatically). Returns True if a new embedding was computed.
        """
        new_hash = compute_content_hash(content)
        existing = self.documents.get(doc_id)
        engine = self.engine

        if (existing and existing.get("hash") == new_hash and existing.get("vector")
                and existing.get("backend") == engine.backend_id):
            existing["metadata"] = metadata or {}
            return False

        vector, backend = engine.embed(content)
        self.documents[doc_id] = {
            "id": doc_id,
            "hash": new_hash,
            "content": content,
            "metadata": metadata or {},
            "vector": vector,
            "backend": backend,
        }
        return True

    def add_or_update_documents(self, items: List[Tuple[str, str, Optional[Dict[str, Any]]]]) -> int:
        """
        Batch version of `add_or_update_document` for [(doc_id, content, metadata), ...]: documents that
        need (re-)embedding are embedded together in one provider request. Returns how many were embedded.
        """
        engine = self.engine
        todo = []
        for doc_id, content, metadata in items:
            new_hash = compute_content_hash(content)
            existing = self.documents.get(doc_id)
            if (existing and existing.get("hash") == new_hash and existing.get("vector")
                    and existing.get("backend") == engine.backend_id):
                existing["metadata"] = metadata or {}
            else:
                todo.append((doc_id, content, metadata, new_hash))
        if todo:
            for (doc_id, content, metadata, new_hash), (vector, backend) in zip(todo, engine.embed_many([t[1] for t in todo])):
                self.documents[doc_id] = {"id": doc_id, "hash": new_hash, "content": content,
                                          "metadata": metadata or {}, "vector": vector, "backend": backend}
        return len(todo)

    def search(self, query: str, top_k: int = 3, threshold: float = 0.0) -> List[Dict[str, Any]]:
        """
        Cosine-similarity search over documents embedded by the same backend as the query.
        Documents from another backend are skipped (never compared) and counted in `last_search_info`.
        """
        self.last_search_info = {"backend": None, "skipped_incompatible": 0, "compared": 0}
        if not self.documents:
            return []

        query_vec, backend = self.engine.embed(query)
        scored_results = []
        skipped = 0
        for doc_id, doc in self.documents.items():
            doc_vec = doc.get("vector")
            if not doc_vec:
                continue
            if doc.get("backend") != backend:
                skipped += 1
                continue
            sim = cosine_similarity(query_vec, doc_vec)
            if sim >= threshold:
                scored_results.append({
                    "id": doc_id,
                    "content": doc.get("content", ""),
                    "metadata": doc.get("metadata", {}),
                    "score": round(sim, 4),
                })

        self.last_search_info = {"backend": backend, "skipped_incompatible": skipped,
                                 "compared": len(self.documents) - skipped}
        if skipped:
            logger.warning("Vector search skipped %d/%d documents embedded by a different backend (query used %s)",
                           skipped, len(self.documents), backend)
        scored_results.sort(key=lambda x: x["score"], reverse=True)
        return scored_results[:top_k]

    def status(self) -> Dict[str, Any]:
        backends: Dict[str, int] = {}
        for doc in self.documents.values():
            backends[doc.get("backend", "unknown")] = backends.get(doc.get("backend", "unknown"), 0) + 1
        current = self.engine.backend_id
        return {
            "cache_file": self.cache_file,
            "documents": len(self.documents),
            "by_backend": backends,
            "stale_documents": sum(n for b, n in backends.items() if b != current),
        }
