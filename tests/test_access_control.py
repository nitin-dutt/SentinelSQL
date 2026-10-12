"""
Regression tests for alignment_plan G1 (SEC-01 .. SEC-07): authentication, role
checks, HITL separation of duties, SQL re-validation on approval, run ownership,
MCP client identity and audit-log integrity.
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch

from app.main import app
from app.hitl.web_store import save_run, get_run
from app.utils.audit_logger import get_audit_logs, clear_audit_logs, verify_audit_chain, log_audit_event
from tests.helpers import ADMIN, ADMIN2, EMPLOYEE, EMPLOYEE2, MCP, MCP2, bearer

client = TestClient(app)


def pending_run(submitter="emp-1", sql="SELECT employee_name, salary FROM salaries LIMIT 5"):
    return save_run({
        "user_query": "show salaries",
        "sql": sql,
        "user_id": submitter,
        "role": "employee",
    })


# ---------------------------------------------------------------- SEC-03: authentication

def test_requests_without_a_token_are_rejected():
    for method, path in [("get", "/schema"), ("get", "/dictionary"), ("get", "/admin/queue"),
                         ("get", "/admin/audit-logs"), ("post", "/hitl/decision"), ("get", "/auth/me")]:
        assert getattr(client, method)(path).status_code == 401, path


def test_forged_role_header_grants_nothing():
    forged = {"X-Role": "admin", "X-User-Id": "root"}
    assert client.get("/admin/queue", headers=forged).status_code == 401
    assert client.get("/admin/audit-logs", headers=forged).status_code == 401
    # ...and cannot upgrade a legitimate employee either
    assert client.get("/admin/queue", headers={**EMPLOYEE, **forged}).status_code == 403


def test_unknown_token_is_rejected():
    assert client.get("/auth/me", headers=bearer("not-a-real-token")).status_code == 401


def test_whoami_reports_token_identity():
    assert client.get("/auth/me", headers=ADMIN).json() == {"user_id": "admin-1", "role": "admin"}
    assert client.get("/auth/me", headers=EMPLOYEE).json() == {"user_id": "emp-1", "role": "employee"}


def test_no_tokens_configured_fails_closed(monkeypatch):
    monkeypatch.setenv("AUTH_TOKENS", "")
    assert client.get("/auth/me", headers=ADMIN).status_code == 401


def test_malformed_token_entries_are_ignored(monkeypatch):
    monkeypatch.setenv("AUTH_TOKENS", "badentry,tok:user:superuser,good:bob:admin")
    assert client.get("/auth/me", headers=bearer("good")).json()["user_id"] == "bob"
    assert client.get("/auth/me", headers=bearer("tok")).status_code == 401


def test_every_admin_route_requires_admin_role():
    admin_routes = [
        ("get", "/admin/queue"), ("get", "/admin/audit-logs"), ("delete", "/admin/audit-logs"),
        ("get", "/admin/audit-logs/verify"), ("get", "/admin/cache/stats"), ("delete", "/admin/cache"),
        ("get", "/admin/golden-queries"), ("delete", "/admin/golden-queries/1"),
        ("get", "/admin/dictionary"), ("delete", "/admin/dictionary/1"),
    ]
    for method, path in admin_routes:
        assert getattr(client, method)(path, headers=EMPLOYEE).status_code == 403, path
        assert getattr(client, method)(path, headers=MCP).status_code == 403, path


def test_query_endpoint_rejects_mcp_role():
    assert client.post("/query", json={"query": "hi"}, headers=MCP).status_code == 403


# ---------------------------------------------------------------- SEC-01 / SEC-02 / SEC-04: HITL

def test_employee_cannot_decide_hitl_runs():
    run_id = pending_run()
    for headers in (EMPLOYEE, MCP):
        r = client.post("/hitl/decision", json={"run_id": run_id, "decision": "approve"}, headers=headers)
        assert r.status_code == 403
    assert get_run(run_id)["status"] == "pending"


def test_submitter_cannot_approve_own_run():
    run_id = pending_run(submitter="admin-1")
    r = client.post("/hitl/decision", json={"run_id": run_id, "decision": "approve"}, headers=ADMIN)
    assert r.status_code == 403
    assert get_run(run_id)["status"] == "pending"
    assert any(l["execution_status"] == "hitl_self_approval_blocked" and l["run_id"] == run_id
               for l in get_audit_logs(limit=50))


def test_self_approval_can_be_enabled_explicitly(monkeypatch):
    monkeypatch.setenv("HITL_ALLOW_SELF_APPROVAL", "true")
    run_id = pending_run(submitter="admin-1")
    r = client.post("/hitl/decision", json={"run_id": run_id, "decision": "approve"}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["status"] == "completed"


def test_different_admin_can_approve_and_is_recorded():
    run_id = pending_run(submitter="emp-1")
    r = client.post("/hitl/decision", json={"run_id": run_id, "decision": "approve"}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["status"] == "completed"
    row = next(l for l in get_audit_logs(limit=50) if l["run_id"] == run_id and l["execution_status"] == "hitl_approved")
    assert row["approved_by"] == "admin-1"
    assert row["user_id"] == "emp-1"


def test_run_cannot_be_decided_twice():
    run_id = pending_run()
    first = client.post("/hitl/decision", json={"run_id": run_id, "decision": "approve"}, headers=ADMIN)
    assert first.json()["status"] == "completed"
    second = client.post("/hitl/decision", json={"run_id": run_id, "decision": "approve"}, headers=ADMIN2)
    assert second.json()["status"] == "rejected"
    assert "already decided" in second.json()["reason"]


@pytest.mark.parametrize("bad_sql", [
    "DROP TABLE customers",
    "SELECT 1; DROP TABLE customers",
    "DELETE FROM orders",
    "SELECT * FROM customers, orders JOIN products",
    "WITH x AS (DELETE FROM orders RETURNING *) SELECT * FROM x",
])
def test_edited_sql_still_goes_through_the_ast_gate(bad_sql):
    run_id = pending_run()
    with patch("app.routers.hitl.execute_readonly_query") as mock_exec:
        r = client.post("/hitl/decision",
                        json={"run_id": run_id, "decision": "edit", "edited_sql": bad_sql}, headers=ADMIN)
    assert r.json()["status"] == "rejected"
    assert "policy" in r.json()["reason"].lower()
    mock_exec.assert_not_called()


def test_edited_sql_gets_a_limit_injected():
    run_id = pending_run()
    with patch("app.routers.hitl.execute_readonly_query", return_value={"columns": ["name"], "rows": []}) as mock_exec:
        r = client.post("/hitl/decision",
                        json={"run_id": run_id, "decision": "edit",
                              "edited_sql": "SELECT name FROM customers"}, headers=ADMIN)
    assert r.json()["status"] == "completed"
    executed = mock_exec.call_args[0][0]
    assert "LIMIT" in executed.upper()


def test_edited_salary_query_with_alias_is_still_masked_end_to_end():
    """The original exploit: edit to `salary AS pay`, expect no raw salaries."""
    run_id = pending_run()
    r = client.post("/hitl/decision",
                    json={"run_id": run_id, "decision": "edit",
                          "edited_sql": "SELECT employee_name, salary AS pay FROM salaries"}, headers=ADMIN)
    body = r.json()
    assert body["status"] == "completed"
    assert len(body["results"]["rows"]) <= 100
    assert all(row["pay"] == "[CONFIDENTIAL]" for row in body["results"]["rows"])


def test_invalid_sql_is_never_saved_as_golden_template():
    from app.rag.golden_store import get_all_golden_queries
    run_id = pending_run()
    marker = "poisoning attempt xyz"
    state = get_run(run_id)
    state["user_query"] = marker
    client.post("/hitl/decision",
                json={"run_id": run_id, "decision": "edit", "edited_sql": "DROP TABLE customers",
                      "save_as_template": True}, headers=ADMIN)
    assert not any(t["natural_language_query"] == marker for t in get_all_golden_queries())


def test_non_admin_cannot_save_templates():
    run_id = pending_run()
    r = client.post("/hitl/decision", json={"run_id": run_id, "decision": "approve",
                                            "save_as_template": True}, headers=EMPLOYEE)
    assert r.status_code == 403


# ---------------------------------------------------------------- SEC-05: run ownership

def test_run_status_visible_only_to_owner_and_admin():
    run_id = pending_run(submitter="emp-1")
    assert client.get(f"/query/status/{run_id}", headers=EMPLOYEE).status_code == 200
    assert client.get(f"/query/status/{run_id}", headers=ADMIN).status_code == 200
    assert client.get(f"/query/status/{run_id}", headers=EMPLOYEE2).status_code == 404
    assert client.get(f"/query/status/{run_id}").status_code == 401


# ---------------------------------------------------------------- SEC-06: MCP identity

def test_mcp_bridge_requires_a_token_and_the_right_role():
    body = {"sql": "SELECT name FROM customers LIMIT 1", "rationale": "x"}
    assert client.post("/mcp/submit", json=body).status_code == 401
    assert client.post("/mcp/submit", json=body, headers=EMPLOYEE).status_code == 403
    assert client.get("/mcp/schema").status_code == 401


def test_mcp_audit_rows_carry_the_real_client_identity():
    r = client.post("/mcp/submit", json={"sql": "SELECT name FROM customers LIMIT 1", "rationale": "ident"}, headers=MCP)
    assert r.json()["status"] == "completed"
    row = next(l for l in get_audit_logs(limit=20) if l["ingress_channel"] == "MCP_AGENT" and "ident" in (l["natural_language_query"] or ""))
    assert row["user_id"] == "mcp-client-1"
    assert row["role"] == "external_ai"


def test_mcp_client_cannot_poll_another_clients_run():
    r = client.post("/mcp/submit", json={"sql": "SELECT employee_name, salary FROM salaries LIMIT 3", "rationale": "mine"}, headers=MCP)
    run_id = r.json()["run_id"]
    assert client.get(f"/mcp/status/{run_id}", headers=MCP).json()["status"] == "pending"
    assert client.get(f"/mcp/status/{run_id}", headers=MCP2).json()["status"] == "not_found"
    assert client.get(f"/mcp/status/{run_id}", headers=ADMIN).json()["status"] == "pending"


# ---------------------------------------------------------------- SEC-07: audit integrity

@pytest.fixture
def fresh_audit(tmp_path, monkeypatch):
    path = tmp_path / "audit.db"
    monkeypatch.setenv("AUDIT_DB_PATH", str(path))
    return path


def test_audit_chain_verifies_and_detects_edits(fresh_audit):
    for i in range(4):
        log_audit_event(user_id=f"u{i}", user_query=f"q{i}", generated_sql="SELECT 1", execution_status="completed")
    ok = verify_audit_chain()
    assert (ok["valid"], ok["checked"], ok["first_invalid_id"], ok["reason"]) == (True, 4, None, None) and ok["head_hash"]

    conn = sqlite3.connect(fresh_audit)
    conn.execute("UPDATE audit_logs SET generated_sql = 'SELECT secret' WHERE id = 2")
    conn.commit()
    conn.close()
    result = verify_audit_chain()
    assert result["valid"] is False and result["first_invalid_id"] == 2


def test_audit_chain_detects_deleted_rows(fresh_audit):
    for i in range(4):
        log_audit_event(user_id=f"u{i}", execution_status="completed")
    conn = sqlite3.connect(fresh_audit)
    conn.execute("DELETE FROM audit_logs WHERE id = 2")
    conn.commit()
    conn.close()
    result = verify_audit_chain()
    assert result["valid"] is False and result["first_invalid_id"] == 3


def test_clearing_the_audit_log_is_itself_recorded(fresh_audit):
    for i in range(3):
        log_audit_event(user_id=f"u{i}", execution_status="completed")
    assert clear_audit_logs(cleared_by="admin-1") == 3
    rows = get_audit_logs(limit=10)
    assert len(rows) == 1
    assert rows[0]["execution_status"] == "audit_logs_cleared"
    assert rows[0]["user_id"] == "admin-1" and rows[0]["masked_rows_count"] == 3
    assert verify_audit_chain()["valid"] is True
    # ids keep counting up: the sequence is not reset
    log_audit_event(user_id="after", execution_status="completed")
    assert max(r["id"] for r in get_audit_logs(limit=10)) > rows[0]["id"]


def test_clear_endpoint_records_the_actor():
    r = client.delete("/admin/audit-logs", headers=ADMIN)
    assert r.status_code == 200
    assert get_audit_logs(limit=1)[0]["execution_status"] == "audit_logs_cleared"
    assert get_audit_logs(limit=1)[0]["user_id"] == "admin-1"
    assert client.get("/admin/audit-logs/verify", headers=ADMIN).json()["valid"] is True


# ---------------------------------------------------------------- audit anchoring and forged rows (BEN audit suite findings)

def test_an_unhashed_row_forged_after_the_chain_started_is_detected(fresh_audit):
    for i in range(5):
        log_audit_event(user_id=f"u{i}", execution_status="completed")
    conn = sqlite3.connect(fresh_audit)
    conn.execute("INSERT INTO audit_logs (user_id, role, execution_status) VALUES ('mallory', 'admin', 'hitl_approved')")
    conn.commit()
    conn.close()
    result = verify_audit_chain()
    assert result["valid"] is False and "unhashed row" in result["reason"]


def test_legacy_rows_before_the_chain_are_still_tolerated(fresh_audit):
    from app.utils.audit_logger import _connect
    conn = _connect()
    conn.execute("INSERT INTO audit_logs (user_id, execution_status) VALUES ('legacy', 'completed')")
    conn.close()
    for i in range(3):
        log_audit_event(user_id=f"u{i}", execution_status="completed")
    assert verify_audit_chain()["valid"] is True


def test_anchor_detects_tail_truncation_head_truncation_and_full_rewrites(fresh_audit):
    from app.utils.audit_logger import audit_anchor, _compute_hash, _HASHED_FIELDS
    for i in range(6):
        log_audit_event(user_id=f"u{i}", user_query=f"q{i}", execution_status="completed")
    anchor = audit_anchor()
    assert anchor["count"] == 6 and verify_audit_chain(anchor=anchor)["valid"]
    log_audit_event(user_id="later", execution_status="completed")
    assert verify_audit_chain(anchor=anchor)["valid"]                        # growth after the anchor is fine

    def copy_db(tag):
        import shutil
        target = fresh_audit.parent / f"{tag}.db"
        shutil.copy(fresh_audit, target)
        return target

    import os
    original = os.environ["AUDIT_DB_PATH"]
    try:
        for tag, sql, why in [
            ("tail", "DELETE FROM audit_logs WHERE id >= 6", "anchored entry is missing"),
            ("head", "DELETE FROM audit_logs WHERE id <= 2", "moved"),
        ]:
            target = copy_db(tag)
            c = sqlite3.connect(target); c.execute(sql); c.commit(); c.close()
            os.environ["AUDIT_DB_PATH"] = str(target)
            result = verify_audit_chain(anchor=anchor)
            assert result["valid"] is False and why in result["reason"], (tag, result)

        # an attacker who recomputes the whole chain: internally consistent, but different hashes
        target = copy_db("rewrite")
        c = sqlite3.connect(target); c.row_factory = sqlite3.Row
        prev = "GENESIS"
        for row in c.execute("SELECT * FROM audit_logs ORDER BY id").fetchall():
            data = dict(row)
            data["natural_language_query"] = "rewritten"
            new_hash = _compute_hash(prev, data)
            c.execute("UPDATE audit_logs SET natural_language_query='rewritten', prev_hash=?, row_hash=? WHERE id=?", (prev, new_hash, data["id"]))
            prev = new_hash
        c.commit(); c.close()
        os.environ["AUDIT_DB_PATH"] = str(target)
        assert verify_audit_chain()["valid"] is True                       # undetectable from the database alone...
        assert verify_audit_chain(anchor=anchor)["valid"] is False         # ...but not against an external anchor
    finally:
        os.environ["AUDIT_DB_PATH"] = original


def test_anchor_endpoints():
    body = client.get("/admin/audit-logs/anchor", headers=ADMIN).json()
    assert {"head_hash", "count", "valid_when_taken"} <= set(body)
    ok = client.get("/admin/audit-logs/verify", params={"anchor_head": body["head_hash"], "anchor_count": body["count"]}, headers=ADMIN).json()
    assert ok["valid"] is True
    bad = client.get("/admin/audit-logs/verify", params={"anchor_head": "deadbeef", "anchor_count": 1}, headers=ADMIN).json()
    assert bad["valid"] is False
    assert client.get("/admin/audit-logs/anchor", headers=EMPLOYEE).status_code == 403
