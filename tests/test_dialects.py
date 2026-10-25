"""
PostgreSQL / MySQL compatibility that can be checked without a server: URL handling, dialect detection, and that the
policy engine reads and writes each engine's SQL correctly (the live checks are in test_pg_mysql_live.py).
"""

import pytest

from app.db.urls import normalize_database_url, sql_dialect, sql_dialect_label
from app.utils.sql_parser import parse_and_validate_ast

PG = "postgresql://u:p@localhost:5432/d"
MY = "mysql://u:p@localhost:3306/d"


@pytest.fixture
def use_url(monkeypatch):
    def _set(url):
        # Empty, not deleted: a module imported later calls load_dotenv(), which would re-add ACTIVE_DB=sqlite from a
        # developer's .env for any variable that is missing. load_dotenv never overrides one that exists, even if empty.
        for k in ("ACTIVE_DB", "DATABASE_URL_SQLITE", "DATABASE_URL_POSTGRES", "DATABASE_URL_MYSQL", "READONLY_DATABASE_URL"):
            monkeypatch.setenv(k, "")
        monkeypatch.setenv("DATABASE_URL", url)
    return _set


# ---------------------------------------------------------------- one URL is enough

@pytest.mark.parametrize("given,expected", [
    ("postgres://u:p@h:5432/d", "postgresql+psycopg2://u:p@h:5432/d"),
    ("postgresql://u:p@h/d", "postgresql+psycopg2://u:p@h/d"),
    ("mysql://u:p@h:3306/d", "mysql+mysqlconnector://u:p@h:3306/d"),
    ("mariadb://u:p@h/d", "mysql+mysqlconnector://u:p@h/d"),
    ("postgresql+psycopg2://u:p@h/d", "postgresql+psycopg2://u:p@h/d"),      # an explicit driver is left alone
    ("mysql+pymysql://u:p@h/d", "mysql+pymysql://u:p@h/d"),
    ("sqlite:///sentinel.db", "sqlite:///sentinel.db"),
])
def test_database_urls_are_mapped_to_installed_drivers(given, expected):
    assert normalize_database_url(given) == expected


@pytest.mark.parametrize("url,dialect,label", [
    ("sqlite:///x.db", "sqlite", "SQLite"),
    (PG, "postgres", "PostgreSQL"),
    ("postgres://u:p@h/d", "postgres", "PostgreSQL"),
    (MY, "mysql", "MySQL"),
    ("mysql+pymysql://u:p@h/d", "mysql", "MySQL"),
])
def test_the_dialect_comes_from_the_single_database_url(use_url, url, dialect, label):
    use_url(url)
    assert sql_dialect() == dialect and sql_dialect_label() == label


def test_a_single_database_url_drives_the_engines(use_url):
    from app.db.urls import resolve_urls
    use_url("postgres://u:p@h:5432/d")
    active, primary, readonly = resolve_urls()
    assert primary == "postgresql+psycopg2://u:p@h:5432/d" and readonly == primary   # session-level READ ONLY is applied to it


# ---------------------------------------------------------------- the policy reads each dialect's SQL

def test_mysql_backtick_identifiers_are_accepted_and_the_limit_keeps_the_dialect(use_url):
    use_url(MY)
    r = parse_and_validate_ast("SELECT `name`, `region` FROM `customers` WHERE `region` = 'North'")
    assert r.is_valid and r.sanitized_sql.endswith("LIMIT 100")
    assert "`name`" in r.sanitized_sql                 # regenerated for MySQL, not turned into "name" (which MySQL reads as a string)


def test_mysql_limit_offset_comma_form_is_read_correctly(use_url):
    use_url(MY)
    r = parse_and_validate_ast("SELECT name FROM customers LIMIT 20, 10")
    assert r.is_valid and r.has_limit


def test_postgres_casts_ilike_and_quoted_identifiers_are_accepted(use_url):
    use_url(PG)
    assert parse_and_validate_ast("SELECT name FROM customers WHERE name ILIKE 'a%'").is_valid
    assert parse_and_validate_ast("SELECT order_id::text FROM orders").is_valid
    assert parse_and_validate_ast('SELECT "name" FROM "customers"').is_valid


