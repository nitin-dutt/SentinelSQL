"""
Live PostgreSQL and MySQL checks (the parked PGM track). They run only when a server is reachable:

    docker run -d --name sentinel-test-pg    -e POSTGRES_USER=sentinel -e POSTGRES_PASSWORD=pw -e POSTGRES_DB=demo -p 55432:5432 postgres:16
    docker run -d --name sentinel-test-mysql -e MYSQL_ROOT_PASSWORD=pw -e MYSQL_DATABASE=demo -e MYSQL_USER=sentinel -e MYSQL_PASSWORD=pw -p 33307:3306 mysql:8
    pytest -m pg_mysql

Override the connection URLs with TEST_POSTGRES_URL / TEST_MYSQL_URL (and TEST_MYSQL_ROOT_URL for the grant checks).
Without a reachable server every test here is skipped, so a plain `pytest` run is unaffected.
"""

import json
import os
import time

import pytest
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.pg_mysql

URLS = {
    "postgres": os.getenv("TEST_POSTGRES_URL", "postgresql://sentinel:pw@localhost:55432/demo"),
    "mysql": os.getenv("TEST_MYSQL_URL", "mysql://sentinel:pw@localhost:33307/demo"),
}
ROOT_URLS = {
    "postgres": os.getenv("TEST_POSTGRES_URL", URLS["postgres"]),
    "mysql": os.getenv("TEST_MYSQL_ROOT_URL", "mysql://root:pw@localhost:33307/demo"),
}


def _reachable(url: str) -> bool:
    from app.db.urls import normalize_database_url
    try:
        eng = create_engine(normalize_database_url(url), connect_args={"connect_timeout": 3} if "postgres" in url else {"connection_timeout": 3})
        with eng.connect() as c:
            c.execute(text("SELECT 1"))
        eng.dispose()
        return True
    except Exception:
        return False


def _with_credentials(server: str, user: str, password: str) -> str:
    """The same host, port and database as the configured test server, with other credentials (works in CI, where the ports differ)."""
    from sqlalchemy.engine import make_url
    from app.db.urls import normalize_database_url
    return make_url(normalize_database_url(URLS[server])).set(username=user, password=password).render_as_string(hide_password=False)


@pytest.fixture(scope="module", params=["postgres", "mysql"])
def server(request):
    name = request.param
    if not _reachable(URLS[name]):
        pytest.skip(f"no {name} server reachable (see the module docstring to start one)")
    return name


@pytest.fixture(scope="module")
def clean_test_database(server):
    """Drops every table in the throwaway test database so each run starts from nothing. Refuses any other database name."""
    from sqlalchemy import MetaData
    from sqlalchemy.engine import make_url
    from app.db.urls import normalize_database_url
    name = make_url(normalize_database_url(URLS[server])).database or ""
    assert name == "demo" or name.endswith("_test"), f"refusing to drop tables in database {name!r}; use a throwaway database named 'demo' or '*_test'"
    eng = create_engine(normalize_database_url(URLS[server]))
    meta = MetaData()
    meta.reflect(bind=eng)
    if server == "mysql":
        with eng.begin() as c:
            c.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
            meta.drop_all(c)
            c.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
    else:
        meta.drop_all(eng)
    eng.dispose()


@pytest.fixture
def live(server, clean_test_database, tmp_path, monkeypatch):
    """The application pointed at the live database through a single DATABASE_URL, seeded with the demo data."""
    for k in ("ACTIVE_DB", "DATABASE_URL_SQLITE", "DATABASE_URL_POSTGRES", "DATABASE_URL_MYSQL", "READONLY_DATABASE_URL"):
        monkeypatch.setenv(k, "")
    monkeypatch.setenv("DATABASE_URL", URLS[server])
    monkeypatch.setenv("SEED_DEMO_DATA", "true")
    monkeypatch.setenv("QUERY_TIMEOUT", "2")
    monkeypatch.setenv("DATA_VERSION_CHECK_SECONDS", "0")
    monkeypatch.setenv("DICTIONARY_DB_PATH", str(tmp_path / "dict.db"))
    monkeypatch.setenv("GOLDEN_DB_PATH", str(tmp_path / "golden.db"))
    from app.db import database as db
    from app.utils.schemacontext import clear_schema_cache
    db.reset_engines()
    clear_schema_cache()
    db.init_db()
    yield db
    db.reset_engines()
    clear_schema_cache()


