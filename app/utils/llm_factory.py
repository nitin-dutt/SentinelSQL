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

import os
import time
from functools import lru_cache
from typing import Any
from dotenv import load_dotenv

from app.utils.logger import get_logger

load_dotenv()
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
        retries = max(0, int(os.getenv("LLM_MAX_RETRIES", "2")))
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
        response = self._invoke_with_retries(prompt)
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
    gemini_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    explicit_provider = os.getenv("LLM_PROVIDER", "").strip().lower()
    timeout = float(os.getenv("LLM_TIMEOUT_SECONDS", "60"))

    # 1. Use Google Gemini if key is provided (unless user explicitly configured ollama)
    if gemini_key and explicit_provider != "ollama":
        model_name = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
        return _build_llm(role, "gemini", model_name, gemini_key, timeout)

    # 2. Fallback to Local Ollama
    from app.config import INTENT_MODEL, SQL_MODEL, VALIDATION_MODEL

    model_mapping = {
        "intent": INTENT_MODEL,
        "sql": SQL_MODEL,
        "validation": VALIDATION_MODEL,
        "default": INTENT_MODEL
    }
    return _build_llm(role, "ollama", model_mapping.get(role, INTENT_MODEL), "", timeout)
