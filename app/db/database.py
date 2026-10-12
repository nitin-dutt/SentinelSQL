"""
SentinelSQL database access.

Importing this module has no side effects (alignment_plan RUN-05): engines are created on first
use, nothing is printed, and a connection problem raises instead of killing the interpreter.
`engine`, `readonly_engine`, `DATABASE_URL` and `READONLY_DATABASE_URL` remain importable names
and resolve lazily.
"""

import os
import re
import threading
import time
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv
from sqlalchemy import create_engine, event, text

from app.utils.logger import get_logger
from app.utils.masking import apply_data_masking

load_dotenv()
logger = get_logger("sentinel.db")

_lock = threading.Lock()
_state: Dict[str, object] = {}


# --------------------------------------------------------------------------- configuration

def _truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def resolve_urls() -> Tuple[str, str, str]:
    """Returns (active_db, primary_url, readonly_url) from the current environment."""
    active_db = os.getenv("ACTIVE_DB", "").strip().lower()
    if active_db in ("postgres", "postgresql"):
        url = os.getenv("DATABASE_URL_POSTGRES") or os.getenv("DATABASE_URL") or "postgresql+psycopg2://user:password@localhost:5432/database_name"
    elif active_db == "mysql":
        url = os.getenv("DATABASE_URL_MYSQL") or os.getenv("DATABASE_URL") or "mysql+mysqlconnector://user:password@localhost:3307/database_name"
    elif active_db == "sqlite":
        url = os.getenv("DATABASE_URL_SQLITE") or os.getenv("DATABASE_URL", "sqlite:///sentinel.db")
    else:
        url = os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL_SQLITE", "sqlite:///sentinel.db")

    readonly = os.getenv("READONLY_DATABASE_URL")
    if not readonly:
        if url.startswith("sqlite"):
            readonly = f"sqlite:///file:{url.replace('sqlite:///', '')}?mode=ro&uri=true"
        else:
            readonly = url
    return active_db, url, readonly


def _safe(url: str) -> str:
    return url.split("@")[-1]  # never log credentials


def session_read_only_statements(dialect: str) -> List[str]:
    """
    SQL that makes every session on a non-SQLite read-only connection refuse writes
    (alignment_plan PII-04). SQLite is handled separately with `mode=ro`.
    """
    if dialect == "postgresql":
        return ["SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY"]
    if dialect == "mysql":
        return ["SET SESSION TRANSACTION READ ONLY"]
    return []