# ---------------------------------------------------------------- connection and configuration (PGM-07)

def test_one_url_selects_the_engine_and_the_dialect(live, server):
    assert live.get_engine().dialect.name == {"postgres": "postgresql", "mysql": "mysql"}[server]
    assert live.sql_dialect() == server


def test_a_wrong_password_fails_clearly_and_never_falls_back_to_sqlite(server, monkeypatch):
    bad = URLS[server].replace(":pw@", ":wrong-password@")
    for k in ("ACTIVE_DB", "DATABASE_URL_SQLITE", "DATABASE_URL_POSTGRES", "DATABASE_URL_MYSQL", "READONLY_DATABASE_URL", "DB_FALLBACK_TO_SQLITE"):
        monkeypatch.setenv(k, "")
    monkeypatch.setenv("DATABASE_URL", bad)
    from app.db import database as db
    db.reset_engines()
    try:
        with pytest.raises(Exception) as err:
            db.check_connection()
        assert "sqlite" not in str(err.value).lower()
        assert db.get_engine().dialect.name != "sqlite"
    finally:
        db.reset_engines()


# ---------------------------------------------------------------- seeding and schema (PGM-03, PGM-05)

def test_demo_seed_creates_the_same_21_tables_and_is_idempotent(live):
    from sqlalchemy import inspect
    expected = {"customers": 4, "orders": 5, "products": 4, "salaries": 3}
    for _ in range(2):                                    # the second pass proves nothing is duplicated
        live.init_db()
        with live.get_engine().connect() as c:
            assert len(inspect(c).get_table_names()) >= 21
            for table, n in expected.items():
                assert c.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() == n
    assert live.demo_seed_health() == {}


def test_introspection_finds_tables_columns_and_foreign_keys_and_hides_system_schemas(live):
    from app.utils.schemacontext import introspect_schema
    schema = introspect_schema(force_refresh=True)
    assert len(schema["tables"]) >= 21 and len(schema["relationships"]) >= 18
    assert "orders.customer_id -> customers.customer_id" in schema["relationships"]
    for system in ("information_schema", "pg_catalog", "mysql", "sys", "performance_schema"):
        assert system not in schema["tables"]


# ---------------------------------------------------------------- read-only and timeouts (PGM-01, PGM-02)

def test_the_read_only_connection_refuses_every_kind_of_write(live):
    for stmt in ("DELETE FROM customers", "UPDATE customers SET name = 'x'", "INSERT INTO customers (name, email, region) VALUES ('a','b','c')",
                 "DROP TABLE customers", "CREATE TABLE evil (a int)"):
        with pytest.raises(Exception):
            with live.get_readonly_engine().connect() as c:
                c.execute(text(stmt))
                c.commit()
    with live.get_engine().connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM customers")).scalar() >= 4          # nothing was removed


def test_a_dedicated_select_only_user_cannot_write_even_without_session_settings(server, live, monkeypatch):
    """Production setup: READONLY_DATABASE_URL points at a user that only has SELECT."""
    root = create_engine(__import__("app.db.urls", fromlist=["x"]).normalize_database_url(ROOT_URLS[server]), isolation_level="AUTOCOMMIT")
    try:
        with root.connect() as c:
            if server == "postgres":
                # grants from an earlier run keep the role "in use"; DROP OWNED removes them so the test can be repeated
                c.execute(text("DO $$ BEGIN IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'sentinel_ro') THEN EXECUTE 'DROP OWNED BY sentinel_ro'; END IF; END $$"))
                c.execute(text("DROP USER IF EXISTS sentinel_ro"))
                c.execute(text("CREATE USER sentinel_ro WITH PASSWORD 'ro'"))
                c.execute(text("GRANT CONNECT ON DATABASE demo TO sentinel_ro"))
                c.execute(text("GRANT USAGE ON SCHEMA public TO sentinel_ro"))
                c.execute(text("GRANT SELECT ON ALL TABLES IN SCHEMA public TO sentinel_ro"))
                ro_url = _with_credentials(server, "sentinel_ro", "ro")
            else:
                c.execute(text("DROP USER IF EXISTS 'sentinel_ro'@'%'"))
                c.execute(text("CREATE USER 'sentinel_ro'@'%' IDENTIFIED BY 'ro'"))
                c.execute(text("GRANT SELECT ON demo.* TO 'sentinel_ro'@'%'"))
                ro_url = _with_credentials(server, "sentinel_ro", "ro")
    except Exception as e:
        pytest.skip(f"cannot create a SELECT-only user here: {e}")
    finally:
        root.dispose()
    monkeypatch.setenv("READONLY_DATABASE_URL", ro_url)
    live.reset_engines()
    assert live.execute_readonly_query("SELECT COUNT(*) AS n FROM customers")["rows"][0]["n"] >= 4
    with pytest.raises(Exception):
        with live.get_readonly_engine().connect() as c:
            c.execute(text("DELETE FROM customers"))
            c.commit()


