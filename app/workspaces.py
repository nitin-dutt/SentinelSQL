"""
The launch page's sessions: trying the sample database without a token, and connecting your own database.

Two things live here, both started from the public launch page and both kept in memory only (a restart ends them):

  * Sample sessions. With APP_ENV=dev anyone may enter as an employee, a reviewer or an agent without a token, for local
    development and testing. In demo mode (DEMO_MODE=true) the same button hands out the shared demo logins instead.
    Otherwise a token from AUTH_TOKENS is required, as before.

  * Workspaces. A visitor gives the URL of their own PostgreSQL or MySQL database and gets a private workspace: three
    session tokens (tester, reviewer, agent) that work only on that database. Every request made with one of them runs
    in a settings *scope* (see app/settings.py), so the pipeline, the policy gate, the review queue, the audit log, the
    traces and the cache all use that workspace's database and its own files. Nothing is shared with the sample
    database or with another workspace.

What protects the server and the visitor's database
  * Only postgres/mysql URLs, and only a short list of URL options, are accepted.
  * Unless WORKSPACE_ALLOW_PRIVATE_HOSTS is on (the default in dev only), the host must resolve to public addresses, and
    the connection is pinned to the address that was checked, so the server cannot be pointed at its own network.
  * The connection is read-only at the session level, and every query still passes the policy gate, the row cap and the
    statement timeout. A SELECT-only database user is still the stronger guarantee and the page says so.
  * The URL (with its password) is held in memory, never written to disk or to a log, and is dropped when the workspace
    closes: after WORKSPACE_TTL_MINUTES without a request, on disconnect, or at restart.
  * Each workspace may ask WORKSPACE_QUESTION_QUOTA questions; at most WORKSPACE_MAX_ACTIVE exist at once.
"""

import hmac
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app import demo
from app.auth import Actor, ROLE_ADMIN, ROLE_EMPLOYEE, ROLE_EXTERNAL_AI, get_actor, load_token_registry
from app.db import database
from app.db.urls import _dialect_for_url, normalize_database_url, sql_dialect_label
from app.settings import settings
from app.utils.logger import get_logger

logger = get_logger("sentinel.workspaces")
router = APIRouter()

_ID = re.compile(r"^ws-[0-9a-f]{12}$")
_ROLE_USER = {ROLE_EMPLOYEE: "tester", ROLE_ADMIN: "reviewer", ROLE_EXTERNAL_AI: "agent"}
_DEV_USER = {ROLE_EMPLOYEE: "dev-employee", ROLE_ADMIN: "dev-reviewer", ROLE_EXTERNAL_AI: "dev-agent"}
# URL options a visitor may pass. Everything else is refused: options can name local files, sockets or server settings.
_ALLOWED_OPTIONS = {"postgresql": {"sslmode", "channel_binding"}, "mysql": {"charset"}}
_CONNECT_ATTEMPTS_PER_MINUTE = 5


