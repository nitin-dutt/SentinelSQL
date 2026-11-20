"""
Every setting SentinelSQL reads from the environment, in one place.

    from app.settings import settings
    settings.limits.query_timeout        # seconds, read from QUERY_TIMEOUT now

Rules
  * This is the only module under app/ that reads the environment. Other modules ask `settings`.
  * Values are read when they are asked for, not when the app is imported, so a test, a benchmark or an operator can
    change a variable and the next request sees it. The few places that keep a value for the life of the process (a
    module-level constant, a singleton built once) say so where they do it; `reset_runtime_state` in app/runtime.py
    clears the singletons.
  * Each property keeps the variable's existing name, default and parsing. Where a bad value used to fall back to the
    default it still does; where it used to raise it still raises.
  * The .env file is loaded once, in app/__init__.py. `load_dotenv` never overrides a variable that already exists,
    even an empty one, and it re-adds one that is missing: to switch a variable off in a test, set it to "" instead of
    deleting it.

.env.example lists the same variables (tests/test_settings.py checks that the two agree).

Scopes
  A request made with the token of a connected workspace (app/workspaces.py) runs inside a *scope*: a set of values
  that replace the environment for that request only (its DATABASE_URL, and its own audit, review, trace and
  conversation files). Because every module asks `settings`, the whole pipeline then works on that workspace's database
  and files without knowing about workspaces. `settings.scope_key()` is "" outside a scope; modules that keep something
  built from the environment (engines, the schema, the retriever, the cache) keep one per scope key.
"""

import contextvars
import os
from contextlib import contextmanager
from typing import Dict, Iterator, Optional, Set

_YES = ("1", "true", "yes")

# {"key": str, "dir": str, "values": {ENV_NAME: value or None}} for the current request, or None
_scope: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar("sentinel_settings_scope", default=None)


def _get(name: str, default: str = "") -> str:
    scope = _scope.get()
    if scope is not None and name in scope["values"]:
        value = scope["values"][name]
        return default if value is None else value
    return os.getenv(name, default)


def _raw(name: str) -> Optional[str]:
    """The variable as it is, or None when it is not set (an empty value is returned as "")."""
    scope = _scope.get()
    if scope is not None and name in scope["values"]:
        return scope["values"][name]
    return os.getenv(name)


def _yes(name: str, default: str = "false") -> bool:
    return _get(name, default).strip().lower() in _YES


def _int_or(name: str, default: int, minimum: Optional[int] = None) -> int:
    """An integer; a value that is not a number falls back to the default."""
    try:
        value = int(_get(name, str(default)))
    except ValueError:
        return default
    return value if minimum is None else max(minimum, value)


class Database:
    @property
    def active_db(self) -> str:
        """ACTIVE_DB: sqlite | postgres | mysql, or "" to go by DATABASE_URL."""
        return _get("ACTIVE_DB").strip().lower()

    @property
    def url(self) -> Optional[str]:
        return _raw("DATABASE_URL")

    @property
    def sqlite_url(self) -> Optional[str]:
        return _raw("DATABASE_URL_SQLITE")

    @property
    def postgres_url(self) -> Optional[str]:
        return _raw("DATABASE_URL_POSTGRES")

    @property
    def mysql_url(self) -> Optional[str]:
        return _raw("DATABASE_URL_MYSQL")

    @property
    def readonly_url(self) -> Optional[str]:
        return _raw("READONLY_DATABASE_URL")

    @property
    def fallback_to_sqlite(self) -> bool:
        return _get("DB_FALLBACK_TO_SQLITE").strip().lower() in ("1", "true", "yes", "on")

    @property
    def seed_demo_data(self) -> Optional[bool]:
        """SEED_DEMO_DATA: True or False when it is set, None when the database type decides."""
        explicit = _raw("SEED_DEMO_DATA")
        if explicit is None or not explicit.strip():
            return None
        return explicit.strip().lower() in ("1", "true", "yes", "on")

    @property
    def data_version_check_seconds(self) -> float:
        return float(_get("DATA_VERSION_CHECK_SECONDS", "1"))


