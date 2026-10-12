"""
SentinelSQL Enterprise Semantic Cache
-------------------------------------
Two-tier caching layer:
- Tier 1: Exact hash hit - deterministic match on (role, normalized_query)
- Tier 2: Semantic vector hit - cosine similarity (default >= 0.92), **only when the two questions
  agree on every constraint** (numbers, dates, ranking/comparison words, names) and every string
  literal in the cached SQL is supported by the new question (alignment_plan CAC-01).

Correctness rules
- Partition: results are cached per *role*. Today every user of a role is entitled to the same rows, so
  a role is the policy scope. If row-level filters per user are ever introduced the scope key must
  include the user (the `role` argument is the scope).
- Invalidation (CAC-03): an entry is only served while the *cache version* it was stored under is still
  current. The version combines the governance-policy fingerprint (sensitivity.json, limits) with a data
  version (SQLite: modification time of the database file; other engines: not tracked, so TTL is the
  only bound). Edit a policy or write to the database and older entries stop being served. Manual
  invalidation: clear() / delete() and the /admin/cache endpoints. TTL (SEMANTIC_CACHE_TTL_SECONDS)
  always applies.
- Backends (CAC-02): vectors carry the embedding backend that produced them and are only compared
  with vectors from the same backend.
- Concurrency (CAC-04): the lock only guards in-memory structures. Embedding (network) and disk writes
  happen outside it, and similarity is computed with a numpy matrix instead of a Python loop.

Other features: configurable TTL and LRU capacity, thread safety, persistent JSON file with throttled
atomic writes, hit/miss telemetry, admin invalidation.
"""

import atexit
import hashlib
import json
import os
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

try:  # numpy is optional: without it the scan falls back to pure Python
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

import sqlglot
from sqlglot import exp

from app.cache.constraints import extract_constraints
from app.rag.vector_store import EmbeddingEngine, cosine_similarity, get_embedding_engine
from app.utils.logger import get_logger

logger = get_logger("sentinel.cache")


def default_cache_version() -> str:
    """Policy fingerprint + data version: changes whenever a cached answer could have become wrong."""
    from app.db.database import get_data_version
    from app.utils.sql_parser import get_policy_fingerprint
    return f"{get_policy_fingerprint()}|{get_data_version()}"


def _string_literals(sql: str) -> List[str]:
    """Lower-cased string literals in the SQL (LIMIT/OFFSET excluded): the values a question must support."""
    try:
        tree = sqlglot.parse_one(sql)
    except Exception:
        return []
    values = []
    for lit in tree.find_all(exp.Literal):
        if lit.is_string and lit.this and not lit.find_ancestor(exp.Limit, exp.Offset):
            values.append(str(lit.this).strip().lower())
    return [v for v in values if v]


