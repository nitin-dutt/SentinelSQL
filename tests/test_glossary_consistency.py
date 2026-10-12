"""
Regression tests for DAT-06 / DAT-07: the glossary and golden stores initialise lazily with
configurable paths, and every demo glossary formula references columns that really exist.
"""

import os
import re
import sqlite3
import subprocess
import sys

import pytest

from app.db import database as db
from app.rag import data_dictionary as dd
from app.rag import golden_store as gs
from app.utils.schemacontext import clear_schema_cache

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def demo(tmp_path, monkeypatch):
    """A freshly seeded demo database with the glossary and golden store pointed at it."""
    path = tmp_path / "demo.db"
    monkeypatch.setenv("ACTIVE_DB", "sqlite")
    monkeypatch.setenv("DATABASE_URL_SQLITE", f"sqlite:///{path.as_posix()}")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DICTIONARY_DB_PATH", raising=False)
    monkeypatch.delenv("GOLDEN_DB_PATH", raising=False)
    monkeypatch.setenv("DICTIONARY_VECTOR_CACHE", str(tmp_path / "dict_vectors.json"))
    monkeypatch.setenv("GOLDEN_VECTOR_CACHE", str(tmp_path / "golden_vectors.json"))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)   # offline embeddings: deterministic, no network
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    db.reset_engines()
    clear_schema_cache()
    dd._INITIALIZED.clear(); gs._INITIALIZED.clear(); dd._VECTOR_STORE = None; gs._VECTOR_STORE = None
    db.init_db()
    yield path
    db.reset_engines()
    clear_schema_cache()
    dd._INITIALIZED.clear(); gs._INITIALIZED.clear(); dd._VECTOR_STORE = None; gs._VECTOR_STORE = None


def test_importing_the_stores_touches_no_database(tmp_path):
    ghost = tmp_path / "nothing.db"
    code = "import app.rag.golden_store, app.rag.data_dictionary"
    env = {**os.environ, "ACTIVE_DB": "sqlite", "DATABASE_URL_SQLITE": f"sqlite:///{ghost.as_posix()}",
           "PYTHONIOENCODING": ""}
    result = subprocess.run([sys.executable, "-c", code], cwd=PROJECT_ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert not ghost.exists()


def test_stores_follow_the_configured_database(demo):
    assert dd._db_path() == str(demo).replace("\\", "/") or os.path.samefile(dd._db_path(), demo)
    assert len(dd.get_all_terms()) == len(dd.DEFAULT_GLOSSARY_SEEDS)
    gs.save_golden_query("how many customers", "SELECT COUNT(*) FROM customers")
    con = sqlite3.connect(demo)
    assert con.execute("SELECT COUNT(*) FROM golden_queries").fetchone()[0] == 1
    con.close()


def test_explicit_paths_override_the_database_location(tmp_path, monkeypatch):
    monkeypatch.setenv("DICTIONARY_DB_PATH", str(tmp_path / "terms.db"))
    monkeypatch.setenv("GOLDEN_DB_PATH", str(tmp_path / "golden.db"))
    assert dd._db_path().endswith("terms.db") and gs._db_path().endswith("golden.db")


def test_non_sqlite_deployments_keep_bookkeeping_out_of_the_customer_database(monkeypatch):
    monkeypatch.setenv("ACTIVE_DB", "postgres")
    monkeypatch.setenv("DATABASE_URL_POSTGRES", "postgresql+psycopg2://u:p@h/db")
    monkeypatch.delenv("DICTIONARY_DB_PATH", raising=False)
    assert dd._db_path() == "sentinel_app.db"


def test_every_glossary_formula_references_real_columns(demo):
    """The old seeds used orders.total_amount, customers.status, products.cost... none of which exist."""
    con = sqlite3.connect(demo)
    columns = {}
    for (table,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        columns[table] = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    con.close()
    problems = []
    for seed in dd.DEFAULT_GLOSSARY_SEEDS:
        for table, column in re.findall(r"\b([a-z_]+)\.([a-z_]+)\b", seed["formula_or_rule"]):
            if table in columns and column not in columns[table]:
                problems.append((seed["term"], f"{table}.{column}"))
            if table not in columns and table not in ("julianday",):
                problems.append((seed["term"], f"unknown table {table}"))
    assert problems == []


def test_every_glossary_formula_runs_against_the_demo_data(demo):
    con = sqlite3.connect(demo)
    probes = {
        "Net Revenue": "SELECT {f} FROM orders",
        "Active Customer": "SELECT COUNT(*) FROM customers WHERE {f}",
        "Average Order Value (AOV)": "SELECT {f} FROM orders",
        "VIP Customer": "SELECT customer_id FROM orders GROUP BY customer_id HAVING {f}",
        "Open Invoice": "SELECT COUNT(*) FROM invoices WHERE {f}",
        "Escalated Ticket": "SELECT COUNT(*) FROM support_tickets WHERE {f}",
        "Slow Delivery": "SELECT COUNT(*) FROM shipments WHERE {f}",
        "Senior Employee": "SELECT COUNT(*) FROM employees WHERE {f}",
        "Top Performer": "SELECT employee_id FROM performance_reviews GROUP BY employee_id HAVING {f}",
    }
    assert set(probes) == {s["term"] for s in dd.DEFAULT_GLOSSARY_SEEDS}
    for seed in dd.DEFAULT_GLOSSARY_SEEDS:
        formula = re.sub(r"\s{2,}\(.*\)$", "", seed["formula_or_rule"])   # strip the trailing usage hint
        con.execute(probes[seed["term"]].format(f=formula)).fetchall()
    con.close()


def test_system_seeds_are_reconciled_but_admin_terms_are_untouched(demo):
    dd.get_all_terms()                      # first use creates the table (initialisation is lazy)
    con = sqlite3.connect(demo)
    con.execute("UPDATE data_dictionary SET formula_or_rule = 'orders.total_amount' WHERE term = 'Net Revenue'")  # the old bug
    con.execute("INSERT INTO data_dictionary (term, definition, formula_or_rule, created_by) VALUES ('Legacy Metric', 'x', 'y', 'system')")
    con.execute("INSERT INTO data_dictionary (term, definition, formula_or_rule, created_by) VALUES ('Mine', 'x', 'custom', 'alice-admin')")
    con.commit(); con.close()
    dd._INITIALIZED.clear()
    terms = {t["term"]: t for t in dd.get_all_terms()}
    assert "total_amount" not in terms["Net Revenue"]["formula_or_rule"]
    assert "Legacy Metric" not in terms
    assert terms["Mine"]["formula_or_rule"] == "custom"


def test_vectors_of_deleted_terms_are_pruned(demo):
    dd.get_all_terms()
    term_id = dd.add_term("Temp Metric", "d", "COUNT(*)", "orders")
    assert str(term_id) in dd.get_vector_store().documents
    con = sqlite3.connect(demo); con.execute("DELETE FROM data_dictionary WHERE id = ?", (term_id,)); con.commit(); con.close()
    dd._INITIALIZED.clear()
    dd.get_all_terms()
    assert str(term_id) not in dd.get_vector_store().documents