class Limits:
    @property
    def max_rows(self) -> int:
        """MAX_ROWS: the LIMIT the policy gate adds to a query that has none (1000, the same ceiling an explicit LIMIT may use).
        It was 100 until the Spider benchmark showed that the cap alone cost 5 points (hold-out 80% -> 85%, main 82.5% -> 85%);
        an answer cut by the gate is now marked `truncated` instead of being silently short."""
        return int(_get("MAX_ROWS", "1000"))

    @property
    def max_result_rows(self) -> int:
        """MAX_RESULT_ROWS: the largest LIMIT the gate permits, and the most rows an execution fetches."""
        return int(_get("MAX_RESULT_ROWS", "1000"))

    @property
    def query_timeout(self) -> float:
        """QUERY_TIMEOUT: seconds before a running statement is cancelled."""
        return float(_get("QUERY_TIMEOUT", "10"))


class Pipeline:
    @property
    def intent_mode(self) -> str:
        return _get("INTENT_MODE", "llm").strip().lower()

    @property
    def sql_candidates(self) -> int:
        """SQL_CANDIDATES as written (app/agents/candidate_voting.py clamps it to 1..5)."""
        return _int_or("SQL_CANDIDATES", 1)

    @property
    def sql_repair_attempts(self) -> int:
        return _int_or("SQL_REPAIR_ATTEMPTS", 1, minimum=0)

    @property
    def fewshot_top_k(self) -> int:
        return _int_or("FEWSHOT_TOP_K", 2, minimum=0)

    @property
    def validation_mode(self) -> str:
        return _get("VALIDATION_MODE", "ast").strip().lower()

    @property
    def policy_file(self) -> Optional[str]:
        """SENSITIVITY_CONFIG: the policy JSON; None means the file shipped with the app."""
        return _raw("SENSITIVITY_CONFIG")

    @property
    def table_descriptions_file(self) -> Optional[str]:
        return _raw("TABLE_DESCRIPTIONS_PATH")


class Models:
    @property
    def gemini_api_key(self) -> Optional[str]:
        return _raw("GEMINI_API_KEY") or _raw("GOOGLE_API_KEY") or None

    @property
    def provider(self) -> str:
        """LLM_PROVIDER: "" (Gemini when a key is set, else Ollama) or "ollama"."""
        return _get("LLM_PROVIDER").strip().lower()

    @property
    def gemini_model(self) -> str:
        return _get("GEMINI_MODEL", "gemini-3.5-flash-lite")

    @property
    def intent_model(self) -> str:
        return _get("INTENT_MODEL", "qwen2.5:14b-instruct")

    @property
    def sql_model(self) -> str:
        return _get("SQL_MODEL", "deepseek-coder:6.7b")

    @property
    def validation_model(self) -> str:
        return _get("VALIDATION_MODEL", "qwen2.5:14b-instruct")

    @property
    def timeout_seconds(self) -> float:
        return float(_get("LLM_TIMEOUT_SECONDS", "60"))

    @property
    def max_retries(self) -> int:
        return max(0, int(_get("LLM_MAX_RETRIES", "2")))

    @property
    def prices_json(self) -> str:
        return _get("LLM_PRICES_JSON").strip()

    @property
    def price_input_per_mtok(self) -> str:
        return _get("LLM_PRICE_INPUT_PER_MTOK").strip()

    @property
    def price_output_per_mtok(self) -> str:
        return _get("LLM_PRICE_OUTPUT_PER_MTOK").strip()


class Embeddings:
    @property
    def provider(self) -> str:
        """EMBEDDING_PROVIDER: auto | gemini | ollama | offline."""
        return _get("EMBEDDING_PROVIDER", "auto").strip().lower()

    @property
    def strict(self) -> bool:
        return _yes("EMBEDDING_STRICT", "")

    @property
    def retry_seconds(self) -> float:
        return float(_get("EMBEDDING_RETRY_SECONDS", "30"))

    @property
    def gemini_dimension(self) -> int:
        return int(_get("GEMINI_EMBEDDING_DIM", "768"))

    @property
    def gemini_dimension_text(self) -> str:
        """GEMINI_EMBEDDING_DIM as written (part of the signature that decides when the engine is rebuilt)."""
        return _get("GEMINI_EMBEDDING_DIM", "768")

    @property
    def cache_size(self) -> int:
        """EMBEDDING_CACHE_SIZE: how many vectors the engine keeps in memory before the least recently used go."""
        return _int_or("EMBEDDING_CACHE_SIZE", 2048, minimum=16)