def test_a_runaway_query_is_cancelled_at_the_timeout_and_the_connection_stays_healthy(live, server):
    heavy = {"postgres": "SELECT COUNT(*) FROM generate_series(1, 400000000) a",
             "mysql": "SELECT COUNT(*) FROM information_schema.columns a, information_schema.columns b, information_schema.columns c"}[server]
    started = time.time()
    with pytest.raises(live.QueryTimeoutError):
        live.execute_readonly_query(heavy)
    assert time.time() - started < 6
    assert live.execute_readonly_query("SELECT COUNT(*) AS n FROM customers")["rows"][0]["n"] >= 4


# ---------------------------------------------------------------- typing, masking, dialect SQL (PGM-04, PGM-06)

def test_numbers_and_dates_come_back_as_plain_json_and_sensitive_values_are_masked(live):
    out = live.execute_readonly_query("SELECT order_id, amount, order_date FROM orders ORDER BY order_id LIMIT 2")
    json.dumps(out)                                                      # Decimal / date from the driver must not break serialisation
    assert out["rows"][0]["order_id"] == 1 and isinstance(out["rows"][0]["order_date"], str)
    salary = live.execute_readonly_query("SELECT employee_name, salary AS pay FROM salaries LIMIT 1")["rows"][0]
    assert salary["pay"] == "[CONFIDENTIAL]"                             # lineage masking through an alias, on a Decimal value
    email = live.execute_readonly_query("SELECT email FROM customers LIMIT 1")["rows"][0]["email"]
    assert "*" in email


def test_unusual_column_types_survive_the_round_trip(live, server):
    with live.get_engine().begin() as c:
        c.execute(text("DROP TABLE IF EXISTS study_types"))
        if server == "postgres":
            c.execute(text("CREATE TABLE study_types (id serial primary key, flag boolean, doc jsonb, tags text[], at timestamptz, blob bytea, price numeric(10,2))"))
            c.execute(text("INSERT INTO study_types (flag, doc, tags, at, blob, price) VALUES (true, '{\"a\": 1}', ARRAY['x','y'], now(), '\\x0102', 12.50)"))
        else:
            c.execute(text("CREATE TABLE study_types (id int auto_increment primary key, flag boolean, doc json, at timestamp, blob_col blob, price decimal(10,2))"))
            c.execute(text("INSERT INTO study_types (flag, doc, at, blob_col, price) VALUES (true, '{\"a\": 1}', now(), x'0102', 12.50)"))
    try:
        from app.utils.schemacontext import clear_schema_cache
        clear_schema_cache()
        out = live.execute_readonly_query("SELECT * FROM study_types")
        json.dumps(out)
        row = out["rows"][0]
        assert row["price"] == 12.5 and row["flag"] in (True, 1)
        assert any(isinstance(v, str) and v.startswith("<binary") for v in row.values())      # binary is not exposed
    finally:
        with live.get_engine().begin() as c:
            c.execute(text("DROP TABLE IF EXISTS study_types"))
        clear_schema_cache()


