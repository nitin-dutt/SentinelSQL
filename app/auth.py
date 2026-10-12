"""
SentinelSQL authentication & role-based access control (alignment_plan SEC-03).

Identity comes from a bearer token that the server recognises, never from
client-supplied headers such as X-Role. Tokens are configured in the environment:

    AUTH_TOKENS="<token>:<user_id>:<role>,<token>:<user_id>:<role>"

Roles:
    employee     - may ask natural-language questions (/query)
    admin        - everything employee can do, plus the review queue and admin APIs
    external_ai  - an MCP / agent client (/mcp/*)

If no valid tokens are configured, every protected route returns 401 (fail closed).
The registry is read on each request so tokens can be rotated by restarting the
process and so tests can set them per-test.
"""

import hmac
import logging
import os
from dataclasses import dataclass
from typing import Callable, Dict, Optional

from fastapi import Depends, HTTPException, Request

logger = logging.getLogger("sentinel.auth")

ROLE_EMPLOYEE = "employee"
ROLE_ADMIN = "admin"
ROLE_EXTERNAL_AI = "external_ai"
VALID_ROLES = {ROLE_EMPLOYEE, ROLE_ADMIN, ROLE_EXTERNAL_AI}


@dataclass(frozen=True)
class Actor:
    user_id: str
    role: str


def load_token_registry() -> Dict[str, Actor]:
    """Parses AUTH_TOKENS into {token: Actor}. Malformed entries are skipped loudly."""
    registry: Dict[str, Actor] = {}
    for item in os.getenv("AUTH_TOKENS", "").split(","):
        item = item.strip()
        if not item:
            continue
        parts = item.split(":")
        if len(parts) != 3 or not all(p.strip() for p in parts):
            logger.warning("Ignoring malformed AUTH_TOKENS entry (expected token:user_id:role)")
            continue
        token, user_id, role = (p.strip() for p in parts)
        if role not in VALID_ROLES:
            logger.warning("Ignoring AUTH_TOKENS entry for '%s': unknown role '%s'", user_id, role)
            continue
        registry[token] = Actor(user_id=user_id, role=role)
    return registry


def authenticate_token(token: str) -> Optional[Actor]:
    """Constant-time lookup of a bearer token."""
    match: Optional[Actor] = None
    for known, actor in load_token_registry().items():
        if hmac.compare_digest(known.encode("utf-8"), token.encode("utf-8")):
            match = actor
    return match


def get_actor(request: Request) -> Actor:
    """FastAPI dependency: resolves the caller from the Authorization header or raises 401."""
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    actor = authenticate_token(token)
    if actor is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid or unknown token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return actor


def require_roles(*roles: str) -> Callable[..., Actor]:
    """FastAPI dependency factory: the caller must be authenticated and hold one of `roles`."""
    allowed = set(roles)

    def dependency(actor: Actor = Depends(get_actor)) -> Actor:
        if actor.role not in allowed:
            raise HTTPException(status_code=403, detail=f"Requires role: {', '.join(sorted(allowed))}")
        return actor

    return dependency


def self_approval_allowed() -> bool:
    """Separation of duties: by default a reviewer may not approve a run they submitted."""
    return os.getenv("HITL_ALLOW_SELF_APPROVAL", "false").strip().lower() in ("1", "true", "yes")
