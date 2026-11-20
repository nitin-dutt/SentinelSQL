"""
Conversation memory for the analyst chat (POST /chat).

What is kept per turn: the user's message, the standalone question it was understood as, the outcome, the SQL, and the
SHAPE of the answer (column names and row count). Result rows are never stored here: they would be a second, unmasked-by-
review copy of query output. The browser holds the rows it was shown; the server only needs the shape to resolve
"now break that down by region".

Stored in a small SQLite file (CONVERSATION_DB_PATH, default conversations.db). A conversation belongs to the user who
started it and expires CONVERSATION_TTL_HOURS (default 24) after its last turn; expiry is applied lazily on access.
"""

import json
import sqlite3
import time
from typing import Any, Dict, List, Optional
from uuid import uuid4

from app.settings import settings
from app.utils.sqlite_store import SqliteStore, delete_older_than

MAX_TURNS_PER_CONVERSATION = 40


def _path() -> str:
    return settings.stores.conversation_db


def _create(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS conversations (
        conversation_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS turns (
        turn_id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL, created_at REAL NOT NULL,
        turn_json TEXT NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_turns_conversation ON turns(conversation_id)")


_STORE = SqliteStore(_path, _create)
_connect = _STORE.connect


def _purge(conn: sqlite3.Connection) -> None:
    ttl = settings.analyst.conversation_ttl_seconds
    conn.execute("DELETE FROM turns WHERE conversation_id IN (SELECT conversation_id FROM conversations WHERE updated_at < ?)", (time.time() - ttl,))
    delete_older_than(conn, "conversations", "updated_at", ttl)
    conn.commit()


def create_conversation(user_id: str) -> str:
    conversation_id = uuid4().hex
    now = time.time()
    conn = _connect()
    try:
        conn.execute("INSERT INTO conversations VALUES (?, ?, ?, ?)", (conversation_id, user_id, now, now))
        conn.commit()
    finally:
        conn.close()
    return conversation_id


def get_turns(conversation_id: str, user_id: str) -> List[Dict[str, Any]]:
    """The conversation's turns, oldest first. Raises KeyError if it does not exist, has expired or belongs to someone else."""
    conn = _connect()
    try:
        _purge(conn)
        owner = conn.execute("SELECT user_id FROM conversations WHERE conversation_id = ?", (conversation_id,)).fetchone()
        if owner is None or owner["user_id"] != user_id:
            raise KeyError(conversation_id)
        rows = conn.execute("SELECT turn_json FROM turns WHERE conversation_id = ? ORDER BY turn_id", (conversation_id,)).fetchall()
    finally:
        conn.close()
    return [json.loads(r["turn_json"]) for r in rows]


def add_turn(conversation_id: str, *, message: str, standalone_question: str, status: str, sql: Optional[str] = None,
             columns: Optional[List[str]] = None, row_count: Optional[int] = None, reason: Optional[str] = None,
             clarification: Optional[Dict[str, Any]] = None) -> None:
    turn = {"message": message, "standalone_question": standalone_question, "status": status, "sql": sql,
            "columns": columns, "row_count": row_count, "reason": (reason or None) and reason[:300], "clarification": clarification}
    now = time.time()
    conn = _connect()
    try:
        conn.execute("INSERT INTO turns (conversation_id, created_at, turn_json) VALUES (?, ?, ?)", (conversation_id, now, json.dumps(turn)))
        conn.execute("UPDATE conversations SET updated_at = ? WHERE conversation_id = ?", (now, conversation_id))
        # keep only the newest turns of a very long conversation
        conn.execute("""DELETE FROM turns WHERE conversation_id = ? AND turn_id NOT IN
                        (SELECT turn_id FROM turns WHERE conversation_id = ? ORDER BY turn_id DESC LIMIT ?)""",
                     (conversation_id, conversation_id, MAX_TURNS_PER_CONVERSATION))
        conn.commit()
    finally:
        conn.close()


def delete_conversation(conversation_id: str, user_id: str) -> bool:
    conn = _connect()
    try:
        owner = conn.execute("SELECT user_id FROM conversations WHERE conversation_id = ?", (conversation_id,)).fetchone()
        if owner is None or owner["user_id"] != user_id:
            return False
        conn.execute("DELETE FROM turns WHERE conversation_id = ?", (conversation_id,))
        conn.execute("DELETE FROM conversations WHERE conversation_id = ?", (conversation_id,))
        conn.commit()
        return True
    finally:
        conn.close()


def clear_conversations() -> None:
    """Remove everything (tests)."""
    conn = _connect()
    try:
        conn.execute("DELETE FROM turns")
        conn.execute("DELETE FROM conversations")
        conn.commit()
    finally:
        conn.close()
