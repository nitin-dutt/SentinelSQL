"""
LLM harness for benchmarks.

Every suite that needs a model goes through the *product's own* `UniversalLLM.invoke`; this module
wraps that one method so that, transparently for all product code paths (intent agent, SQL generator,
validator, ...):

  * responses are cached on disk (JSONL), keyed by (model, run tag, prompt). A re-run is free, an
    interrupted run resumes where it stopped, and a result file can always be traced to the exact
    model output that produced it;
  * calls are paced to the provider's quota (the free tier allows 15 requests/minute/model; we use 14);
  * a *daily* quota exhaustion stops the suite cleanly (progress is already on disk) instead of burning
    retries;
  * usage is accounted (live calls, cache hits, seconds spent waiting for the rate limit).

`tag` makes repeated runs independent samples: the same prompt under tag "r1" and "r2" is two separate
model calls (the Gemini 3.x models ignore temperature, so outputs vary run to run).
"""

import contextlib
import contextvars
import hashlib
import json
import os
import re
import threading
import time
from collections import deque
from typing import Dict, Optional

_TAG: contextvars.ContextVar[str] = contextvars.ContextVar("bench_llm_tag", default="default")


MAX_HARNESS_RETRIES = 4


class DailyQuotaExceeded(RuntimeError):
    """The provider's per-day quota is used up: stop and resume tomorrow (everything so far is cached)."""


@contextlib.contextmanager
def run_tag(tag: str):
    """Distinguishes independent repetitions of the same prompt."""
    token = _TAG.set(tag)
    try:
        yield
    finally:
        _TAG.reset(token)


class RateLimiter:
    """Sliding-window limiter: at most `rpm` acquisitions in any 60 s window. Thread-safe."""

    def __init__(self, rpm: float, window_s: float = 60.0):
        self.rpm = rpm
        self.window_s = window_s
        self._times: deque = deque()
        self._lock = threading.Lock()
        self.waited_s = 0.0

    def acquire(self) -> float:
        waited = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                while self._times and now - self._times[0] >= self.window_s:
                    self._times.popleft()
                if len(self._times) < self.rpm:
                    self._times.append(now)
                    self.waited_s += waited
                    return waited
                sleep_for = self.window_s - (now - self._times[0]) + 0.05
            time.sleep(sleep_for)
            waited += sleep_for


class LLMCache:
    """Append-only JSONL response cache."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._data: Dict[str, dict] = {}
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            rec = json.loads(line)
                            self._data[rec["key"]] = rec
                        except Exception:
                            continue  # a half-written last line from a killed run

    def get(self, key: str) -> Optional[dict]:
        return self._data.get(key)

    def put(self, key: str, record: dict) -> None:
        record = {"key": key, **record}
        with self._lock:
            self._data[key] = record
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def __len__(self) -> int:
        return len(self._data)


class Usage:
    def __init__(self):
        self.live_calls = 0
        self.cache_hits = 0
        self.failed_calls = 0
        self.harness_retries = 0
        self.live_latency_s = []
        self._lock = threading.Lock()

    def snapshot(self) -> dict:
        return {"live_calls": self.live_calls, "cache_hits": self.cache_hits, "failed_calls": self.failed_calls, "harness_retries": self.harness_retries}


@contextlib.contextmanager
def harness(cache_path: str, rpm: float = 14.0, use_cache: bool = True):
    """
    Installs the caching/pacing wrapper around `UniversalLLM.invoke` for the duration of the block and
    yields a `Usage` object. With `use_cache=False` calls are still paced and counted, never cached
    (used by the latency suite, which must measure real model time).
    """
    from app.utils.llm_factory import LLMUnavailableError, UniversalLLM

    cache = LLMCache(cache_path) if use_cache else None
    limiter = RateLimiter(rpm)
    usage = Usage()
    original = UniversalLLM.invoke
    previous_retries = os.environ.get("LLM_MAX_RETRIES")
    os.environ["LLM_MAX_RETRIES"] = "0"   # the harness owns retries (it knows the quota and honours retryDelay)

    def wrapped(self, prompt: str) -> str:
        key = hashlib.sha256(f"{self.model_name}\x1f{_TAG.get()}\x1f{prompt}".encode("utf-8")).hexdigest()
        if cache is not None:
            hit = cache.get(key)
            if hit is not None:
                with usage._lock:
                    usage.cache_hits += 1
                return hit["response"]
        response = None
        for attempt in range(MAX_HARNESS_RETRIES + 1):
            limiter.acquire()
            started = time.perf_counter()
            try:
                response = original(self, prompt)
                break
            except LLMUnavailableError as e:
                with usage._lock:
                    usage.failed_calls += 1
                cause = str(e.__cause__ or e)
                if "PerDay" in cause or "per day" in cause.lower():
                    raise DailyQuotaExceeded(cause[:300]) from e
                if attempt == MAX_HARNESS_RETRIES:
                    raise
                # A benchmark must not record a provider hiccup as a product failure: wait out the
                # provider's suggested delay and ask again.
                delay = re.search(r"retryDelay['\"]?[:=] ?['\"]?(\d+)", cause)
                wait = min(90, int(delay.group(1)) + 2) if delay else 20
                with usage._lock:
                    usage.harness_retries += 1
                time.sleep(wait)
        latency = time.perf_counter() - started
        with usage._lock:
            usage.live_calls += 1
            usage.live_latency_s.append(latency)
        if cache is not None:
            cache.put(key, {"response": response, "latency_s": round(latency, 3), "model": self.model_name,
                            "tag": _TAG.get(), "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "prompt_sha": hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]})
        return response

    UniversalLLM.invoke = wrapped
    try:
        yield usage
    finally:
        UniversalLLM.invoke = original
        if previous_retries is None:
            os.environ.pop("LLM_MAX_RETRIES", None)
        else:
            os.environ["LLM_MAX_RETRIES"] = previous_retries


# ====================================================================== embeddings

class EmbeddingCache:
    """Append-only JSONL cache of embedding vectors (float32, base64) keyed by (model@dim, text)."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._data: Dict[str, str] = {}
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                        self._data[rec["k"]] = rec["v"]
                    except Exception:
                        continue

    @staticmethod
    def key(model: str, text: str) -> str:
        return hashlib.sha256(f"{model}\x1f{text}".encode("utf-8")).hexdigest()

    @staticmethod
    def _encode(vec) -> str:
        import base64
        import numpy as np
        return base64.b64encode(np.asarray(vec, dtype=np.float32).tobytes()).decode("ascii")

    @staticmethod
    def _decode(blob: str):
        import base64
        import numpy as np
        return np.frombuffer(base64.b64decode(blob), dtype=np.float32).astype(float).tolist()

    def get(self, key: str):
        blob = self._data.get(key)
        return None if blob is None else self._decode(blob)

    def put_many(self, items) -> None:
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as f:
                for key, vec in items:
                    blob = self._encode(vec)
                    self._data[key] = blob
                    f.write(json.dumps({"k": key, "v": blob}) + "\n")

    def __len__(self) -> int:
        return len(self._data)


