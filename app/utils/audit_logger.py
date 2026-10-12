"""
SentinelSQL Audit Logger
------------------------
Records every query event, classification decision, human review and data egress.

Tamper evidence (alignment_plan SEC-07): each row stores `prev_hash` and `row_hash`,
where row_hash = SHA-256(prev_hash + the row's fields). Editing or deleting a row in
the middle of the log breaks the chain and `verify_audit_chain()` reports where.
This is tamper-*evident*, not tamper-*proof*: someone with write access to the file
can still rewrite the whole chain. Ship the log to write-once storage for that.

Clearing the log is allowed for administrators, but it is never silent: the first
row after a clear records who cleared it, how many rows were removed, and the hash
the removed chain ended on. The autoincrement counter is not reset.
"""

import datetime
import hashlib
import json
import logging
import os
import sqlite3
from typing import Any, Dict, List, Optional

logger = logging.getLogger("sentinel.audit")

GENESIS_HASH = "GENESIS"
CLEAR_EVENT_STATUS = "audit_logs_cleared"

_HASHED_FIELDS = (
    "run_id", "user_id", "role", "natural_language_query", "generated_sql",
    "confidence_score", "execution_status", "ingress_channel", "client_agent",
    "risk_level", "approved_by", "execution_time_ms", "masked_rows_count", "timestamp",
)

_INITIALIZED_PATHS: set = set()


def _audit_path() -> str:
    # Read on every call so the location can be redirected (e.g. by tests).
    return os.getenv("AUDIT_DB_PATH", "audit.db")


def _connect() -> sqlite3.Connection:
    path = _audit_path()
    # isolation_level=None: we manage BEGIN/COMMIT ourselves for the hash chain.
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    if path not in _INITIALIZED_PATHS:
        _init_audit_db(conn)
        _INITIALIZED_PATHS.add(path)
    return conn


def _init_audit_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT,
            user_id TEXT,
            role TEXT,
            natural_language_query TEXT,
            generated_sql TEXT,
            confidence_score INTEGER,
            execution_status TEXT,
            ingress_channel TEXT DEFAULT 'WEB_UI',
            client_agent TEXT DEFAULT 'sentinel-internal',
            risk_level TEXT DEFAULT 'LOW',
            approved_by TEXT,
            execution_time_ms REAL,
            masked_rows_count INTEGER,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            prev_hash TEXT,
            row_hash TEXT
        )
    """)
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(audit_logs)")}
    migrations = [
        ("ingress_channel", "TEXT DEFAULT 'WEB_UI'"),
        ("client_agent", "TEXT DEFAULT 'sentinel-internal'"),
        ("risk_level", "TEXT DEFAULT 'LOW'"),
        ("approved_by", "TEXT"),
        ("execution_time_ms", "REAL"),
        ("masked_rows_count", "INTEGER"),
        ("prev_hash", "TEXT"),
        ("row_hash", "TEXT"),
    ]
    for col_name, col_type in migrations:
        if col_name not in existing_cols:
            try:
                conn.execute(f"ALTER TABLE audit_logs ADD COLUMN {col_name} {col_type}")
            except sqlite3.OperationalError:
                pass


def _compute_hash(prev_hash: str, values: Dict[str, Any]) -> str:
    payload = json.dumps(
        [prev_hash] + [values.get(f) for f in _HASHED_FIELDS],
        separators=(",", ":"), ensure_ascii=False, default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _insert_chained(conn: sqlite3.Connection, values: Dict[str, Any]) -> str:
    """Appends one row, linking it to the previous row's hash. Returns the new row_hash."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        last = conn.execute(
            "SELECT row_hash FROM audit_logs WHERE row_hash IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        prev_hash = last[0] if last else GENESIS_HASH
        row_hash = _compute_hash(prev_hash, values)
        cols = list(_HASHED_FIELDS) + ["prev_hash", "row_hash"]
        conn.execute(
            f"INSERT INTO audit_logs ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            [values.get(f) for f in _HASHED_FIELDS] + [prev_hash, row_hash],
        )
        conn.execute("COMMIT")
        return row_hash
    except Exception:
        conn.execute("ROLLBACK")
        raise


