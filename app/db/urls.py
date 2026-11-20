"""
Database URL resolution and SQL dialect detection.

A user points SentinelSQL at a database with ONE setting, `DATABASE_URL`, for example

    postgresql://user:password@host:5432/dbname
    mysql://user:password@host:3306/dbname
    sqlite:///sentinel.db

The SQL dialect (which sqlglot must use to read the SQL, and which the model must be told to write) is derived
from that URL, so nothing else has to be configured. Common URL spellings are accepted and mapped to the drivers
that ship in requirements.txt (`postgres://` and `mysql://` do not name a driver the way SQLAlchemy needs).

This module has no dependency on the rest of the app, so the parser, masking and cache can import it without
creating import cycles.
"""

from functools import lru_cache
from typing import Optional, Tuple

from sqlalchemy.engine import make_url

from app.settings import settings

# URL scheme -> the scheme SQLAlchemy needs for the drivers listed in requirements.txt
_SCHEME_ALIASES = {
    "postgres": "postgresql+psycopg2",
    "postgresql": "postgresql+psycopg2",
    "mysql": "mysql+mysqlconnector",
    "mariadb": "mysql+mysqlconnector",
}

# SQLAlchemy backend name -> (sqlglot dialect, name shown to the model)
_DIALECTS = {
    "postgresql": ("postgres", "PostgreSQL"),
    "mysql": ("mysql", "MySQL"),
    "mariadb": ("mysql", "MySQL"),
    "sqlite": ("sqlite", "SQLite"),
}


def normalize_database_url(url: str) -> str:
    """Maps `postgres://`, `postgresql://`, `mysql://` and `mariadb://` to a URL with an installed driver; other URLs pass through."""
    url = (url or "").strip()
    scheme, sep, rest = url.partition("://")
    if sep and scheme.lower() in _SCHEME_ALIASES:
        return f"{_SCHEME_ALIASES[scheme.lower()]}://{rest}"
    return url


def resolve_urls() -> Tuple[str, str, str]:
    """Returns (active_db, primary_url, readonly_url) from the current environment."""
    db = settings.database
    active_db = db.active_db
    default_sqlite = "sqlite:///sentinel.db"
    if active_db in ("postgres", "postgresql"):
        url = db.postgres_url or db.url or "postgresql+psycopg2://user:password@localhost:5432/database_name"
    elif active_db == "mysql":
        url = db.mysql_url or db.url or "mysql+mysqlconnector://user:password@localhost:3307/database_name"
    elif active_db == "sqlite":
        url = db.sqlite_url or (default_sqlite if db.url is None else db.url)
    else:
        url = db.url or (default_sqlite if db.sqlite_url is None else db.sqlite_url)
    url = normalize_database_url(url)

    readonly = db.readonly_url
    if readonly:
        readonly = normalize_database_url(readonly)
    elif url.startswith("sqlite"):
        readonly = f"sqlite:///file:{url.replace('sqlite:///', '')}?mode=ro&uri=true"
    else:
        readonly = url
    return active_db, url, readonly


@lru_cache(maxsize=32)
def _dialect_for_url(url: str) -> Tuple[Optional[str], str]:
    try:
        backend = make_url(url).get_backend_name()
    except Exception:
        return None, "SQL"
    return _DIALECTS.get(backend, (None, "SQL"))


def sql_dialect() -> Optional[str]:
    """The sqlglot dialect of the configured database ("sqlite", "postgres", "mysql"), or None for an unknown one."""
    return _dialect_for_url(resolve_urls()[1])[0]


def sql_dialect_label() -> str:
    """The name of the SQL dialect for prompts: "SQLite", "PostgreSQL" or "MySQL"."""
    return _dialect_for_url(resolve_urls()[1])[1]
