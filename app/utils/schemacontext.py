"""
SentinelSQL Dynamic Schema Introspection & Context Engine
---------------------------------------------------------
Dynamically inspects the connected database (SQLite, PostgreSQL, MySQL)
via SQLAlchemy's universal inspection API to discover tables, columns,
data types, and relational foreign keys with zero hardcoding.
"""

from typing import Dict, Any, List, Optional
from sqlalchemy import inspect
from app.utils.logger import get_logger

logger = get_logger("sentinel.schema")

# System/internal tables that should not be exposed to LLM query generation
IGNORED_TABLES = {"sqlite_sequence", "audit_logs", "alembic_version", "golden_queries", "data_dictionary"}  # internal bookkeeping, never offered to the model

_CACHED_SCHEMA: Optional[Dict[str, Any]] = None


class SchemaUnavailableError(RuntimeError):
    """The live database could not be introspected."""


def clear_schema_cache() -> None:
    """Forget the cached schema so the next call re-inspects the database (after DDL or seeding)."""
    global _CACHED_SCHEMA
    _CACHED_SCHEMA = None


def introspect_schema(force_refresh: bool = False) -> Dict[str, Any]:
    """
    Introspects the active database to extract tables, columns, and foreign keys.
    Results are cached in memory for high-performance sub-millisecond retrieval.
    """
    global _CACHED_SCHEMA
    if _CACHED_SCHEMA is not None and not force_refresh:
        return _CACHED_SCHEMA

    try:
        from app.db.database import get_engine
        inspector = inspect(get_engine())
        table_names = inspector.get_table_names()

        tables_data: Dict[str, Dict[str, Any]] = {}
        all_relationships: List[str] = []

        for table in table_names:
            if table.lower() in IGNORED_TABLES:
                continue

            columns = []
            for col in inspector.get_columns(table):
                col_type = str(col.get("type", "UNKNOWN")).upper()
                columns.append({
                    "name": col["name"],
                    "type": col_type,
                    "primary_key": bool(col.get("primary_key", False)),
                    "nullable": bool(col.get("nullable", True))
                })

            foreign_keys = []
            try:
                for fk in inspector.get_foreign_keys(table):
                    ref_table = fk.get("referred_table")
                    constrained = fk.get("constrained_columns", [])
                    referred = fk.get("referred_columns", [])
                    if constrained and referred and ref_table:
                        rel_str = f"{table}.{constrained[0]} -> {ref_table}.{referred[0]}"
                        foreign_keys.append(rel_str)
                        all_relationships.append(rel_str)
            except Exception:
                pass

            col_names = [c["name"] for c in columns]
            text_card = f"{table}({', '.join(col_names)})"

            tables_data[table] = {
                "columns": columns,
                "column_names": col_names,
                "foreign_keys": foreign_keys,
                "text_repr": text_card
            }

        # Build formatted schema context string for LLM prompts
        lines = []
        for table, meta in tables_data.items():
            lines.append(meta["text_repr"])

        if all_relationships:
            lines.append("")
            lines.append("Relationships:")
            for rel in all_relationships:
                lines.append(rel)

        formatted_context = "\n".join(lines).strip()

        _CACHED_SCHEMA = {
            "tables": tables_data,
            "relationships": all_relationships,
            "formatted_context": formatted_context
        }
        return _CACHED_SCHEMA

    except Exception as e:
        # Do not invent a schema: a hand-written fallback would silently present tables that may
        # not exist and let the model generate SQL against a database that is not there.
        logger.error("Schema introspection failed: %s", e)
        raise SchemaUnavailableError(f"Could not inspect the database schema: {e}") from e


def get_schema_context(force_refresh: bool = False) -> str:
    """
    Returns the formatted database schema context string for LLM generation prompts.
    Dynamically reflects the live connected database.
    """
    data = introspect_schema(force_refresh=force_refresh)
    return data.get("formatted_context", "")


def get_schema_tables_dict(force_refresh: bool = False) -> Dict[str, Dict[str, Any]]:
    """
    Returns structured table metadata for Schema RAG vector indexing.
    """
    data = introspect_schema(force_refresh=force_refresh)
    return data.get("tables", {})