def test_postgres_date_functions_and_filters_are_accepted(use_url):
    use_url(PG)
    r = parse_and_validate_ast("SELECT DATE_TRUNC('month', order_date) AS m, COUNT(*) FROM orders GROUP BY 1")
    assert r.is_valid


@pytest.mark.parametrize("url,sql", [
    (PG, "SELECT name FROM customers; DROP TABLE customers"),
    (PG, "DELETE FROM customers"),
    (PG, "COPY customers TO '/tmp/x'"),
    (PG, "SELECT pg_sleep(10)"),
    (PG, "SELECT pg_read_file('/etc/passwd')"),
    (PG, "SELECT * FROM customers"),
    (MY, "SELECT name FROM customers; DROP TABLE customers"),
    (MY, "DELETE FROM customers"),
    (MY, "SELECT SLEEP(10)"),
    (MY, "SELECT LOAD_FILE('/etc/passwd')"),
    (MY, "SELECT * FROM customers"),
    (MY, "REPLACE INTO customers (customer_id, name) VALUES (1, 'x')"),
])
def test_attacks_written_in_each_dialect_are_still_blocked(use_url, url, sql):
    use_url(url)
    assert not parse_and_validate_ast(sql).is_valid


@pytest.mark.parametrize("url,sql", [
    (PG, "SELECT * FROM pg_catalog.pg_user"),
    (PG, "SELECT usename FROM pg_shadow"),
    (PG, "SELECT table_name FROM information_schema.tables"),
    (MY, "SELECT user FROM mysql.user"),
    (MY, "SELECT table_name FROM information_schema.tables"),
    (MY, "SELECT * FROM performance_schema.threads"),
])
def test_system_catalogs_of_each_engine_never_run_unreviewed(use_url, url, sql):
    use_url(url)
    r = parse_and_validate_ast(sql)
    assert (not r.is_valid) or r.requires_human


# ---------------------------------------------------------------- the model is told which dialect to write

def test_the_generator_prompt_names_the_configured_dialect(use_url):
    from unittest.mock import patch
    from app.agents.intent_agent import QueryIntent
    from app.agents.sql_generator import SQLGenerationAgent
    seen = {}

    class Recorder:
        def invoke(self, prompt):
            seen["p"] = prompt
            return "SELECT 1;"

    for url, label in ((PG, "PostgreSQL"), (MY, "MySQL")):
        use_url(url)
        with patch("app.agents.sql_generator.get_llm", return_value=Recorder()):
            SQLGenerationAgent().generate_sql(QueryIntent(tables=["t"]), "t(a)", "q?")
        assert f"valid {label} SELECT query" in seen["p"] and "SQLite" not in seen["p"]


def test_sql_extraction_reads_the_configured_dialect(use_url):
    from unittest.mock import patch
    from app.agents.sql_generator import SQLGenerationAgent
    use_url(MY)
    with patch("app.agents.sql_generator.get_llm", return_value=object()):
        agent = SQLGenerationAgent()
    assert agent._extract_sql("Here you go: SELECT `name` FROM `customers` LIMIT 5;").startswith("SELECT `name`")


# ---------------------------------------------------------------- driver values become JSON-safe

def test_driver_values_are_converted_to_plain_json_types():
    import datetime, json, uuid
    from decimal import Decimal
    from app.db.database import _json_safe
    row = {"a": Decimal("10.50"), "b": Decimal("7"), "c": datetime.date(2024, 5, 1), "d": datetime.datetime(2024, 5, 1, 12, 30),
           "e": uuid.UUID("12345678-1234-5678-1234-567812345678"), "f": b"\x00\x01\x02", "g": [Decimal("1.5"), None], "h": float("nan")}
    safe = {k: _json_safe(v) for k, v in row.items()}
    assert safe["a"] == 10.5 and safe["b"] == 7 and isinstance(safe["b"], int)
    assert safe["c"] == "2024-05-01" and safe["d"] == "2024-05-01T12:30:00"
    assert safe["f"] == "<binary 3 bytes>" and safe["g"] == [1.5, None]
    json.dumps({k: v for k, v in safe.items() if k != "h"})            # nothing raises


