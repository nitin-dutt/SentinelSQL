"""
Human-in-the-Loop run store (alignment_plan DAT-03).

Paused runs used to live in a Python dict, so a restart (or `uvicorn --reload`) silently
dropped every pending approval, MCP clients polling afterwards got `not_found`, and two worker
processes could not see each other's runs. Runs are now persisted in a small SQLite file
(HITL_DB_PATH, default `hitl_runs.db`), separate from both the audited log and the queried database.

Lifecycle
  * pending runs expire after HITL_PENDING_TTL_HOURS (default 72) and become `expired`;
  * finished runs (completed / rejected / failed / expired) are deleted after
    HITL_FINISHED_TTL_HOURS (default 24) so approved result sets do not linger.
Expiry is applied lazily on every access, so no background job is needed.
"""

import json
import os
import sqlite3
import threading
import time
from typing import Dict, Optional
from uuid import uuid4

from app.utils.logger import get_logger

logger = get_logger("sentinel.hitl")

_INITIALIZED: set = set()
_init_lock = threading.Lock()


def _path() -> str:
    return os.getenv("HITL_DB_PATH", "hitl_runs.db")


def _pending_ttl() -> float:
    return float(os.getenv("HITL_PENDING_TTL_HOURS", "72")) * 3600


def _finished_ttl() -> float:
    return float(os.getenv("HITL_FINISHED_TTL_HOURS", "24")) * 3600


def _connect() -> sqlite3.Connection:
    path = _path()
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    if path not in _INITIALIZED:
        with _init_lock:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS hitl_runs (
                    run_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    user_id TEXT,
                    state_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_hitl_status ON hitl_runs(status)")
            conn.commit()
            _INITIALIZED.add(path)
    return conn


def _purge(conn: sqlite3.Connection) -> None:
    now = time.time()
    conn.execute(
        "UPDATE hitl_runs SET status = 'expired', updated_at = ? WHERE status = 'pending' AND created_at < ?",
        (now, now - _pending_ttl()),
    )
    conn.execute(
        "DELETE FROM hitl_runs WHERE status != 'pending' AND updated_at < ?",
        (now - _finished_ttl(),),
    )
    conn.commit()


def _hydrate(row: sqlite3.Row) -> dict:
    state = json.loads(row["state_json"])
    state["status"] = row["status"]
    return state


def save_run(state: dict, status: str = "pending") -> str:
    """Persist a paused execution state and return its run_id."""
    run_id = str(uuid4())
    payload = {k: v for k, v in state.items() if k != "status"}
    now = time.time()
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO hitl_runs (run_id, status, user_id, state_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, status, state.get("user_id"), json.dumps(payload, default=str), now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return run_id


def get_run(run_id: str) -> dict:
    """Retrieve a run by id. Raises KeyError if it does not exist or has been cleaned up."""
    conn = _connect()
    try:
        _purge(conn)
        row = conn.execute("SELECT * FROM hitl_runs WHERE run_id = ?", (run_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise KeyError(f"Run ID not found: {run_id}")
    return _hydrate(row)


def update_run(
    run_id: str,
    status: str,
    results: Optional[dict] = None,
    reason: Optional[str] = None,
    sql: Optional[str] = None,
) -> None:
    """Update status and attach results / reason / the SQL that actually ran, for client polling."""
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM hitl_runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return
        state = json.loads(row["state_json"])
        if results is not None:
            state["results"] = results
        if reason is not None:
            state["reason"] = reason
        if sql is not None:
            state["sql"] = sql
        conn.execute(
            "UPDATE hitl_runs SET status = ?, state_json = ?, updated_at = ? WHERE run_id = ?",
            (status, json.dumps(state, default=str), time.time(), run_id),
        )
        conn.commit()
    finally:
        conn.close()


def delete_run(run_id: str) -> None:
    conn = _connect()
    try:
        conn.execute("DELETE FROM hitl_runs WHERE run_id = ?", (run_id,))
        conn.commit()
    finally:
        conn.close()


def get_all_runs() -> Dict[str, dict]:
    conn = _connect()
    try:
        _purge(conn)
        rows = conn.execute("SELECT * FROM hitl_runs ORDER BY created_at").fetchall()
    finally:
        conn.close()
    return {r["run_id"]: _hydrate(r) for r in rows}


def get_pending_runs() -> Dict[str, dict]:
    """Only runs awaiting human administrator review."""
    conn = _connect()
    try:
        _purge(conn)
        rows = conn.execute("SELECT * FROM hitl_runs WHERE status = 'pending' ORDER BY created_at").fetchall()
    finally:
        conn.close()
    return {r["run_id"]: _hydrate(r) for r in rows}


def clear_runs() -> None:
    """Remove every run (tests and admin tooling)."""
    conn = _connect()
    try:
        conn.execute("DELETE FROM hitl_runs")
        conn.commit()
    finally:
        conn.close()