class SemanticCache:
    def __init__(
        self,
        cache_file: str = ".semantic_cache.json",
        enabled: Optional[bool] = None,
        threshold: Optional[float] = None,
        ttl_seconds: Optional[int] = None,
        max_entries: Optional[int] = None,
        version_provider: Optional[Callable[[], str]] = None,
        engine: Optional[EmbeddingEngine] = None,
    ):
        self.cache_file = cache_file
        self.lock = threading.Lock()          # guards in-memory structures only
        self._io_lock = threading.Lock()      # serialises file writes
        self._engine = engine
        self._version_provider = version_provider or default_cache_version

        env_enabled = os.getenv("SEMANTIC_CACHE_ENABLED", "true").lower() in ("true", "1", "yes")
        self.enabled = enabled if enabled is not None else env_enabled
        self.threshold = threshold if threshold is not None else float(os.getenv("SEMANTIC_CACHE_THRESHOLD", "0.92"))
        self.default_ttl = ttl_seconds if ttl_seconds is not None else int(os.getenv("SEMANTIC_CACHE_TTL_SECONDS", "3600"))
        self.max_entries = max_entries if max_entries is not None else int(os.getenv("SEMANTIC_CACHE_MAX_ENTRIES", "500"))
        self.save_interval = float(os.getenv("SEMANTIC_CACHE_SAVE_INTERVAL_SECONDS", "2"))

        self.entries: Dict[str, Dict[str, Any]] = {}
        self.exact_map: Dict[str, str] = {}   # f"{role}:{normalized_query}" -> entry_id
        self.stats = {"hits": 0, "exact_hits": 0, "semantic_hits": 0, "misses": 0,
                      "semantic_rejected": 0, "stale_evictions": 0}

        self._version = 0                      # bumped on every structural change; invalidates matrices
        self._matrices: Dict[Tuple[str, str], Tuple[int, List[str], Any]] = {}
        self._dirty = False
        self._last_save = 0.0

        self._load_cache()

    @property
    def engine(self) -> EmbeddingEngine:
        return self._engine or get_embedding_engine()

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def normalize_query(query: str) -> str:
        """Normalizes query text by lowercasing, stripping extra whitespace and trailing punctuation."""
        if not query:
            return ""
        clean = query.strip().lower()
        clean = re.sub(r"[?!;.,]+$", "", clean)
        clean = re.sub(r"\s+", " ", clean)
        return clean.strip()

    def _hash_key(self, role: str, normalized_query: str) -> str:
        return f"{role}:{normalized_query}"

    def _is_expired(self, entry: Dict[str, Any], now: float) -> bool:
        return now - entry["created_at"] >= entry["ttl_seconds"]

    def _current_version(self) -> str:
        try:
            return self._version_provider()
        except Exception as e:  # never let a version lookup break serving; fail closed = treat as changed
            logger.warning("Cache version lookup failed (%s); entries will not be served", e)
            return f"unavailable-{time.time()}"

    # ------------------------------------------------------------------ persistence

    def _load_cache(self) -> None:
        if not os.path.exists(self.cache_file):
            return
        try:
            with open(self.cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            now = time.time()
            for entry_id, entry in data.get("entries", {}).items():
                if now - entry.get("created_at", 0) < entry.get("ttl_seconds", self.default_ttl):
                    self.entries[entry_id] = entry
                    self.exact_map[self._hash_key(entry.get("role", "guest"), self.normalize_query(entry.get("query", "")))] = entry_id
            if "stats" in data:
                self.stats.update(data["stats"])
        except Exception as e:
            logger.warning("Failed to load cache file %s: %s", self.cache_file, e)
            self.entries, self.exact_map = {}, {}

    def _snapshot_locked(self) -> Dict[str, Any]:
        """A consistent copy to serialise outside the lock (call with the lock held)."""
        return {"entries": {k: dict(v) for k, v in self.entries.items()}, "stats": dict(self.stats)}

    def _write_snapshot(self, snapshot: Dict[str, Any]) -> None:
        """Atomic write (temp file + replace) so a crash never leaves a half-written cache."""
        with self._io_lock:
            tmp = f"{self.cache_file}.tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(snapshot, f)
                os.replace(tmp, self.cache_file)
            except Exception as e:
                logger.warning("Failed to save cache file %s: %s", self.cache_file, e)

    def flush(self) -> None:
        """Writes pending changes to disk now (also runs at interpreter exit)."""
        with self.lock:
            if not self._dirty:
                return
            snapshot, self._dirty, self._last_save = self._snapshot_locked(), False, time.time()
        self._write_snapshot(snapshot)

    # ------------------------------------------------------------------ lookup

    def _evict_entry_locked(self, entry_id: str) -> None:
        entry = self.entries.pop(entry_id, None)
        if entry:
            key = self._hash_key(entry.get("role", "guest"), entry.get("normalized_query", ""))
            if self.exact_map.get(key) == entry_id:
                del self.exact_map[key]
            self._version += 1

    def _hit_payload(self, entry: Dict[str, Any], match_type: str, similarity: float, now: float) -> Dict[str, Any]:
        entry["hits_count"] = entry.get("hits_count", 0) + 1
        entry["last_accessed"] = now
        self.stats["hits"] += 1
        self.stats["exact_hits" if match_type == "exact" else "semantic_hits"] += 1
        return {
            "sql": entry["sql"],
            "results": entry["results"],
            "confidence_score": entry.get("confidence_score", 100),
            "match_type": match_type,
            "similarity": similarity,
            "cached_at": entry["created_at"],
            "original_query": entry["query"],
        }

    def _usable_locked(self, entry_id: str, entry: Dict[str, Any], now: float, current_version: str) -> bool:
        """Evicts and returns False for expired entries or entries from an older policy/data version."""
        if self._is_expired(entry, now):
            self._evict_entry_locked(entry_id)
            return False
        if entry.get("cache_version") != current_version:
            self.stats["stale_evictions"] += 1
            self._evict_entry_locked(entry_id)
            return False
        return True

    def _matrix_locked(self, role: str, backend: str):
        """(ids, row-normalised numpy matrix) for entries of this role and embedding backend."""
        cached = self._matrices.get((role, backend))
        if cached and cached[0] == self._version:
            return cached[1], cached[2]
        ids = [eid for eid, e in self.entries.items()
               if e.get("role") == role and e.get("embedding_backend") == backend and e.get("vector")]
        matrix = None
        if ids and np is not None:
            matrix = np.array([self.entries[i]["vector"] for i in ids], dtype=np.float32)
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            matrix = matrix / np.where(norms == 0, 1, norms)
        self._matrices[(role, backend)] = (self._version, ids, matrix)
        return ids, matrix

    def get(self, query: str, role: str = "guest") -> Optional[Dict[str, Any]]:
        """
        Returns a cached response for (query, role) or None. Tier 1 is an exact normalized match; tier 2
        is a similarity match that must also satisfy the constraint and literal checks.
        """
        if not self.enabled or not query or not query.strip():
            return None

        clean_query = self.normalize_query(query)
        exact_key = self._hash_key(role, clean_query)
        now = time.time()
        current_version = self._current_version()

        # ---- Tier 1 (in-memory only, under the lock)
        with self.lock:
            entry_id = self.exact_map.get(exact_key)
            entry = self.entries.get(entry_id) if entry_id else None
            if entry and self._usable_locked(entry_id, entry, now, current_version):
                return self._hit_payload(entry, "exact", 1.0, now)

        # ---- Embed OUTSIDE the lock: this can be a network call (CAC-04)
        query_vec, backend = self.engine.embed(clean_query)
        question_constraints = extract_constraints(query)
        question_text = clean_query

        with self.lock:
            ids, matrix = self._matrix_locked(role, backend)
            scored: List[Tuple[float, str]] = []
            if ids:
                if matrix is not None:
                    q = np.asarray(query_vec, dtype=np.float32)
                    norm = float(np.linalg.norm(q))
                    sims = matrix @ (q / norm) if norm > 0 else np.zeros(len(ids), dtype=np.float32)
                    scored = [(float(s), i) for s, i in zip(sims, ids)]
                else:
                    scored = [(cosine_similarity(query_vec, self.entries[i]["vector"]), i) for i in ids]
            scored.sort(reverse=True)

            for sim, entry_id in scored:
                if sim < self.threshold:
                    break
                entry = self.entries.get(entry_id)
                if entry is None or not self._usable_locked(entry_id, entry, now, current_version):
                    continue
                if frozenset(entry.get("constraints") or extract_constraints(entry["query"])) != question_constraints:
                    self.stats["semantic_rejected"] += 1
                    logger.info("Semantic hit rejected (sim %.3f): constraints differ from cached question %r", sim, entry["query"])
                    continue
                missing = [v for v in entry.get("sql_literals", []) if v not in question_text]
                if missing:
                    self.stats["semantic_rejected"] += 1
                    logger.info("Semantic hit rejected (sim %.3f): cached SQL filters on %s which the question does not mention", sim, missing)
                    continue
                return self._hit_payload(entry, "semantic", round(sim, 4), now)

            self.stats["misses"] += 1
            return None

    # ------------------------------------------------------------------ store

    def set(
        self,
        query: str,
        sql: str,
        results: Dict[str, Any],
        role: str = "guest",
        confidence_score: int = 100,
        ttl_seconds: Optional[int] = None,
    ) -> Optional[str]:
        """Stores a verified query result. Returns the generated entry ID."""
        if not self.enabled or not query or not sql or results is None:
            return None

        clean_query = self.normalize_query(query)
        ttl = ttl_seconds if ttl_seconds is not None else self.default_ttl
        vector, backend = self.engine.embed(clean_query)      # outside the lock
        version = self._current_version()
        constraints = sorted(extract_constraints(query))
        literals = _string_literals(sql)
        now = time.time()
        entry_id = hashlib.sha256(f"{role}:{clean_query}:{now}".encode("utf-8")).hexdigest()[:16]

        snapshot = None
        with self.lock:
            if len(self.entries) >= self.max_entries:
                self._evict_stale_or_lru_locked(now)
            self.entries[entry_id] = {
                "id": entry_id, "query": query, "normalized_query": clean_query, "role": role,
                "sql": sql, "results": results, "confidence_score": confidence_score,
                "created_at": now, "last_accessed": now, "ttl_seconds": ttl, "hits_count": 0,
                "vector": vector, "embedding_backend": backend,
                "cache_version": version, "constraints": constraints, "sql_literals": literals,
            }
            self.exact_map[self._hash_key(role, clean_query)] = entry_id
            self._version += 1
            self._dirty = True
            if now - self._last_save >= self.save_interval:
                snapshot, self._dirty, self._last_save = self._snapshot_locked(), False, now
        if snapshot is not None:
            self._write_snapshot(snapshot)   # outside the lock
        return entry_id

    def _evict_stale_or_lru_locked(self, now: float) -> None:
        for eid in [e for e, v in self.entries.items() if self._is_expired(v, now)]:
            self._evict_entry_locked(eid)
        if len(self.entries) >= self.max_entries and self.entries:
            oldest = min(self.entries, key=lambda eid: self.entries[eid].get("last_accessed", 0))
            self._evict_entry_locked(oldest)

    # ------------------------------------------------------------------ administration

    def clear(self) -> int:
        """Clears every entry and resets statistics (manual invalidation hook)."""
        with self.lock:
            count = len(self.entries)
            self.entries.clear()
            self.exact_map.clear()
            self._matrices.clear()
            self._version += 1
            self.stats = {"hits": 0, "exact_hits": 0, "semantic_hits": 0, "misses": 0,
                          "semantic_rejected": 0, "stale_evictions": 0}
            snapshot, self._dirty, self._last_save = self._snapshot_locked(), False, time.time()
        self._write_snapshot(snapshot)
        return count

    def delete(self, entry_id: str) -> bool:
        with self.lock:
            if entry_id not in self.entries:
                return False
            self._evict_entry_locked(entry_id)
            snapshot, self._dirty, self._last_save = self._snapshot_locked(), False, time.time()
        self._write_snapshot(snapshot)
        return True

    def get_stats(self) -> Dict[str, Any]:
        """Telemetry: hit ratios, counts, configuration and embedding health."""
        engine_status = self.engine.status()
        with self.lock:
            total_lookups = self.stats["hits"] + self.stats["misses"]
            hit_ratio = round((self.stats["hits"] / total_lookups) * 100, 1) if total_lookups > 0 else 0.0
            incompatible = sum(1 for e in self.entries.values() if e.get("embedding_backend") != engine_status["backend"])
            return {
                "enabled": self.enabled,
                "total_entries": len(self.entries),
                "hits": self.stats["hits"],
                "exact_hits": self.stats["exact_hits"],
                "semantic_hits": self.stats["semantic_hits"],
                "misses": self.stats["misses"],
                "semantic_rejected": self.stats["semantic_rejected"],
                "stale_evictions": self.stats["stale_evictions"],
                "hit_ratio_percent": hit_ratio,
                "threshold": self.threshold,
                "default_ttl_seconds": self.default_ttl,
                "max_entries": self.max_entries,
                "embedding": engine_status,
                "entries_with_other_embedding_backend": incompatible,
            }


# Singleton instance
_GLOBAL_SEMANTIC_CACHE: Optional[SemanticCache] = None


def get_semantic_cache() -> SemanticCache:
    """Returns the shared global instance of the SemanticCache."""
    global _GLOBAL_SEMANTIC_CACHE
    if _GLOBAL_SEMANTIC_CACHE is None:
        _GLOBAL_SEMANTIC_CACHE = SemanticCache()
        atexit.register(_GLOBAL_SEMANTIC_CACHE.flush)
    return _GLOBAL_SEMANTIC_CACHE
