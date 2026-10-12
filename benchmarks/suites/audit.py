"""
Audit-log suite.

Question answered: if someone with write access to audit.db alters history, does SentinelSQL notice, and
what exactly can it NOT notice?

Method: a baseline log is generated with the real `log_audit_event`. For each attack, a copy of the
database file is tampered with directly through SQLite (the attacker model: write access to the file, no
knowledge needed beyond the schema; "rewrite_whole_chain" additionally knows the hashing scheme) and the
verifier runs. Three verifiers are compared on the same tampered files:

  previous         the verifier as first shipped (skips any row without a hash)
  current          the verifier now (flags unhashed rows after the chain started)
  current+anchor   the current verifier given a chain head/length recorded earlier outside the database

Also measured: cost of writing a chained event and of verifying a long chain.
"""

import os
import shutil
import sqlite3
import time
from typing import Any, Callable, Dict, List

from benchmarks.lib.meta import run_metadata
from benchmarks.lib.stats import latency_summary, proportion
from benchmarks.suites.common import WORK_DIR, ensure_dirs, save_result

BASE_EVENTS = 300


def previous_verifier(conn: sqlite3.Connection) -> bool:
    """The verifier as it was first shipped: rows without a hash are skipped. Returns True when it reports valid."""
    from app.utils.audit_logger import _compute_hash
    conn.row_factory = sqlite3.Row
    prev = None
    for row in conn.execute("SELECT * FROM audit_logs WHERE row_hash IS NOT NULL ORDER BY id ASC").fetchall():
        data = dict(row)
        if prev is not None and data["prev_hash"] != prev:
            return False
        if _compute_hash(data["prev_hash"], data) != data["row_hash"]:
            return False
        prev = data["row_hash"]
    return True


# ---------------------------------------------------------------------------- attacks

def attack_edit_sql(c): c.execute("UPDATE audit_logs SET generated_sql = 'SELECT 1' WHERE id = 150")
def attack_edit_approver(c): c.execute("UPDATE audit_logs SET approved_by = 'nobody' WHERE id = 200")
def attack_edit_timestamp(c): c.execute("UPDATE audit_logs SET timestamp = '2020-01-01T00:00:00+00:00' WHERE id = 100")
def attack_garbage_hash(c): c.execute("UPDATE audit_logs SET row_hash = 'deadbeef' WHERE id = 120")
def attack_delete_middle(c): c.execute("DELETE FROM audit_logs WHERE id = 140")
def attack_delete_tail(c): c.execute("DELETE FROM audit_logs WHERE id > ?", (BASE_EVENTS - 5,))
def attack_delete_head(c): c.execute("DELETE FROM audit_logs WHERE id <= 5")


def attack_reorder(c):
    c.execute("UPDATE audit_logs SET id = -1 WHERE id = 100")
    c.execute("UPDATE audit_logs SET id = 100 WHERE id = 101")
    c.execute("UPDATE audit_logs SET id = 101 WHERE id = -1")


def attack_forge_unhashed(c):
    c.execute("INSERT INTO audit_logs (user_id, role, execution_status, natural_language_query) VALUES ('mallory', 'admin', 'hitl_approved', 'forged')")


def attack_forge_bad_hash(c):
    c.execute("INSERT INTO audit_logs (user_id, role, execution_status, prev_hash, row_hash) VALUES ('mallory', 'admin', 'hitl_approved', 'x', 'y')")


def attack_duplicate_row(c):
    c.execute("INSERT INTO audit_logs (run_id, user_id, role, natural_language_query, generated_sql, confidence_score, execution_status, ingress_channel, "
              "client_agent, risk_level, approved_by, execution_time_ms, masked_rows_count, timestamp, prev_hash, row_hash) "
              "SELECT run_id, user_id, role, natural_language_query, generated_sql, confidence_score, execution_status, ingress_channel, client_agent, "
              "risk_level, approved_by, execution_time_ms, masked_rows_count, timestamp, prev_hash, row_hash FROM audit_logs WHERE id = 50")


def attack_rewrite_chain(c):
    """The attacker knows the hashing scheme: alter a row and recompute every hash after it."""
    from app.utils.audit_logger import _compute_hash
    c.row_factory = sqlite3.Row
    prev = "GENESIS"
    for row in c.execute("SELECT * FROM audit_logs ORDER BY id").fetchall():
        data = dict(row)
        if data["id"] == 150:
            data["generated_sql"] = "SELECT 1"
        new_hash = _compute_hash(prev, data)
        c.execute("UPDATE audit_logs SET generated_sql = ?, prev_hash = ?, row_hash = ? WHERE id = ?", (data["generated_sql"], prev, new_hash, data["id"]))
        prev = new_hash