class Cache:
    @property
    def enabled(self) -> bool:
        return _get("SEMANTIC_CACHE_ENABLED", "true").lower() in ("true", "1", "yes")

    @property
    def threshold(self) -> float:
        return float(_get("SEMANTIC_CACHE_THRESHOLD", "0.92"))

    @property
    def ttl_seconds(self) -> int:
        return int(_get("SEMANTIC_CACHE_TTL_SECONDS", "3600"))

    @property
    def max_entries(self) -> int:
        return int(_get("SEMANTIC_CACHE_MAX_ENTRIES", "500"))

    @property
    def save_interval_seconds(self) -> float:
        return float(_get("SEMANTIC_CACHE_SAVE_INTERVAL_SECONDS", "2"))


class Demo:
    @property
    def enabled(self) -> bool:
        return _yes("DEMO_MODE")

    @property
    def trust_proxy(self) -> bool:
        return _yes("DEMO_TRUST_PROXY")

    @property
    def public_users(self) -> Set[str]:
        return {u.strip() for u in _get("DEMO_PUBLIC_USERS").split(",") if u.strip()}

    @property
    def notice(self) -> str:
        return _get("DEMO_NOTICE", "Public demo on a small sample company database. Nothing you type is stored beyond the audit log.")

    @property
    def daily_query_cap(self) -> int:
        return _int_or("DEMO_DAILY_QUERY_CAP", 150, minimum=0)

    @property
    def rate_per_minute(self) -> int:
        return _int_or("DEMO_RATE_PER_MINUTE", 6, minimum=0)


class Launch:
    @property
    def env(self) -> str:
        """APP_ENV: "dev" lets anyone enter without a token (local development and testing only); anything else requires one."""
        return _get("APP_ENV").strip().lower()

    @property
    def dev(self) -> bool:
        return self.env == "dev"


class Workspaces:
    """Visitors connecting their own PostgreSQL/MySQL database from the launch page (app/workspaces.py)."""

    @property
    def enabled(self) -> bool:
        """WORKSPACES_ENABLED: unset means "on in dev and in demo mode, off otherwise"."""
        explicit = _get("WORKSPACES_ENABLED").strip().lower()
        if explicit:
            return explicit in _YES
        return _get("APP_ENV").strip().lower() == "dev" or _yes("DEMO_MODE")

    @property
    def question_quota(self) -> int:
        return _int_or("WORKSPACE_QUESTION_QUOTA", 25, minimum=1)

    @property
    def ttl_seconds(self) -> float:
        return _int_or("WORKSPACE_TTL_MINUTES", 120, minimum=1) * 60.0

    @property
    def max_active(self) -> int:
        return _int_or("WORKSPACE_MAX_ACTIVE", 20, minimum=1)

    @property
    def max_tables(self) -> int:
        return _int_or("WORKSPACE_MAX_TABLES", 300, minimum=1)

    @property
    def directory(self) -> str:
        return _get("WORKSPACE_DIR", "workspaces")

    @property
    def allow_private_hosts(self) -> bool:
        """WORKSPACE_ALLOW_PRIVATE_HOSTS: may a visitor's URL point at localhost or a private network? Unset: only in dev."""
        explicit = _get("WORKSPACE_ALLOW_PRIVATE_HOSTS").strip().lower()
        if explicit:
            return explicit in _YES
        return _get("APP_ENV").strip().lower() == "dev"


class Analyst:
    @property
    def clarify(self) -> bool:
        return _yes("ANALYST_CLARIFY", "true")

    @property
    def explore(self) -> bool:
        return _yes("ANALYST_EXPLORE", "false")

    @property
    def max_steps(self) -> int:
        """ANALYST_MAX_STEPS as written (app/agents/analyst.py clamps it to 1..8)."""
        return _int_or("ANALYST_MAX_STEPS", 4)

    @property
    def conversation_ttl_seconds(self) -> float:
        try:
            return max(0.0, float(_get("CONVERSATION_TTL_HOURS", "24"))) * 3600
        except ValueError:
            return 24 * 3600