class EmbeddingUsage:
    def __init__(self):
        self.live_texts = 0
        self.cache_hits = 0
        self.live_requests = 0

    def snapshot(self) -> dict:
        return {"live_texts": self.live_texts, "cache_hits": self.cache_hits, "live_requests": self.live_requests}


@contextlib.contextmanager
def embedding_harness(cache_path: str, texts_per_minute: float = 80.0, use_cache: bool = True):
    """
    Caches and paces the provider's embedding calls (the free tier counts every text of a batch as a
    request and allows 1,000 per day), and makes the product's embedding engine *strict* so that a
    provider failure aborts the run rather than silently mixing in offline vectors.
    """
    from langchain_google_genai import GoogleGenerativeAIEmbeddings as Client

    cache = EmbeddingCache(cache_path) if use_cache else None
    limiter = RateLimiter(texts_per_minute)
    usage = EmbeddingUsage()
    orig_query, orig_docs = Client.embed_query, Client.embed_documents
    previous = {k: os.environ.get(k) for k in ("EMBEDDING_STRICT",)}
    os.environ["EMBEDDING_STRICT"] = "1"

    def model_id(client) -> str:
        return f"{getattr(client, 'model', '')}@{getattr(client, 'output_dimensionality', None)}"

    def fetch(client, texts):
        """Returns vectors for `texts`, calling the provider only for texts missing from the cache."""
        mid = model_id(client)
        out = [None] * len(texts)
        missing = []
        for i, t in enumerate(texts):
            hit = cache.get(EmbeddingCache.key(mid, t)) if cache is not None else None
            if hit is not None:
                out[i] = hit
                usage.cache_hits += 1
            else:
                missing.append(i)
        for start in range(0, len(missing), 50):
            chunk = missing[start:start + 50]
            for _ in chunk:
                limiter.acquire()
            for attempt in range(MAX_HARNESS_RETRIES + 1):
                try:
                    vectors = orig_docs(client, [texts[i] for i in chunk])
                    break
                except Exception as e:
                    msg = str(e)
                    if "PerDay" in msg:
                        raise DailyQuotaExceeded(msg[:300]) from e
                    if attempt == MAX_HARNESS_RETRIES or "429" not in msg:
                        raise
                    delay = re.search(r"retryDelay['\"]?[:=] ?['\"]?(\d+)", msg)
                    time.sleep(min(90, int(delay.group(1)) + 2) if delay else 20)
            usage.live_requests += 1
            usage.live_texts += len(chunk)
            if cache is not None:
                cache.put_many((EmbeddingCache.key(mid, texts[i]), v) for i, v in zip(chunk, vectors))
            for i, v in zip(chunk, vectors):
                out[i] = v
        return out

    Client.embed_documents = lambda self, texts, *a, **k: fetch(self, list(texts))
    Client.embed_query = lambda self, text, *a, **k: fetch(self, [text])[0]
    try:
        yield usage
    finally:
        Client.embed_query, Client.embed_documents = orig_query, orig_docs
        for k, v in previous.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
