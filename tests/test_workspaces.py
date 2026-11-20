"""
The launch page's sessions (app/workspaces.py): entering without a token in dev, and a visitor's own database as a
private workspace. A real PostgreSQL/MySQL server is not needed here: the URL check and the engine factory are replaced
so the "visitor's database" is a small SQLite file, and everything after that (scoping, isolation, quota) is the real code.
"""

import os
import sqlite3

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from app import demo, workspaces
from app.main import app
from tests.helpers import ADMIN, EMPLOYEE, bearer, pipeline_writing, use_pipeline

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    for name in ("APP_ENV", "DEMO_MODE", "WORKSPACES_ENABLED", "WORKSPACE_ALLOW_PRIVATE_HOSTS", "WORKSPACE_QUESTION_QUOTA"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "workspaces"))
    workspaces.reset()
    yield
    workspaces.reset()


@pytest.fixture
def visitor_db(monkeypatch, tmp_path):
    """A visitor's database with one table the sample database does not have, reachable through /launch/connect."""
    path = str(tmp_path / "visitor.db").replace(os.sep, "/")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT)")
    conn.executemany("INSERT INTO widgets (name) VALUES (?)", [("sprocket",), ("flange",)])
    conn.commit()
    conn.close()
    monkeypatch.setenv("WORKSPACES_ENABLED", "true")
    monkeypatch.setattr(workspaces, "_checked_url", lambda raw: (make_url(f"sqlite:///{path}"), "postgresql", None))
    monkeypatch.setattr(workspaces, "_engine_for", lambda url, backend, pinned: create_engine(url))
    return path


def connect():
    res = client.post("/launch/connect", json={"url": "postgresql://visitor:secret@db.example.com/shop"})
    assert res.status_code == 200, res.text
    return res.json()


# ---------------------------------------------------------------- entering without a token

def test_a_standard_server_offers_nothing_without_a_token():
    info = client.get("/launch/info").json()
    assert info["mode"] == "standard" and info["sample"]["one_click"] is False and info["connect"]["enabled"] is False
    assert client.post("/launch/sample").status_code == 403
    assert client.post("/launch/connect", json={"url": "postgresql://u:p@example.com/db"}).status_code == 403


def test_dev_mode_enters_without_a_token_and_only_while_it_is_on(monkeypatch):
    monkeypatch.setenv("APP_ENV", "dev")
    assert client.get("/launch/info").json()["mode"] == "dev"
    tokens = client.post("/launch/sample").json()["tokens"]
    assert set(tokens) == {"employee", "admin", "external_ai"}
    assert client.get("/auth/me", headers=bearer(tokens["admin"])).json() == {"user_id": "dev-reviewer", "role": "admin"}
    assert client.post("/launch/sample").json()["tokens"] == tokens          # the same logins on every click
    monkeypatch.delenv("APP_ENV")
    assert client.get("/auth/me", headers=bearer(tokens["admin"])).status_code == 401