class Tracing:
    @property
    def enabled(self) -> bool:
        return _get("TRACING_ENABLED", "true").strip().lower() not in ("0", "false", "no")

    @property
    def max_rows(self) -> int:
        return _int_or("TRACE_MAX_ROWS", 5000, minimum=100)


class Review:
    @property
    def pending_ttl_seconds(self) -> float:
        return float(_get("HITL_PENDING_TTL_HOURS", "72")) * 3600

    @property
    def finished_ttl_seconds(self) -> float:
        return float(_get("HITL_FINISHED_TTL_HOURS", "24")) * 3600

    @property
    def allow_self_approval(self) -> bool:
        return _yes("HITL_ALLOW_SELF_APPROVAL")


class Stores:
    """Where SentinelSQL keeps its own files. None means "decided elsewhere" (see internal_sqlite_path in app/db/database.py)."""

    @property
    def audit_db(self) -> str:
        return _get("AUDIT_DB_PATH", "audit.db")

    @property
    def review_db(self) -> str:
        return _get("HITL_DB_PATH", "hitl_runs.db")

    @property
    def trace_db(self) -> str:
        return _get("TRACE_DB_PATH", "traces.db")

    @property
    def conversation_db(self) -> str:
        return _get("CONVERSATION_DB_PATH", "conversations.db")

    @property
    def dictionary_db(self) -> Optional[str]:
        return _raw("DICTIONARY_DB_PATH") or None

    @property
    def golden_db(self) -> Optional[str]:
        return _raw("GOLDEN_DB_PATH") or None

    @property
    def dictionary_vectors(self) -> str:
        return _get("DICTIONARY_VECTOR_CACHE", ".dictionary_vectors.json")

    @property
    def golden_vectors(self) -> str:
        return _get("GOLDEN_VECTOR_CACHE", ".golden_vectors.json")

    @property
    def ui_dist_dir(self) -> Optional[str]:
        """UI_DIST_DIR: the built frontend to serve (set in the Docker image); None in development."""
        return _raw("UI_DIST_DIR") or None


class Access:
    @property
    def auth_tokens(self) -> str:
        """AUTH_TOKENS: token:user_id:role entries separated by commas. Never log this value."""
        return _get("AUTH_TOKENS")

    @property
    def mcp_client_id(self) -> str:
        return _get("MCP_CLIENT_ID", "mcp-stdio")


class Logging:
    @property
    def level(self) -> str:
        return _get("LOG_LEVEL", "INFO").upper()


class Settings:
    database = Database()
    limits = Limits()
    pipeline = Pipeline()
    models = Models()
    embeddings = Embeddings()
    cache = Cache()
    demo = Demo()
    launch = Launch()
    workspaces = Workspaces()
    analyst = Analyst()
    tracing = Tracing()
    review = Review()
    stores = Stores()
    access = Access()
    logging = Logging()

    # ------------------------------------------------------------ scopes (see the module docstring)

    @staticmethod
    def scope_key() -> str:
        """An empty string for the server's own environment, or the id of the workspace this request belongs to."""
        scope = _scope.get()
        return scope["key"] if scope else ""

    @staticmethod
    def scope_dir() -> Optional[str]:
        """The folder a scope keeps its files in; None outside a scope."""
        scope = _scope.get()
        return scope["dir"] if scope else None

    @staticmethod
    def enter_scope(key: str, directory: str, values: Dict[str, Optional[str]]):
        """Starts a scope and returns the token `exit_scope` needs. Prefer `scoped`."""
        return _scope.set({"key": key, "dir": directory, "values": dict(values)})

    @staticmethod
    def exit_scope(token) -> None:
        _scope.reset(token)

    @contextmanager
    def scoped(self, key: str, directory: str, values: Dict[str, Optional[str]]) -> Iterator[None]:
        token = self.enter_scope(key, directory, values)
        try:
            yield
        finally:
            self.exit_scope(token)


settings = Settings()