DIALECT_QUERIES = {
    "postgres": ["SELECT name FROM customers WHERE name ILIKE 'a%'", "SELECT order_id::text AS o FROM orders",
                 "SELECT DATE_TRUNC('month', order_date) AS m, COUNT(*) AS n FROM orders GROUP BY 1",
                 "SELECT \"name\" FROM \"customers\" WHERE region = ANY(ARRAY['North','South'])"],
    "mysql": ["SELECT `name` FROM `customers` LIMIT 0, 3", "SELECT DATE_FORMAT(order_date, '%Y-%m') AS m, COUNT(*) AS n FROM orders GROUP BY m",
              "SELECT name FROM customers WHERE name LIKE '%a%'", "SELECT name FROM customers WHERE name REGEXP '^A'"],
}


def test_each_engines_own_syntax_passes_the_policy_and_runs(live, server):
    from app.utils.sql_parser import parse_and_validate_ast
    for sql in DIALECT_QUERIES[server]:
        verdict = parse_and_validate_ast(sql)
        assert verdict.is_valid, f"{sql!r}: {verdict.reason}"
        assert live.execute_readonly_query(verdict.sanitized_sql)["columns"]


def test_schema_qualified_and_system_tables_still_need_review(live, server):
    from app.utils.sql_parser import parse_and_validate_ast
    qualified = {"postgres": "SELECT salary FROM public.salaries", "mysql": "SELECT salary FROM demo.salaries"}[server]
    catalog = {"postgres": "SELECT usename FROM pg_user", "mysql": "SELECT user FROM mysql.user"}[server]
    for sql in (qualified, catalog):
        v = parse_and_validate_ast(sql)
        assert v.is_valid and v.requires_human, sql


# ---------------------------------------------------------------- the whole gateway on a live database

def test_mcp_path_end_to_end_on_a_live_database(live):
    from app.mcp_server import mcp_submit_sql
    ok = mcp_submit_sql("SELECT name, email FROM customers LIMIT 2", client_id="agent-live")
    assert ok["status"] == "completed" and "*" in ok["results"]["rows"][0]["email"]
    assert mcp_submit_sql("SELECT * FROM customers", client_id="agent-live")["status"] == "rejected"
    assert mcp_submit_sql("DROP TABLE customers", client_id="agent-live")["status"] == "rejected"
    held = mcp_submit_sql("SELECT employee_name, salary FROM salaries", client_id="agent-live")
    assert held["status"] == "requires_human" and held["run_id"]


def test_the_cache_version_changes_when_the_data_changes(live):
    before = live.get_data_version()
    assert before != "untracked"
    with live.get_engine().begin() as c:
        c.execute(text("INSERT INTO customers (name, email, region) VALUES ('Cache Probe', 'cp@example.com', 'West')"))
    deadline = time.time() + 12                                          # server statistics are flushed asynchronously
    changed = False
    while time.time() < deadline:
        if live.get_data_version() != before:
            changed = True
            break
        time.sleep(0.5)
    with live.get_engine().begin() as c:
        c.execute(text("DELETE FROM customers WHERE email = 'cp@example.com'"))
    assert changed


def test_query_endpoint_and_cache_end_to_end_on_a_live_database(live, server, tmp_path):
    """The whole HTTP path (auth, pipeline, validation, execution, masking, audit, cache) with only the two model calls replaced."""
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from app.agents.intent_agent import QueryIntent
    from app.cache.semantic_cache import SemanticCache
    from app.main import app

    cache = SemanticCache(cache_file=str(tmp_path / "cache.json"), enabled=True)
    sql = "SELECT order_id, amount, order_date FROM orders ORDER BY order_id"
    headers = {"Authorization": "Bearer test-employee-token"}
    with patch("app.routers.query.get_semantic_cache", return_value=cache), \
            patch("app.graph.workflow.get_rag_schema_context", return_value="orders(order_id, amount, order_date)"), \
            patch("app.graph.workflow.IntentAnalysisAgent") as intent, patch("app.graph.workflow.SQLGenerationAgent") as generator:
        intent.return_value.analyze.return_value = QueryIntent(tables=["orders"])
        generator.return_value.generate_sql.return_value = sql
        client = TestClient(app)
        first = client.post("/query", json={"query": "List the orders"}, headers=headers).json()
        second = client.post("/query", json={"query": "List the orders"}, headers=headers).json()
    assert first["status"] == "completed" and first["cached"] is False and len(first["results"]["rows"]) >= 5
    assert second["cached"] is True and second["results"] == first["results"]       # Decimal/date values survived the cache round trip
    json.dumps(first["results"])