def log_audit_event(
    run_id: Optional[str] = None,
    user_id: str = "anonymous",
    role: str = "guest",
    user_query: Optional[str] = None,
    generated_sql: Optional[str] = None,
    confidence_score: Optional[int] = None,
    execution_status: Optional[str] = None,
    ingress_channel: str = "WEB_UI",
    client_agent: str = "sentinel-internal",
    risk_level: str = "LOW",
    approved_by: Optional[str] = None,
    execution_time_ms: Optional[float] = None,
    masked_rows_count: Optional[int] = None,
) -> None:
    """Appends a query event to the hash-chained audit log."""
    values = {
        "run_id": run_id,
        "user_id": user_id,
        "role": role,
        "natural_language_query": user_query,
        "generated_sql": generated_sql,
        # Normalised so the stored value hashes identically when read back.
        "confidence_score": int(confidence_score) if confidence_score is not None else None,
        "execution_status": execution_status,
        "ingress_channel": ingress_channel,
        "client_agent": client_agent,
        "risk_level": risk_level,
        "approved_by": approved_by,
        "execution_time_ms": float(execution_time_ms) if execution_time_ms is not None else None,
        "masked_rows_count": int(masked_rows_count) if masked_rows_count is not None else None,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    try:
        conn = _connect()
        try:
            _insert_chained(conn, values)
        finally:
            conn.close()
    except Exception as e:
        logger.error("Failed to write to audit log: %s", e)


def get_audit_logs(
    limit: int = 50,
    status_filter: Optional[str] = None,
    ingress_filter: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Retrieves recent audit events (newest first) with optional filtering."""
    try:
        conn = _connect()
        try:
            conn.row_factory = sqlite3.Row
            query = "SELECT * FROM audit_logs WHERE 1=1"
            params: List[Any] = []
            if status_filter:
                query += " AND execution_status = ?"
                params.append(status_filter)
            if ingress_filter:
                query += " AND ingress_channel = ?"
                params.append(ingress_filter)
            query += " ORDER BY id DESC LIMIT ?"
            params.append(limit)
            return [dict(row) for row in conn.execute(query, params).fetchall()]
        finally:
            conn.close()
    except Exception as e:
        logger.error("Failed to read audit logs: %s", e)
        return []


def verify_audit_chain(anchor: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Recomputes the hash chain. Rows written before chaining existed (no hash) are tolerated only *before*
    the first chained row: an unhashed row appearing after the chain has started is itself evidence of
    tampering (a forged entry), and is reported.

    Without an anchor, truncating the newest rows, deleting the oldest rows, or rewriting the whole chain
    with recomputed hashes cannot be detected from the database alone. With an `anchor`
    ({"head_hash": ..., "count": ...}, previously obtained from `audit_anchor()` and stored somewhere the
    database's writer cannot reach) those three attacks are detected too: the anchored entry must still be
    present at the same position. A legitimate clear (which restarts the count) requires re-anchoring.
    """
    conn = _connect()
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM audit_logs ORDER BY id ASC").fetchall()
    finally:
        conn.close()

    previous_hash: Optional[str] = None
    chain_started = False
    position_of: Dict[str, int] = {}
    chained = 0
    for row in rows:
        data = dict(row)
        if data["row_hash"] is None:
            if chain_started:
                return {"valid": False, "checked": chained, "first_invalid_id": data["id"], "head_hash": previous_hash,
                        "reason": "an unhashed row appears after the chain started (forged or tampered entry)"}
            continue
        chain_started = True
        if previous_hash is not None and data["prev_hash"] != previous_hash:
            return {"valid": False, "checked": chained, "first_invalid_id": data["id"], "head_hash": previous_hash,
                    "reason": "chain link broken (a row was removed or reordered)"}
        if _compute_hash(data["prev_hash"], data) != data["row_hash"]:
            return {"valid": False, "checked": chained, "first_invalid_id": data["id"], "head_hash": previous_hash,
                    "reason": "row contents do not match their hash (row was edited)"}
        chained += 1
        position_of[data["row_hash"]] = chained
        previous_hash = data["row_hash"]

    result = {"valid": True, "checked": chained, "first_invalid_id": None, "reason": None, "head_hash": previous_hash}
    if anchor:
        where = position_of.get(anchor.get("head_hash"))
        if where is None:
            result.update(valid=False, reason="the anchored entry is missing: newer rows were truncated or the chain was rewritten")
        elif where != anchor.get("count"):
            result.update(valid=False, reason=f"the anchored entry moved from position {anchor.get('count')} to {where}: older rows were removed")
    return result


def audit_anchor() -> Dict[str, Any]:
    """The current chain head and length. Store it outside the audit database (write-once storage, a ticket, a
    separate service) and pass it back to `verify_audit_chain` later to detect truncation and full rewrites."""
    result = verify_audit_chain()
    return {"head_hash": result.get("head_hash"), "count": result["checked"], "valid_when_taken": result["valid"]}


def clear_audit_logs(cleared_by: str = "unknown") -> int:
    """
    Removes all audit rows, then records the clear itself as the first new row.
    Returns the number of rows removed. The autoincrement counter is left alone.
    """
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        count = conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]
        last = conn.execute(
            "SELECT row_hash FROM audit_logs WHERE row_hash IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        old_head = last[0] if last else GENESIS_HASH
        conn.execute("DELETE FROM audit_logs")
        values = {
            "user_id": cleared_by,
            "role": "admin",
            "natural_language_query": f"Cleared {count} audit records (previous chain head {old_head[:16]})",
            "execution_status": CLEAR_EVENT_STATUS,
            "ingress_channel": "WEB_UI",
            "client_agent": "sentinel-admin",
            "risk_level": "HIGH",
            "approved_by": cleared_by,
            "masked_rows_count": count,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        row_hash = _compute_hash(old_head, values)
        cols = list(_HASHED_FIELDS) + ["prev_hash", "row_hash"]
        conn.execute(
            f"INSERT INTO audit_logs ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            [values.get(f) for f in _HASHED_FIELDS] + [old_head, row_hash],
        )
        conn.execute("COMMIT")
        logger.warning("Audit log cleared by %s (%d records removed)", cleared_by, count)
        return count
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