ATTACKS: List[Dict[str, Any]] = [
    {"id": "edit_sql_of_a_row", "fn": attack_edit_sql, "what": "change the recorded SQL of one event"},
    {"id": "edit_approver", "fn": attack_edit_approver, "what": "change who approved a query"},
    {"id": "edit_timestamp", "fn": attack_edit_timestamp, "what": "back-date an event"},
    {"id": "overwrite_hash", "fn": attack_garbage_hash, "what": "overwrite a stored hash"},
    {"id": "delete_middle_row", "fn": attack_delete_middle, "what": "remove one event from the middle"},
    {"id": "reorder_two_rows", "fn": attack_reorder, "what": "swap the order of two events"},
    {"id": "duplicate_a_row", "fn": attack_duplicate_row, "what": "replay an old event as a new one"},
    {"id": "forge_row_without_hash", "fn": attack_forge_unhashed, "what": "insert an event with no hash"},
    {"id": "forge_row_with_bad_hash", "fn": attack_forge_bad_hash, "what": "insert an event with an invented hash"},
    {"id": "delete_newest_rows", "fn": attack_delete_tail, "what": "truncate the most recent events"},
    {"id": "delete_oldest_rows", "fn": attack_delete_head, "what": "remove the oldest events"},
    {"id": "rewrite_whole_chain", "fn": attack_rewrite_chain, "what": "alter an event and recompute every hash after it"},
]


def run(llm: bool = True, limit=None) -> Dict[str, Any]:
    from app.utils import audit_logger
    from app.utils.audit_logger import audit_anchor, log_audit_event, verify_audit_chain

    ensure_dirs()
    folder = os.path.join(WORK_DIR, "audit")
    shutil.rmtree(folder, ignore_errors=True)
    os.makedirs(folder)
    saved_env = os.environ.get("AUDIT_DB_PATH")
    audit_logger._INITIALIZED_PATHS.clear()

    try:
        base = os.path.join(folder, "base.db")
        os.environ["AUDIT_DB_PATH"] = base
        for i in range(BASE_EVENTS):
            log_audit_event(user_id=f"user-{i % 7}", role="employee", user_query=f"question {i}", generated_sql=f"SELECT {i}",
                            confidence_score=90, execution_status="completed", approved_by="admin-1" if i % 10 == 0 else None,
                            execution_time_ms=1.5, masked_rows_count=i % 5)
        anchor = audit_anchor()
        assert verify_audit_chain(anchor=anchor)["valid"], "baseline log must verify"

        matrix = []
        for attack in ATTACKS:
            target = os.path.join(folder, f"{attack['id']}.db")
            shutil.copy(base, target)
            conn = sqlite3.connect(target)
            attack["fn"](conn)
            conn.commit()
            prev_ok = previous_verifier(conn)
            conn.close()
            os.environ["AUDIT_DB_PATH"] = target
            current = verify_audit_chain()
            anchored = verify_audit_chain(anchor=anchor)
            matrix.append({
                "attack": attack["id"], "what": attack["what"],
                "detected_previous": not prev_ok,
                "detected_current": not current["valid"],
                "detected_with_anchor": not anchored["valid"],
                "current_reason": current["reason"], "anchored_reason": anchored["reason"],
            })
        os.environ["AUDIT_DB_PATH"] = base

        n = len(matrix)
        summary = {k: proportion(sum(m[k] for m in matrix), n) for k in ("detected_previous", "detected_current", "detected_with_anchor")}

        # ---------------------------------------------------------------- performance
        perf_db = os.path.join(folder, "perf.db")
        os.environ["AUDIT_DB_PATH"] = perf_db
        write_ms = []
        for i in range(2000):
            t = time.perf_counter()
            log_audit_event(user_id="perf", role="employee", user_query=f"q{i}", generated_sql="SELECT 1", execution_status="completed")
            write_ms.append((time.perf_counter() - t) * 1000)
        for i in range(8000):                       # grow to 10,000 rows
            log_audit_event(user_id="perf", role="employee", user_query=f"q{i}", generated_sql="SELECT 1", execution_status="completed")
        t = time.perf_counter()
        verified = verify_audit_chain()
        verify_ms = (time.perf_counter() - t) * 1000
        performance = {
            "write_latency": latency_summary(write_ms),
            "writes_per_second_sequential": round(1000 / (sum(write_ms) / len(write_ms)), 1),
            "verify_rows": verified["checked"], "verify_valid": verified["valid"], "verify_total_ms": round(verify_ms, 1),
            "verify_rows_per_second": round(verified["checked"] / (verify_ms / 1000)),
        }
    finally:
        if saved_env is None:
            os.environ.pop("AUDIT_DB_PATH", None)
        else:
            os.environ["AUDIT_DB_PATH"] = saved_env
        audit_logger._INITIALIZED_PATHS.clear()

    result = {
        "meta": run_metadata([], {"base_events": BASE_EVENTS, "attacks": n}),
        "summary": summary,
        "matrix": matrix,
        "undetectable_without_anchor": [m["attack"] for m in matrix if not m["detected_current"]],
        "undetectable_even_with_anchor": [m["attack"] for m in matrix if not m["detected_with_anchor"]],
        "performance": performance,
    }
    print("saved", save_result("audit", result))
    return result