@dataclass
class Workspace:
    id: str
    directory: str
    label: str                       # "PostgreSQL · shop on db.example.com": never contains credentials
    dialect: str
    tables: int
    quota: int
    values: Dict[str, Optional[str]]  # the settings scope: this workspace's DATABASE_URL and file locations
    tokens: Dict[str, str]            # role -> session token
    created: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.monotonic)
    used: int = 0

    def summary(self) -> dict:
        ttl = settings.workspaces.ttl_seconds
        return {"id": self.id, "label": self.label, "dialect": self.dialect, "tables": self.tables,
                "questions_used": self.used, "question_quota": self.quota,
                "idle_minutes_allowed": int(ttl // 60)}


_lock = threading.Lock()
_workspaces: Dict[str, Workspace] = {}
_tokens: Dict[str, Tuple[Optional[str], Actor]] = {}    # session token -> (workspace id or None, who it is)
_dev_tokens: Dict[str, str] = {}                         # role -> token of the no-token development login
_connect_limiter = demo._RateLimiter()


# --------------------------------------------------------------------------- lookups used on every request

def _match(token: str) -> Optional[Tuple[Optional[str], Actor]]:
    found = None
    for known, entry in list(_tokens.items()):
        if hmac.compare_digest(known.encode("utf-8"), token.encode("utf-8")):
            found = entry
    return found


def actor_for_token(token: str) -> Optional[Actor]:
    """Who a launch-page session token belongs to, or None. Called by app.auth after the AUTH_TOKENS registry."""
    entry = _match(token)
    if entry is None:
        return None
    workspace_id, actor = entry
    if workspace_id is None:
        return actor if settings.launch.dev else None     # development logins stop working the moment APP_ENV is not dev
    return actor if workspace_id in _workspaces else None


def workspace_for_token(token: str) -> Optional[Workspace]:
    entry = _match(token)
    if entry is None or entry[0] is None:
        return None
    return _workspaces.get(entry[0])


def _bearer(request: Request) -> str:
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


# --------------------------------------------------------------------------- opening and closing workspaces

def _root() -> str:
    return os.path.abspath(settings.workspaces.directory)


def close(workspace_id: str) -> None:
    """Forgets a workspace: its tokens stop working, its connection is closed and its files are deleted."""
    from app.cache import semantic_cache
    from app.rag import schema_retriever
    from app.utils import schemacontext
    with _lock:
        workspace = _workspaces.pop(workspace_id, None)
        for token in [t for t, (owner, _actor) in _tokens.items() if owner == workspace_id]:
            del _tokens[token]
    database.drop_scoped_engines(workspace_id)
    schemacontext.forget_scope(workspace_id)
    schema_retriever.forget_scope(workspace_id)
    semantic_cache.forget_scope(workspace_id)
    if workspace is not None and _ID.match(workspace.id):
        shutil.rmtree(workspace.directory, ignore_errors=True)
        logger.info("Workspace %s closed", workspace.id)


def _sweep() -> None:
    ttl = settings.workspaces.ttl_seconds
    now = time.monotonic()
    for workspace in list(_workspaces.values()):
        if now - workspace.last_used > ttl:
            close(workspace.id)


def reset() -> None:
    """Closes every workspace and forgets every launch-page session (tests, and app.runtime.reset_runtime_state)."""
    for workspace_id in list(_workspaces):
        close(workspace_id)
    with _lock:
        _tokens.clear()
        _dev_tokens.clear()
    _connect_limiter.reset()


def purge_leftovers() -> None:
    """At startup: delete workspace folders a previous process left behind (their sessions died with it)."""
    root = _root()
    if not os.path.isdir(root):
        return
    for name in os.listdir(root):
        if _ID.match(name) and name not in _workspaces:
            shutil.rmtree(os.path.join(root, name), ignore_errors=True)


def _refuse(detail: str, status: int = 422) -> HTTPException:
    return HTTPException(status_code=status, detail=detail)


def _checked_url(raw: str):
    """Parses and vets a visitor's database URL. Returns (sqlalchemy URL, backend, address to pin the connection to or None)."""
    try:
        url = make_url(normalize_database_url(raw))
    except Exception:
        raise _refuse("That is not a database URL. Expected postgresql://user:password@host:5432/dbname "
                      "or mysql://user:password@host:3306/dbname.") from None
    backend = url.get_backend_name()
    if backend == "mariadb":
        backend = "mysql"
    if backend not in _ALLOWED_OPTIONS:
        raise _refuse("Only PostgreSQL and MySQL databases can be connected here.")
    if not url.host or not url.database or not url.username:
        raise _refuse("The URL needs a user, a host and a database name: postgresql://user:password@host:5432/dbname")
    extra = set(url.query) - _ALLOWED_OPTIONS[backend]
    if extra:
        raise _refuse(f"URL option not allowed here: {', '.join(sorted(extra))}. "
                      f"Allowed: {', '.join(sorted(_ALLOWED_OPTIONS[backend])) or 'none'}.")

    if settings.workspaces.allow_private_hosts:
        return url, backend, None
    port = url.port or (5432 if backend == "postgresql" else 3306)
    try:
        addresses = sorted({info[4][0] for info in socket.getaddrinfo(url.host, port, type=socket.SOCK_STREAM)})
    except OSError:
        raise _refuse(f"The host {url.host} could not be found.") from None
    if not addresses or not all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addresses):
        raise _refuse("The host must be reachable from the internet. Local and private network addresses are not accepted "
                      "by the hosted demo: run SentinelSQL yourself to use a database on your own network.")
    return url, backend, addresses[0]


def _engine_for(url, backend: str, pinned: Optional[str]):
    if backend == "postgresql":
        args = {"connect_timeout": 5, "application_name": "sentinelsql-workspace"}
        if pinned:
            args["hostaddr"] = pinned       # connect to the address that was checked; the host name is kept for TLS
    else:
        args = {"connection_timeout": 5}
        if pinned:
            url = url.set(host=pinned)
    engine = create_engine(url, connect_args=args, pool_size=2, max_overflow=1, pool_pre_ping=True, pool_recycle=300)
    database.enforce_read_only_sessions(engine)
    return engine


def _safe_error(error: Exception, password: Optional[str]) -> str:
    message = str(getattr(error, "orig", error)).strip().splitlines()[0] if str(error).strip() else type(error).__name__
    # Drivers do not normally echo the password, but a URL or a key=value pair in a message would carry it.
    message = re.sub(r"(://[^:/@\s]+):[^@\s]+@", r"\1:***@", message)
    message = re.sub(r"(?i)(password\s*=\s*)\S+", r"\1***", message)
    if password and len(password) >= 8:      # a short password would also match ordinary words in the message
        message = message.replace(password, "***")
    return message[:240]


def connect_database(raw_url: str) -> Workspace:
    """Opens a workspace on the visitor's database, or raises HTTPException with a message the visitor can act on."""
    _sweep()
    if len(_workspaces) >= settings.workspaces.max_active:
        raise _refuse("Every free workspace is in use right now. Please try again in a few minutes, or use the sample database.", 503)

    url, backend, pinned = _checked_url(raw_url)
    engine = _engine_for(url, backend, pinned)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as e:
        engine.dispose()
        raise _refuse(f"Could not connect: {_safe_error(e, url.password)}") from None

    workspace_id = "ws-" + secrets.token_hex(6)
    directory = os.path.join(_root(), workspace_id)
    os.makedirs(directory, exist_ok=True)
    descriptions = os.path.join(directory, "table_descriptions.json")
    with open(descriptions, "w", encoding="utf-8") as f:
        json.dump({}, f)                                  # the bundled descriptions are about the sample tables
    path = lambda name: os.path.join(directory, name)     # noqa: E731
    values: Dict[str, Optional[str]] = {
        "DATABASE_URL": url.render_as_string(hide_password=False), "ACTIVE_DB": "", "READONLY_DATABASE_URL": None,
        "DB_FALLBACK_TO_SQLITE": "", "SEED_DEMO_DATA": "false",
        "AUDIT_DB_PATH": path("audit.db"), "HITL_DB_PATH": path("hitl_runs.db"), "TRACE_DB_PATH": path("traces.db"),
        "CONVERSATION_DB_PATH": path("conversations.db"), "DICTIONARY_DB_PATH": path("app.db"), "GOLDEN_DB_PATH": path("app.db"),
        "DICTIONARY_VECTOR_CACHE": path("dictionary_vectors.json"), "GOLDEN_VECTOR_CACHE": path("golden_vectors.json"),
        "TABLE_DESCRIPTIONS_PATH": descriptions,
    }
    dialect = _dialect_for_url(values["DATABASE_URL"])[1]
    workspace = Workspace(id=workspace_id, directory=directory, label=f"{dialect} · {url.database} on {url.host}",
                          dialect=dialect, tables=0, quota=settings.workspaces.question_quota, values=values,
                          tokens={role: secrets.token_urlsafe(24) for role in _ROLE_USER})
    database.register_scoped_engines(workspace_id, engine, engine)
    with _lock:
        _workspaces[workspace_id] = workspace
        for role, token in workspace.tokens.items():
            _tokens[token] = (workspace_id, Actor(user_id=_ROLE_USER[role], role=role))

    try:
        from app.utils.schemacontext import introspect_schema
        with settings.scoped(workspace_id, directory, values):
            workspace.tables = len(introspect_schema().get("tables", {}))
    except Exception as e:
        close(workspace_id)
        raise _refuse(f"Connected, but the tables could not be read: {_safe_error(e, url.password)}") from None
    if workspace.tables == 0:
        close(workspace_id)
        raise _refuse("Connected, but no tables were found in the default schema (PostgreSQL: public; MySQL: the database in the URL).")
    if workspace.tables > settings.workspaces.max_tables:
        count = workspace.tables
        close(workspace_id)
        raise _refuse(f"This database has {count} tables; the free workspace handles up to {settings.workspaces.max_tables}.")
    logger.info("Workspace %s opened: %s, %d tables", workspace_id, workspace.label, workspace.tables)
    return workspace


# --------------------------------------------------------------------------- sample sessions

def _sample_tokens() -> Dict[str, str]:
    """role -> token for trying the sample database without typing one: dev logins, or the demo's shared logins."""
    if settings.launch.dev:
        with _lock:
            for role, user in _DEV_USER.items():
                if role not in _dev_tokens:
                    _dev_tokens[role] = secrets.token_urlsafe(24)
                    _tokens[_dev_tokens[role]] = (None, Actor(user_id=user, role=role))
            return dict(_dev_tokens)
    if demo.demo_enabled():
        public = settings.demo.public_users
        return {actor.role: token for token, actor in load_token_registry().items() if actor.user_id in public}
    return {}


def _mode() -> str:
    return "dev" if settings.launch.dev else "demo" if demo.demo_enabled() else "standard"


# --------------------------------------------------------------------------- HTTP

class ConnectRequest(BaseModel):
    url: str


@router.get("/launch/info")
def launch_info():
    """Public. Everything the launch page needs to decide what to offer; never a token."""
    mode = _mode()
    sample: Dict[str, object] = {"one_click": mode in ("dev", "demo"), "roles": sorted(_sample_tokens())}
    try:
        from app.utils.schemacontext import introspect_schema
        sample.update(dialect=sql_dialect_label(), tables=len(introspect_schema().get("tables", {})))
    except Exception:
        pass
    info: Dict[str, object] = {
        "mode": mode,
        "sample": sample,
        "connect": {"enabled": settings.workspaces.enabled, "question_quota": settings.workspaces.question_quota,
                    "idle_minutes": int(settings.workspaces.ttl_seconds // 60), "max_tables": settings.workspaces.max_tables,
                    "private_hosts": settings.workspaces.allow_private_hosts},
        "examples": demo.EXAMPLE_QUESTIONS,
        "limits": None,
    }
    if mode == "demo":
        info["limits"] = {"rate_per_minute": settings.demo.rate_per_minute, "daily_cap": demo.budget.cap(),
                          "remaining_today": demo.budget.remaining(), "max_question_chars": demo.MAX_QUERY_CHARS,
                          "notice": settings.demo.notice}
    return info


@router.post("/launch/sample")
def launch_sample():
    """Public, and only in dev or demo mode: session tokens for the sample database, one per role."""
    tokens = _sample_tokens()
    if not tokens:
        raise HTTPException(status_code=403, detail="This server requires an access token.")
    return {"tokens": tokens}


@router.post("/launch/connect")
def launch_connect(req: ConnectRequest, request: Request):
    """Public when workspaces are enabled: connects the visitor's own database and returns their workspace."""
    if not settings.workspaces.enabled:
        raise HTTPException(status_code=403, detail="Connecting your own database is switched off on this server.")
    if not _connect_limiter.allow(demo.client_address(request), _CONNECT_ATTEMPTS_PER_MINUTE):
        raise HTTPException(status_code=429, detail="Too many connection attempts. Please wait a minute.", headers={"Retry-After": "60"})
    workspace = connect_database(req.url)
    return {"tokens": workspace.tokens, "workspace": workspace.summary()}


@router.get("/launch/session")
def launch_session(request: Request, actor: Actor = Depends(get_actor)):
    """Who the caller is and, for a workspace session, which database it is on and how much of its quota is left."""
    workspace = workspace_for_token(_bearer(request))
    return {"user_id": actor.user_id, "role": actor.role, "workspace": workspace.summary() if workspace else None}


@router.delete("/launch/workspace")
def launch_disconnect(request: Request, actor: Actor = Depends(get_actor)):
    """Closes the caller's workspace now: the connection, the tokens and every file kept for it."""
    workspace = workspace_for_token(_bearer(request))
    if workspace is None:
        raise HTTPException(status_code=404, detail="This session is not a connected workspace.")
    close(workspace.id)
    return {"status": "closed"}


def install(app: FastAPI) -> None:
    """Registers the /launch routes and the middleware that runs a workspace's requests in its settings scope."""
    app.include_router(router)

    @app.exception_handler(database.ScopeClosedError)
    async def _closed(_request: Request, error: database.ScopeClosedError):
        return JSONResponse(status_code=410, content={"detail": str(error)})

    @app.middleware("http")
    async def workspace_scope(request: Request, call_next):
        token = _bearer(request)
        workspace = workspace_for_token(token) if token else None
        if workspace is None:
            return await call_next(request)
        workspace.last_used = time.monotonic()
        if request.method == "POST" and request.url.path in demo.RATE_LIMITED_PATHS:
            with _lock:
                if workspace.used >= workspace.quota:
                    return JSONResponse(status_code=429, content={"detail": (
                        f"This free workspace has used its {workspace.quota} questions. "
                        "Run SentinelSQL yourself for unlimited use, or keep exploring the sample database.")})
                workspace.used += 1
        scope = settings.enter_scope(workspace.id, workspace.directory, workspace.values)
        try:
            return await call_next(request)
        finally:
            settings.exit_scope(scope)


def startup() -> None:
    purge_leftovers()
    if settings.launch.dev:
        logger.warning("APP_ENV=dev: anyone who can reach this server can enter without a token. Never use it on a public host.")
    if settings.workspaces.enabled and settings.workspaces.allow_private_hosts and not settings.launch.dev:
        logger.warning("WORKSPACE_ALLOW_PRIVATE_HOSTS is on outside dev: visitors can point the server at its own network.")


__all__: List[str] = ["Workspace", "actor_for_token", "workspace_for_token", "connect_database", "close", "reset", "install", "startup"]
