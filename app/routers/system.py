"""Identity, schema and health endpoints."""

from fastapi import APIRouter, Depends

from app.auth import Actor, get_actor
from app.utils.schemacontext import introspect_schema

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
