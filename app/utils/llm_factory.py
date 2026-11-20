"""
SentinelSQL Universal LLM Factory
----------------------------------
Provides pluggable LLM support for Google Gemini (Free Tier) and local Ollama.
Allows recruiters and evaluators to run SentinelSQL immediately with a free
Gemini API key without requiring local GPU compute or Ollama installations.

Reliability (alignment_plan RUN-02): every call has a timeout (LLM_TIMEOUT_SECONDS, default 60)
and transient failures (rate limits, 5xx, timeouts, connection resets) are retried with
exponential backoff (LLM_MAX_RETRIES, default 2). A call that still fails raises
`LLMUnavailableError`, which the pipeline turns into a clean "error" response instead of a 500.
"""

import time
from functools import lru_cache
from typing import Any

from app.utils.logger import get_logger
from app.settings import settings

logger = get_logger("sentinel.llm")


class LLMUnavailableError(RuntimeError):
    """The language model could not produce a response (after retries)."""


_TRANSIENT_MARKERS = (
    "429", "500", "502", "503", "504", "timeout", "timed out", "unavailable", "rate limit", "rate_limit",
    "resource_exhausted", "resourceexhausted", "overloaded", "connection", "temporarily", "deadline",
)


def _is_transient(error: Exception) -> bool:
    text = f"{type(error).__name__} {error}".lower()
    return any(marker in text for marker in _TRANSIENT_MARKERS)


class UniversalLLM:
    """
    Unified LLM wrapper ensuring consistent .invoke(prompt) -> str output
    across both Google Gemini and Ollama providers.
    """
    def __init__(self, provider: str, client: Any, model_name: str = ""):
        self.provider = provider
        self.client = client
        self.model_name = model_name

    def _invoke_with_retries(self, prompt: str) -> Any:
        retries = settings.models.max_retries
        for attempt in range(retries + 1):
            try:
                return self.client.invoke(prompt)
            except Exception as e:
                if attempt < retries and _is_transient(e):
                    delay = min(2 ** attempt, 8)
                    logger.warning("LLM call failed (%s); retry %d/%d in %ds", type(e).__name__, attempt + 1, retries, delay)
                    time.sleep(delay)
                    continue
                logger.error("LLM call failed: %s: %s", type(e).__name__, str(e)[:200])
                raise LLMUnavailableError(f"The language model is unavailable right now ({type(e).__name__}). Please try again.") from e

    def invoke(self, prompt: str) -> str:
        from app.observability import tracing
        started = time.perf_counter()
        try:
            response = self._invoke_with_retries(prompt)
        except Exception:
            tracing.record_model_call(self.model_name, (time.perf_counter() - started) * 1000, prompt, "", ok=False)
            raise
        text_out = self._text_of(response)
        # one record per call in the current request's trace: latency, and the provider's token counts when it reports them
        tracing.record_model_call(self.model_name, (time.perf_counter() - started) * 1000, prompt, text_out,
                                  usage=tracing.usage_from_response(response))
        return text_out

    @staticmethod
    def _text_of(response: Any) -> str:
        text_out = ""
        # LangChain ChatGoogleGenerativeAI returns an AIMessage with a .content attribute
        if hasattr(response, "content"):
            content = response.content
            if isinstance(content, list):
                parts = []
                for item in content:
                    if isinstance(item, str):
                        parts.append(item)
                    elif isinstance(item, dict):
                        parts.append(str(item.get("text") or item.get("content") or ""))
                    elif hasattr(item, "text"):
                        parts.append(str(item.text))
                    else:
                        parts.append(str(item))
                text_out = "".join(parts).strip()
            else:
                text_out = str(content).strip()
        else:
            text_out = str(response).strip()

        # Handle edge-case where content is a serialized dict string
        if (text_out.startswith("{'type': 'text'") or text_out.startswith('{"type": "text"')) and "'text':" in text_out:
            import ast
            try:
                parsed = ast.literal_eval(text_out)
                if isinstance(parsed, dict) and "text" in parsed:
                    text_out = str(parsed["text"]).strip()
                elif isinstance(parsed, list) and len(parsed) > 0 and isinstance(parsed[0], dict) and "text" in parsed[0]:
                    text_out = str(parsed[0]["text"]).strip()
            except Exception:
                pass

        return text_out


@lru_cache(maxsize=16)
def _build_llm(role: str, provider: str, model_name: str, api_key: str, timeout: float) -> UniversalLLM:
    """Clients are built once per (role, provider, model) instead of once per request (RUN-03)."""
    if provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI
        client = ChatGoogleGenerativeAI(
            model=model_name,
            google_api_key=api_key,
            temperature=0.0,
            timeout=timeout,
            max_retries=0,  # retries are handled (and logged) by UniversalLLM
        )
        return UniversalLLM(provider="gemini", client=client, model_name=model_name)

    from langchain_ollama import OllamaLLM
    return UniversalLLM(provider="ollama", client=OllamaLLM(model=model_name), model_name=model_name)


def get_llm(role: str = "default") -> UniversalLLM:
    """
    Returns an initialized LLM client based on available environment credentials.
    Priority:
    1. If GEMINI_API_KEY or GOOGLE_API_KEY is present -> Google Gemini
    2. Otherwise -> Local Ollama instance
    """
    gemini_key = settings.models.gemini_api_key
    explicit_provider = settings.models.provider
    timeout = settings.models.timeout_seconds

    # 1. Use Google Gemini if key is provided (unless user explicitly configured ollama)
    if gemini_key and explicit_provider != "ollama":
        model_name = settings.models.gemini_model
        return _build_llm(role, "gemini", model_name, gemini_key, timeout)

    # 2. Fallback to Local Ollama
    models = settings.models
    model_mapping = {
        "intent": models.intent_model,
        "sql": models.sql_model,
        "validation": models.validation_model,
        "default": models.intent_model
    }
    return _build_llm(role, "ollama", model_mapping.get(role, models.intent_model), "", timeout)