# ---------------------------------------------------------------- constructs that only exist on these engines

@pytest.mark.parametrize("url,sql", [
    (PG, "SELECT name INTO newtbl FROM customers"),
    (PG, "SELECT name FROM customers FOR UPDATE"),
    (PG, "SELECT name FROM customers FOR SHARE"),
    (PG, "SELECT set_config('default_transaction_read_only', 'off', false)"),
    (PG, "SELECT pg_terminate_backend(123)"),
    (PG, "SELECT pg_reload_conf()"),
    (PG, "SELECT pg_advisory_lock(1)"),
    (PG, "WITH x AS (DELETE FROM customers RETURNING customer_id) SELECT customer_id FROM x"),
    (MY, "SELECT name FROM customers INTO OUTFILE '/tmp/o.txt'"),
    (MY, "SELECT name INTO @v FROM customers LIMIT 1"),
    (MY, "SELECT name FROM customers FOR UPDATE"),
    (MY, "SELECT name FROM customers LOCK IN SHARE MODE"),
    (MY, "SELECT GET_LOCK('x', 10)"),
    (MY, "SELECT /*! 50000 SLEEP(5) */ 1"),
    (MY, "SELECT /*!50000 LOAD_FILE('/etc/passwd') */ 1"),
    (MY, "SELECT 1 /*M! SLEEP(5) */"),
])
def test_engine_specific_write_lock_and_hidden_code_constructs_are_blocked(use_url, url, sql):
    use_url(url)
    assert not parse_and_validate_ast(sql).is_valid


def test_ordinary_comments_are_still_fine_on_mysql(use_url):
    use_url(MY)
    assert parse_and_validate_ast("SELECT name /* the customer's name */ FROM customers -- all of them").is_valid


def test_executable_comments_are_not_a_postgres_concern(use_url):
    use_url(PG)
    assert parse_and_validate_ast("SELECT name /*! just a comment on PostgreSQL */ FROM customers").is_valid


# ---------------------------------------------------------------- the security benchmark behaves the same on every engine

def _benchmark_sql_cases():
    import importlib.util
    import json
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location("heldout_sql", os.path.join(root, "benchmarks", "data", "heldout_security_sql.py"))
    held = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(held)
    cases = [sql for _, _, _, sql, _ in held.ATTACK_SQL] + [sql for _, _, sql, _ in held.BENIGN_SQL]
    with open(os.path.join(root, "benchmarks", "data", "security_200.json"), encoding="utf-8") as f:
        cases += [c["input"] for c in json.load(f)["cases"] if c.get("input_type") == "sql"]
    return cases


def test_no_benchmark_sql_case_is_judged_less_strictly_on_postgres_or_mysql(monkeypatch):
    """
    The 326 attack and benign queries of the security benchmark were written for SQLite. On PostgreSQL or MySQL a query may be
    judged STRICTER (another engine's identifier quoting is a syntax error there), but never LESS strict, and only a handful differ.
    """
    rank = {"allow": 0, "review": 1, "block": 2}

    def verdicts(url):
        for k in ("ACTIVE_DB", "DATABASE_URL_SQLITE", "DATABASE_URL_POSTGRES", "DATABASE_URL_MYSQL", "READONLY_DATABASE_URL"):
            monkeypatch.setenv(k, "")          # empty, not deleted: load_dotenv() would put a developer's ACTIVE_DB back
        monkeypatch.setenv("DATABASE_URL", url)
        out = []
        for sql in _benchmark_sql_cases():
            r = parse_and_validate_ast(sql)
            out.append("block" if not r.is_valid else ("review" if r.requires_human else "allow"))
        return out

    base = verdicts("sqlite:///x.db")
    assert len(base) >= 300
    for url in (PG, MY):
        other = verdicts(url)
        looser = [i for i, (b, o) in enumerate(zip(base, other)) if rank[o] < rank[b]]
        assert not looser, f"{url}: cases judged less strictly than on SQLite: {looser}"
        assert sum(b != o for b, o in zip(base, other)) <= 5
