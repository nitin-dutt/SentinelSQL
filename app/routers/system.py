"""Identity, schema and health endpoints."""

from fastapi import APIRouter, Depends

from app.auth import Actor, get_actor
from app.rag.schema_retriever import load_table_descriptions
from app.utils.schemacontext import introspect_schema
from app.utils.sql_parser import is_sensitive_table

router = APIRouter()


@router.get("/health")
def health():
    """Liveness probe for containers and load balancers. Deliberately reveals nothing else."""
    return {"status": "ok"}


@router.get("/auth/me")
def whoami(actor: Actor = Depends(get_actor)):
    """Lets a client confirm its token and learn its role (used by the UI login)."""
    return {"user_id": actor.user_id, "role": actor.role}


@router.get("/schema")
def get_database_schema_endpoint(actor: Actor = Depends(get_actor)):
    """Returns the live introspected schema of the connected database."""
    return introspect_schema()


@router.get("/schema/overview")
def get_schema_overview_endpoint(actor: Actor = Depends(get_actor)):
    """
    The database as a person wants to read it: per table a plain-language description, whether the governance policy treats it as
    sensitive (questions about it are held for human review), its columns and its foreign keys. Used by the UI's structure panel.
    """
    descriptions = load_table_descriptions()
    tables = []
    for name, info in sorted(introspect_schema().get("tables", {}).items()):
        tables.append({
            "name": name,
            "description": descriptions.get(name.lower(), ""),
            "sensitive": is_sensitive_table(name),
            "columns": [{"name": c["name"], "type": str(c["type"]).split("(")[0].lower(), "primary_key": bool(c.get("primary_key"))}
                        for c in info.get("columns", [])],
            "foreign_keys": info.get("foreign_keys", []),
        })
    return {"tables": tables}