def test_demo_mode_hands_out_the_shared_demo_logins(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("DEMO_PUBLIC_USERS", "emp-1,admin-1")
    demo.budget.reset()
    info = client.get("/launch/info").json()
    assert info["mode"] == "demo" and info["limits"]["daily_cap"] > 0 and "token" not in str(info).lower()
    assert client.post("/launch/sample").json()["tokens"] == {"employee": "test-employee-token", "admin": "test-admin-token"}


# ---------------------------------------------------------------- what a visitor's URL may be

@pytest.mark.parametrize("url, expected", [
    ("sqlite:///somewhere.db", "Only PostgreSQL and MySQL"),
    ("not a url", "not a database URL"),
    ("postgresql://host/db", "needs a user"),
    ("postgresql://u:p@example.com/db?sslrootcert=/etc/passwd", "option not allowed"),
    ("mysql://u:p@example.com/db?unix_socket=/var/run/mysqld.sock", "option not allowed"),
    ("postgresql://u:p@127.0.0.1:5432/db", "reachable from the internet"),
    ("mysql://u:p@localhost/db", "reachable from the internet"),
    ("postgresql://u:p@10.0.0.5/db", "reachable from the internet"),
    ("postgresql://u:p@169.254.169.254/db", "reachable from the internet"),
])
def test_unsafe_urls_are_refused_before_any_connection(monkeypatch, url, expected):
    monkeypatch.setenv("WORKSPACES_ENABLED", "true")
    res = client.post("/launch/connect", json={"url": url})
    assert res.status_code == 422 and expected in res.json()["detail"]


def test_private_hosts_are_allowed_in_dev_only(monkeypatch):
    monkeypatch.setenv("APP_ENV", "dev")
    url, backend, pinned = workspaces._checked_url("postgresql://u:p@localhost:5432/db")
    assert backend == "postgresql" and pinned is None


def test_connection_errors_never_show_the_password():
    assert "hunter2" not in workspaces._safe_error(RuntimeError("auth failed for postgresql://u:hunter2@h/db"), "hunter2")
    assert "s3cr3t-long-pass" not in workspaces._safe_error(RuntimeError("bad password s3cr3t-long-pass given"), "s3cr3t-long-pass")
    # a one-letter password must not garble the message (it used to turn "port" into "***ort")
    assert workspaces._safe_error(RuntimeError("port 5432 refused"), "p") == "port 5432 refused"


# ---------------------------------------------------------------- a workspace is its own world

def test_a_workspace_sees_only_its_own_database(visitor_db):
    out = connect()
    assert out["workspace"]["tables"] == 1 and "secret" not in str(out)
    tester, reviewer = bearer(out["tokens"]["employee"]), bearer(out["tokens"]["admin"])

    assert client.get("/auth/me", headers=tester).json() == {"user_id": "tester", "role": "employee"}
    assert [t["name"] for t in client.get("/schema/overview", headers=tester).json()["tables"]] == ["widgets"]
    sample = {t["name"] for t in client.get("/schema/overview", headers=EMPLOYEE).json()["tables"]}
    assert "customers" in sample and "widgets" not in sample

    with use_pipeline(pipeline_writing("SELECT name FROM widgets ORDER BY name")):
        mine = client.post("/query", json={"query": "list the widgets"}, headers=tester).json()
        theirs = client.post("/query", json={"query": "list the widgets please"}, headers=EMPLOYEE).json()
    assert mine["status"] == "completed" and [r["name"] for r in mine["results"]["rows"]] == ["flange", "sprocket"]
    assert theirs["status"] != "completed"                       # the sample database has no such table

    # the audit log, like the review queue, traces and cache, is the workspace's own file
    mine_log = client.get("/admin/audit-logs", headers=reviewer).json()
    assert {row["user_id"] for row in mine_log} == {"tester"}
    assert all(row["user_id"] != "tester" for row in client.get("/admin/audit-logs?limit=200", headers=ADMIN).json())
    assert client.get("/launch/session", headers=tester).json()["workspace"]["questions_used"] == 1


def test_the_answer_cache_does_not_cross_between_databases(visitor_db):
    tester = bearer(connect()["tokens"]["employee"])
    question = "how many things are there in total"
    with use_pipeline(pipeline_writing("SELECT COUNT(*) AS n FROM widgets")):
        assert client.post("/query", json={"query": question}, headers=tester).json()["results"]["rows"] == [{"n": 2}]
    with use_pipeline(pipeline_writing("SELECT COUNT(*) AS n FROM customers")):
        sample = client.post("/query", json={"query": question}, headers=EMPLOYEE).json()
    assert sample["status"] == "completed" and not sample.get("cached") and sample["results"]["rows"] != [{"n": 2}]


def test_the_question_quota_is_enforced_per_workspace(visitor_db, monkeypatch):
    monkeypatch.setenv("WORKSPACE_QUESTION_QUOTA", "2")
    tester = bearer(connect()["tokens"]["employee"])
    with use_pipeline(pipeline_writing("SELECT name FROM widgets")):
        codes = [client.post("/query", json={"query": f"widgets {i}"}, headers=tester).status_code for i in range(3)]
        assert client.post("/query", json={"query": "customers"}, headers=EMPLOYEE).status_code == 200
    assert codes == [200, 200, 429]


def test_disconnecting_ends_the_tokens_and_deletes_the_files(visitor_db):
    out = connect()
    tester = bearer(out["tokens"]["employee"])
    folder = workspaces._workspaces[out["workspace"]["id"]].directory
    with use_pipeline(pipeline_writing("SELECT name FROM widgets")):
        client.post("/query", json={"query": "widgets"}, headers=tester)
    assert os.path.isdir(folder)
    assert client.delete("/launch/workspace", headers=tester).json() == {"status": "closed"}
    assert client.get("/auth/me", headers=tester).status_code == 401 and not os.path.exists(folder)
    assert client.delete("/launch/workspace", headers=EMPLOYEE).status_code == 404


def test_idle_workspaces_are_closed_and_capacity_is_limited(visitor_db, monkeypatch):
    monkeypatch.setenv("WORKSPACE_MAX_ACTIVE", "1")
    first = connect()
    assert client.post("/launch/connect", json={"url": "postgresql://v:s@db.example.com/shop"}).status_code == 503
    workspaces._workspaces[first["workspace"]["id"]].last_used -= 10 ** 6       # long idle: swept on the next connect
    second = connect()
    assert client.get("/auth/me", headers=bearer(first["tokens"]["employee"])).status_code == 401
    assert client.get("/auth/me", headers=bearer(second["tokens"]["employee"])).status_code == 200