def enforce_read_only_sessions(eng) -> None:
    """Registers a connect hook so a pooled connection can never be used to write."""
    statements = session_read_only_statements(eng.dialect.name)
    if not statements:
        return

    @event.listens_for(eng, "connect")
    def _set_read_only(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        try:
            for stmt in statements:
                cursor.execute(stmt)
            # SET is transactional in PostgreSQL; commit so a later rollback cannot undo it.
            dbapi_connection.commit()
        finally:
            cursor.close()


def internal_sqlite_path(env_override: str) -> str:
    """
    File for SentinelSQL's own bookkeeping tables (golden queries, business glossary).
    Resolution: the named environment variable, else the primary database when it is a SQLite file
    (the bundled demo keeps everything in one place), else `sentinel_app.db` so that pointing the app
    at PostgreSQL/MySQL never creates SentinelSQL tables inside the customer's database.
    """
    explicit = os.getenv(env_override)
    if explicit:
        return explicit
    _, url, _ = resolve_urls()
    return url.replace("sqlite:///", "", 1) if url.startswith("sqlite") else "sentinel_app.db"


# --------------------------------------------------------------------------- lazy engines

def _build_engines() -> None:
    active_db, url, readonly_url = resolve_urls()
    logger.info("Database mode: %s | primary: %s | read-only: %s", active_db or "default (sqlite)", _safe(url), _safe(readonly_url))

    primary = create_engine(url)
    if not url.startswith("sqlite") and _truthy(os.getenv("DB_FALLBACK_TO_SQLITE")):
        # Opt-in only: silently switching a configured Postgres/MySQL deployment to the demo
        # SQLite file would hide an outage and point the app at the wrong data.
        try:
            with primary.connect():
                pass
        except Exception as e:
            logger.error("Cannot reach %s (%s); DB_FALLBACK_TO_SQLITE is set, using local sentinel.db", _safe(url), e)
            url, readonly_url = "sqlite:///sentinel.db", "sqlite:///file:sentinel.db?mode=ro&uri=true"
            primary = create_engine(url)

    readonly = create_engine(readonly_url)
    if not readonly_url.startswith("sqlite"):
        enforce_read_only_sessions(readonly)
        if readonly_url == url:
            logger.warning("READONLY_DATABASE_URL is not set: the read-only engine reuses the primary credentials and "
                           "relies on session-level READ ONLY. Create a SELECT-only database user for production.")
    _state.update(url=url, readonly_url=readonly_url, engine=primary, readonly_engine=readonly)


def _ensure_built() -> None:
    if "engine" not in _state:
        with _lock:
            if "engine" not in _state:
                _build_engines()


def get_engine():
    _ensure_built()
    return _state["engine"]


def get_readonly_engine():
    _ensure_built()
    return _state["readonly_engine"]


def reset_engines() -> None:
    """Disposes and forgets the engines so the next use re-reads the environment (used by tests)."""
    with _lock:
        for key in ("engine", "readonly_engine"):
            eng = _state.pop(key, None)
            if eng is not None:
                eng.dispose()
        _state.clear()


def check_connection() -> None:
    """Fails fast with a clear error if the primary database is unreachable (called at app startup)."""
    with get_engine().connect() as connection:
        connection.execute(text("SELECT 1"))
    logger.info("Database connection successful")


def __getattr__(name: str):
    """Lazy module attributes: `from app.db.database import engine` still works."""
    if name == "engine":
        return get_engine()
    if name == "readonly_engine":
        return get_readonly_engine()
    if name == "DATABASE_URL":
        _ensure_built()
        return _state["url"]
    if name == "READONLY_DATABASE_URL":
        _ensure_built()
        return _state["readonly_url"]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


_data_version_memo: Tuple[float, str] = (0.0, "")


def get_data_version() -> str:
    """
    A cheap token that changes when the database content changes, used to invalidate cached answers
    (alignment_plan CAC-03). For a SQLite file it is the modification time of the file (and its WAL);
    results are re-checked at most once per DATA_VERSION_CHECK_SECONDS (default 1). For PostgreSQL/MySQL
    there is no cheap equivalent, so it returns "untracked" and the cache TTL is the only bound.
    """
    global _data_version_memo
    checked_at, value = _data_version_memo
    if value and time.monotonic() - checked_at < float(os.getenv("DATA_VERSION_CHECK_SECONDS", "1")):
        return value
    _, url, _ = resolve_urls()
    if url.startswith("sqlite"):
        path = url.replace("sqlite:///", "", 1)
        parts = []
        for candidate in (path, path + "-wal"):
            try:
                parts.append(str(os.stat(candidate).st_mtime_ns))
            except OSError:
                parts.append("0")
        value = "sqlite:" + ":".join(parts)
    else:
        value = "untracked"
    _data_version_memo = (time.monotonic(), value)
    return value


# --------------------------------------------------------------------------- initialisation / seeding

SCHEMA_FILE = os.path.join(os.path.dirname(__file__), "schema.sql")

# Row counts the base demo seed produces; more than this means duplicates have accumulated.
EXPECTED_BASE_ROWS = {"customers": 4, "orders": 5, "products": 4, "salaries": 3}


def split_sql_statements(script: str) -> List[str]:
    """
    Splits a SQL script on top-level semicolons. Unlike `script.split(';')` it respects
    single-quoted strings (including '' escapes), -- line comments and /* */ block comments.
    """
    statements, buf = [], []
    i, n = 0, len(script)
    while i < n:
        ch = script[i]
        if ch == "'":
            j = i + 1
            while j < n:
                if script[j] == "'":
                    if j + 1 < n and script[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            buf.append(script[i:j + 1])
            i = j + 1
        elif script.startswith("--", i):
            j = script.find("\n", i)
            i = n if j == -1 else j  # comment dropped, newline kept
        elif script.startswith("/*", i):
            j = script.find("*/", i + 2)
            i = n if j == -1 else j + 2
        elif ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            i += 1
        else:
            buf.append(ch)
            i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def seed_demo_data_enabled() -> bool:
    """
    Demo seeding is automatic only for SQLite (the bundled demo). For PostgreSQL/MySQL it must be
    requested explicitly with SEED_DEMO_DATA=true, so pointing SentinelSQL at a real database
    never writes demo tables into it (alignment_plan DAT-05).
    """
    explicit = os.getenv("SEED_DEMO_DATA")
    if explicit is not None and explicit.strip():
        return _truthy(explicit)
    return get_engine().dialect.name == "sqlite"


def _translate_for_dialect(stmt: str, dialect: str) -> str:
    if dialect == "postgresql":
        stmt = re.sub(r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT", "SERIAL PRIMARY KEY", stmt, flags=re.I)
        if re.search(r"insert\s+or\s+ignore\s+into", stmt, re.I):
            stmt = re.sub(r"insert\s+or\s+ignore\s+into", "INSERT INTO", stmt, flags=re.I) + " ON CONFLICT DO NOTHING"
    elif dialect == "mysql":
        stmt = re.sub(r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT", "INT AUTO_INCREMENT PRIMARY KEY", stmt, flags=re.I)
        stmt = re.sub(r"insert\s+or\s+ignore\s+into", "INSERT IGNORE INTO", stmt, flags=re.I)
    return stmt


def demo_seed_health() -> Dict[str, int]:
    """Returns the base demo tables whose row count exceeds the seed (i.e. duplicates), {table: count}."""
    problems: Dict[str, int] = {}
    with get_engine().connect() as connection:
        for table, expected in EXPECTED_BASE_ROWS.items():
            try:
                count = connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
            except Exception:
                continue  # table absent: nothing to compare
            if count and count > expected:
                problems[table] = count
    return problems


def init_db() -> None:
    """
    Creates and seeds the demo database from schema.sql plus the extended demo dataset.
    Does nothing unless seeding is enabled (see `seed_demo_data_enabled`). Safe to call on every
    start: all inserts are idempotent.
    """
    if not seed_demo_data_enabled():
        logger.info("Demo data seeding is disabled for this database; leaving it untouched")
        return

    engine = get_engine()
    dialect = engine.dialect.name
    with open(SCHEMA_FILE, "r", encoding="utf-8") as f:
        statements = split_sql_statements(f.read())

    from app.db.demo_seed import seed_extended_demo

    with engine.connect() as connection:
        with connection.begin():
            for stmt in statements:
                try:
                    connection.execute(text(_translate_for_dialect(stmt, dialect)))
                except Exception as e:
                    if "already exists" not in str(e).lower():
                        logger.warning("schema.sql statement failed: %s", e)
            seed_extended_demo(connection)
    logger.info("Demo database initialized")

    duplicates = demo_seed_health()
    if duplicates:
        logger.warning("Base demo tables hold more rows than the seed (%s). Run scripts/reset_demo_db.py to repair.", duplicates)


# --------------------------------------------------------------------------- query execution

class QueryTimeoutError(Exception):
    """Raised when a query exceeds QUERY_TIMEOUT seconds and is cancelled."""


def _query_timeout_seconds() -> float:
    return float(os.getenv("QUERY_TIMEOUT", "10"))


def _max_result_rows() -> int:
    return int(os.getenv("MAX_RESULT_ROWS", "1000"))


def _arm_statement_timeout(connection, seconds: float):
    """
    Makes the database abandon the next statement after `seconds` (alignment_plan GRD-05).
    A LIMIT bounds the rows returned, not the work done: aggregations, LIKE scans and nested
    subqueries can still run for minutes under a LIMIT. Returns a disarm() callback.
    """
    dialect = connection.dialect.name
    if dialect == "sqlite":
        raw = connection.connection.dbapi_connection
        deadline = time.monotonic() + seconds
        # Called every 10k VM instructions; a non-zero return interrupts the statement.
        raw.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10000)
        return lambda: raw.set_progress_handler(None, 0)
    if dialect == "postgresql":
        connection.execute(text(f"SET LOCAL statement_timeout = {int(seconds * 1000)}"))
    elif dialect == "mysql":
        connection.execute(text(f"SET SESSION MAX_EXECUTION_TIME = {int(seconds * 1000)}"))
    return lambda: None


def execute_readonly_query(sql_query: str) -> Dict:
    """
    Executes a given SQL query and returns the results.
    A last-resort regex check allows only statements that start with SELECT or WITH (CTE);
    the real policy is enforced upstream by parse_and_validate_ast.

    Resource limits: the statement is cancelled after QUERY_TIMEOUT seconds, and at most
    MAX_RESULT_ROWS rows are fetched (`truncated: true` is set when more were available).
    """
    if not re.match(r"^\s*(select|with)\b", sql_query, re.IGNORECASE):
        raise ValueError("Only SELECT queries are allowed for this endpoint.")
    max_rows = _max_result_rows()
    with get_readonly_engine().connect() as connection:
        disarm = _arm_statement_timeout(connection, _query_timeout_seconds())
        try:
            result = connection.execute(text(sql_query))
            columns = list(result.keys())
            rows = result.fetchmany(max_rows + 1)
        except Exception as e:
            msg = str(e).lower()
            if "interrupted" in msg or "statement timeout" in msg or "max_execution_time" in msg or "maximum statement execution time" in msg:
                raise QueryTimeoutError(f"Query exceeded the {_query_timeout_seconds():g}s execution limit and was cancelled.") from e
            raise
        finally:
            disarm()

    truncated = len(rows) > max_rows
    raw_results = {
        "columns": columns,
        "rows": [dict(row._mapping) for row in rows[:max_rows]],
    }
    masked = apply_data_masking(raw_results, sql=sql_query)
    if truncated:
        masked["truncated"] = True
    return masked
