"""
Regression tests for alignment_plan G6 (data, schema & persistence) and G8 (runtime
reliability): DAT-01..05 and RUN-01..06.
"""

import os
import re
import sqlite3
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.agents.intent_agent import IntentAnalysisAgent
from app.agents.sql_generator import SQLGenerationAgent
from app.db import database as db
from app.graph import workflow
from app.hitl import web_store
from app.utils.llm_factory import LLMUnavailableError, UniversalLLM, get_llm
from app.utils.schemacontext import clear_schema_cache
from tests.helpers import ADMIN, EMPLOYEE

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_python(code: str, **env) -> subprocess.CompletedProcess:
    """Runs `code` in a fresh interpreter from the project root (a real process boundary)."""
    full_env = {**os.environ, "PYTHONIOENCODING": "", **env}
    return subprocess.run([sys.executable, "-c", code], cwd=PROJECT_ROOT, env=full_env,
                          capture_output=True, text=True, timeout=120)


@pytest.fixture
def temp_demo_db(tmp_path, monkeypatch):
    """Points the app at an empty throwaway SQLite file and restores everything afterwards."""
    path = tmp_path / "demo.db"
    monkeypatch.setenv("ACTIVE_DB", "sqlite")
    monkeypatch.setenv("DATABASE_URL_SQLITE", f"sqlite:///{path.as_posix()}")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("READONLY_DATABASE_URL", raising=False)
    monkeypatch.delenv("SEED_DEMO_DATA", raising=False)
    db.reset_engines()
    clear_schema_cache()
    yield path
    db.reset_engines()
    clear_schema_cache()


def table_counts(path) -> dict:
    con = sqlite3.connect(path)
    try:
        names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {n: con.execute(f"SELECT COUNT(*) FROM {n}").fetchone()[0] for n in sorted(names)}
    finally:
        con.close()


# ---------------------------------------------------------------- DAT-05 / DAT-01: init and seeding

def test_sql_splitter_respects_strings_and_comments():
    script = "INSERT INTO t VALUES ('a;b'); -- trailing; comment\n/* block; */ SELECT 'it''s;'; "
    assert db.split_sql_statements(script) == ["INSERT INTO t VALUES ('a;b')", "SELECT 'it''s;'"]
    assert db.split_sql_statements("SELECT 1") == ["SELECT 1"]
    assert db.split_sql_statements("  ;; ") == []


def test_init_db_builds_the_extended_demo_and_is_idempotent(temp_demo_db):
    for _ in range(3):  # restarts / hot reloads
        db.init_db()
    counts = table_counts(temp_demo_db)
    assert len(counts) >= 20
    assert {"customers", "orders", "products", "salaries", "employees", "order_items", "payment_cards"} <= set(counts)
    # the base tables keep exactly their seed rows: no duplicate accumulation
    assert (counts["customers"], counts["orders"], counts["products"], counts["salaries"]) == (4, 5, 4, 3)
    assert db.demo_seed_health() == {}


def test_extended_demo_data_is_deterministic_and_foreign_keys_hold(temp_demo_db):
    from app.db.demo_seed import build_rows
    assert build_rows() == build_rows()
    db.init_db()
    con = sqlite3.connect(temp_demo_db)
    try:
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        con.close()


def test_demo_data_contains_the_pii_the_masking_features_need(temp_demo_db):
    db.init_db()
    con = sqlite3.connect(temp_demo_db)
    try:
        assert con.execute("SELECT COUNT(*) FROM employees WHERE ssn LIKE '9__-__-____'").fetchone()[0] == 24
        assert con.execute("SELECT COUNT(*) FROM payment_cards").fetchone()[0] >= 6
        assert con.execute("SELECT COUNT(*) FROM support_tickets WHERE notes LIKE '%@%' OR notes LIKE '%(555)%'").fetchone()[0] > 0
    finally:
        con.close()


def test_health_check_flags_accumulated_duplicates(temp_demo_db):
    db.init_db()
    con = sqlite3.connect(temp_demo_db)
    con.execute("INSERT INTO orders (customer_id, amount, order_date) VALUES (1, 1.0, '2030-01-01')")
    con.commit()
    con.close()
    assert db.demo_seed_health() == {"orders": 6}


def test_seeding_is_off_by_default_for_non_sqlite_databases(monkeypatch):
    monkeypatch.setenv("ACTIVE_DB", "postgres")
    monkeypatch.setenv("DATABASE_URL_POSTGRES", "postgresql+psycopg2://u:p@127.0.0.1:1/none")  # unreachable
    monkeypatch.delenv("SEED_DEMO_DATA", raising=False)
    db.reset_engines()
    try:
        assert db.seed_demo_data_enabled() is False
        db.init_db()  # must return without ever connecting (it would fail: nothing listens on port 1)
    finally:
        db.reset_engines()


def test_seeding_can_be_forced_either_way(temp_demo_db, monkeypatch):
    monkeypatch.setenv("SEED_DEMO_DATA", "false")
    db.init_db()
    assert not temp_demo_db.exists() or table_counts(temp_demo_db) == {}
    monkeypatch.setenv("SEED_DEMO_DATA", "true")
    db.init_db()
    assert "employees" in table_counts(temp_demo_db)


# ---------------------------------------------------------------- RUN-05: import has no side effects

def test_importing_the_database_module_prints_nothing_and_touches_no_file(tmp_path):
    ghost = tmp_path / "must_not_be_created.db"
    result = run_python("import app.db.database", ACTIVE_DB="sqlite", DATABASE_URL_SQLITE=f"sqlite:///{ghost.as_posix()}")
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""  # stdout is the JSON-RPC channel of the stdio MCP transport
    assert not ghost.exists()


def test_an_unreachable_database_raises_instead_of_exiting():
    code = ("from app.db.database import check_connection\n"
            "try:\n    check_connection()\nexcept Exception as e:\n    print('RAISED', type(e).__name__)\n")
    result = run_python(code, ACTIVE_DB="postgres", DATABASE_URL_POSTGRES="postgresql+psycopg2://u:p@127.0.0.1:1/none",
                        DB_FALLBACK_TO_SQLITE="")
    assert result.returncode == 0 and "RAISED" in result.stdout


def test_lazy_module_attributes_still_work():
    from app.db.database import engine, readonly_engine
    assert engine.dialect.name == "sqlite" and readonly_engine.dialect.name == "sqlite"


# ---------------------------------------------------------------- DAT-04 / DAT-02: schema handling

def test_schema_failure_raises_instead_of_inventing_a_schema(monkeypatch):
    from app.utils import schemacontext
    clear_schema_cache()
    with patch("app.db.database.get_engine", side_effect=RuntimeError("db is down")):
        with pytest.raises(schemacontext.SchemaUnavailableError, match="db is down"):
            schemacontext.introspect_schema(force_refresh=True)
    clear_schema_cache()


def test_no_table_names_are_hardcoded_in_the_application_code():
    allowed = {"app/db/demo_seed.py"}  # seed data legitimately names its tables
    pattern = re.compile(r"\b(customers|orders|salaries)\b\s*\(")
    offenders = []
    for root, _dirs, files in os.walk(os.path.join(PROJECT_ROOT, "app")):
        for name in files:
            rel = os.path.relpath(os.path.join(root, name), PROJECT_ROOT).replace(os.sep, "/")
            if name.endswith(".py") and rel not in allowed and pattern.search(open(os.path.join(root, name), encoding="utf-8").read()):
                offenders.append(rel)
    assert offenders == []


def test_table_descriptions_come_from_data_not_code(tmp_path, monkeypatch):
    from app.rag.schema_retriever import load_table_descriptions
    assert "employees" in load_table_descriptions()
    custom = tmp_path / "d.json"
    custom.write_text('{"_comment": "x", "Widgets": "Things we sell"}')
    monkeypatch.setenv("TABLE_DESCRIPTIONS_PATH", str(custom))
    assert load_table_descriptions() == {"widgets": "Things we sell"}
    monkeypatch.setenv("TABLE_DESCRIPTIONS_PATH", str(tmp_path / "missing.json"))
    assert load_table_descriptions() == {}


def test_schema_rag_activates_on_the_extended_demo(temp_demo_db, tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)  # offline embeddings: deterministic, no network
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    db.init_db()
    from app.rag.schema_retriever import SchemaRAGRetriever
    from app.utils.schemacontext import introspect_schema
    total = len(introspect_schema()["tables"])
    assert total > 5
    retrieved = SchemaRAGRetriever(cache_file=str(tmp_path / "v.json")).retrieve_schema("sales items per order and product")
    tables_in_prompt = [line.split("(")[0] for line in retrieved.splitlines() if "(" in line and "->" not in line]
    assert 0 < len(tables_in_prompt) < total  # pruned: RAG is doing real work now
    # FK closure: if an FK is listed, both of its tables are present
    for rel in [l for l in retrieved.splitlines() if "->" in l]:
        left, right = rel.split("->")
        assert left.split(".")[0].strip() in tables_in_prompt and right.split(".")[0].strip() in tables_in_prompt


# ---------------------------------------------------------------- DAT-03: persistent HITL runs

def test_runs_survive_a_process_restart(tmp_path):
    store = tmp_path / "runs.db"
    first = run_python("from app.hitl.web_store import save_run; "
                       "print(save_run({'user_query': 'q', 'sql': 'SELECT 1', 'user_id': 'emp-1'}))", HITL_DB_PATH=str(store))
    assert first.returncode == 0, first.stderr
    run_id = first.stdout.strip().splitlines()[-1]
    second = run_python(f"from app.hitl.web_store import get_run; r = get_run('{run_id}'); print(r['status'], r['user_id'], r['sql'])",
                        HITL_DB_PATH=str(store))
    assert second.stdout.split() == ["pending", "emp-1", "SELECT", "1"]


@pytest.fixture
def run_store(tmp_path, monkeypatch):
    monkeypatch.setenv("HITL_DB_PATH", str(tmp_path / "runs.db"))
    yield web_store


def test_store_roundtrip_update_and_listing(run_store):
    rid = run_store.save_run({"user_query": "q", "sql": "SELECT 1", "user_id": "u1"})
    assert run_store.get_run(rid)["status"] == "pending"
    assert rid in run_store.get_pending_runs()
    run_store.update_run(rid, status="completed", results={"columns": ["n"], "rows": [{"n": 1}]}, sql="SELECT 1 LIMIT 100")
    done = run_store.get_run(rid)
    assert done["status"] == "completed" and done["sql"] == "SELECT 1 LIMIT 100" and done["results"]["rows"] == [{"n": 1}]
    assert rid not in run_store.get_pending_runs() and rid in run_store.get_all_runs()
    run_store.delete_run(rid)
    with pytest.raises(KeyError):
        run_store.get_run(rid)


def test_non_json_values_in_results_do_not_break_persistence(run_store):
    import datetime, decimal
    rid = run_store.save_run({"user_query": "q"})
    run_store.update_run(rid, status="completed", results={"rows": [{"d": datetime.date(2025, 1, 2), "n": decimal.Decimal("1.50")}]})
    assert run_store.get_run(rid)["results"]["rows"] == [{"d": "2025-01-02", "n": "1.50"}]


def test_stale_pending_runs_expire(run_store, monkeypatch):
    rid = run_store.save_run({"user_query": "q", "user_id": "u1"})
    monkeypatch.setenv("HITL_PENDING_TTL_HOURS", "0")
    assert run_store.get_run(rid)["status"] == "expired"
    assert rid not in run_store.get_pending_runs()


def test_finished_runs_are_deleted_after_their_retention(run_store, monkeypatch):
    rid = run_store.save_run({"user_query": "q"})
    run_store.update_run(rid, status="completed", results={"rows": []})
    monkeypatch.setenv("HITL_FINISHED_TTL_HOURS", "0")
    with pytest.raises(KeyError):
        run_store.get_run(rid)


def test_an_expired_run_cannot_be_approved(run_store, monkeypatch):
    client = TestClient(__import__("app.main", fromlist=["app"]).app)
    rid = run_store.save_run({"user_query": "q", "sql": "SELECT name FROM customers LIMIT 1", "user_id": "emp-1"})
    monkeypatch.setenv("HITL_PENDING_TTL_HOURS", "0")
    r = client.post("/hitl/decision", json={"run_id": rid, "decision": "approve"}, headers=ADMIN)
    assert r.json()["status"] == "rejected" and "expired" in r.json()["reason"]


# ---------------------------------------------------------------- RUN-01: logging

def test_validation_works_on_a_cp1252_console():
    """The original crash: emoji in print() raised UnicodeEncodeError on a default Windows console."""
    code = ("from app.agents.sql_validator import SQLValidationAgent\n"
            "r = SQLValidationAgent().validate('SELECT name FROM customers LIMIT 5', 'q')\n"
            "print('SAFE' if r['safe'] else 'UNSAFE')\n")
    result = run_python(code, PYTHONIOENCODING="cp1252")
    assert result.returncode == 0, result.stderr
    assert "SAFE" in result.stdout and "UnicodeEncodeError" not in result.stderr


def test_application_code_does_not_use_print():
    allowed = {"app/main.py", "app/hitl/approval.py"}  # interactive CLI entry points
    offenders = []
    for root, _dirs, files in os.walk(os.path.join(PROJECT_ROOT, "app")):
        for name in files:
            rel = os.path.relpath(os.path.join(root, name), PROJECT_ROOT).replace(os.sep, "/")
            if name.endswith(".py") and rel not in allowed and re.search(r"^\s*print\(", open(os.path.join(root, name), encoding="utf-8").read(), re.M):
                offenders.append(rel)
    assert offenders == []


def test_log_output_goes_to_stderr_not_stdout():
    result = run_python("from app.utils.logger import get_logger; get_logger('sentinel.t').warning('hello')")
    assert "hello" in result.stderr and result.stdout == ""


# ---------------------------------------------------------------- RUN-02: LLM reliability

class FlakyClient:
    def __init__(self, failures, error=RuntimeError("503 Service Unavailable")):
        self.failures, self.error, self.calls = failures, error, 0

    def invoke(self, prompt):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return MagicMock(content="ok")


def test_transient_llm_failures_are_retried(monkeypatch):
    monkeypatch.setenv("LLM_MAX_RETRIES", "2")
    client = FlakyClient(failures=2)
    with patch("app.utils.llm_factory.time.sleep") as sleep:
        assert UniversalLLM("test", client).invoke("hi") == "ok"
    assert client.calls == 3 and sleep.call_count == 2


def test_persistent_llm_failure_raises_a_clean_error(monkeypatch):
    monkeypatch.setenv("LLM_MAX_RETRIES", "1")
    client = FlakyClient(failures=99)
    with patch("app.utils.llm_factory.time.sleep"):
        with pytest.raises(LLMUnavailableError, match="unavailable"):
            UniversalLLM("test", client).invoke("hi")
    assert client.calls == 2


def test_non_transient_llm_errors_are_not_retried(monkeypatch):
    monkeypatch.setenv("LLM_MAX_RETRIES", "3")
    client = FlakyClient(failures=99, error=ValueError("invalid api key"))
    with pytest.raises(LLMUnavailableError):
        UniversalLLM("test", client).invoke("hi")
    assert client.calls == 1


def test_llm_clients_are_cached_per_role():
    assert get_llm("sql") is get_llm("sql") and get_llm("sql") is not get_llm("intent")


@pytest.fixture
def sql_agent():
    with patch("app.agents.sql_generator.get_llm", return_value=MagicMock()):
        return SQLGenerationAgent()


@pytest.mark.parametrize("raw,expected", [
    ("```sql\nSELECT name FROM customers LIMIT 5;\n```", "SELECT name FROM customers LIMIT 5;"),
    ("Here is your query:\nSELECT name FROM customers LIMIT 5; -- done", "SELECT name FROM customers LIMIT 5;"),
    ("SELECT name FROM customers WHERE region = 'a;b' LIMIT 5;", "SELECT name FROM customers WHERE region = 'a;b' LIMIT 5;"),
    ("Sure! With pleasure. WITH x AS (SELECT name FROM customers) SELECT name FROM x LIMIT 3",
     "WITH x AS (SELECT name FROM customers) SELECT name FROM x LIMIT 3;"),
    ("SELECT name FROM customers; DROP TABLE customers;", "SELECT name FROM customers;"),
])
def test_sql_extraction_from_messy_model_output(sql_agent, raw, expected):
    assert sql_agent._extract_sql(raw) == expected


@pytest.mark.parametrize("raw", ["I cannot help with that request.", "SELECT FROM WHERE ((", "DROP TABLE customers", ""])
def test_unusable_model_output_is_a_clean_value_error(sql_agent, raw):
    with pytest.raises(ValueError):
        sql_agent._extract_sql(raw)


@pytest.mark.parametrize("garbage", ["not json at all", '{"tables": ["customers"', '"just a string"', "[]"])
def test_garbage_intent_json_is_a_clean_value_error(garbage):
    fake = MagicMock()
    fake.invoke.return_value = garbage
    with patch("app.agents.intent_agent.get_llm", return_value=fake):
        with pytest.raises(ValueError):
            IntentAnalysisAgent().analyze("show customers", "customers(name)")


def test_pipeline_reports_an_llm_outage_as_an_error_not_an_exception():
    with patch("app.graph.workflow.IntentAnalysisAgent") as agent, patch("app.graph.workflow.get_rag_schema_context", return_value="s"):
        agent.return_value.analyze.side_effect = LLMUnavailableError("The language model is unavailable right now (X).")
        out = workflow.build_graph().invoke({"user_query": "show customers", "user_id": "u", "role": "employee"})
    assert out["validation"]["error"] is True and out["validation"]["safe"] is False
    assert "unavailable" in out["validation"]["reason"]


def test_api_returns_status_error_and_audits_it():
    from app.main import app
    from app.utils.audit_logger import get_audit_logs
    client = TestClient(app)
    failing = {"validation": {"safe": False, "requires_human": False, "reason": "model down", "score": 0, "risk_level": "LOW", "error": True}}
    with patch("app.routers.query.build_graph") as graph:
        graph.return_value.invoke.return_value = failing
        r = client.post("/query", json={"query": "unique-error-case"}, headers=EMPLOYEE)
    assert r.status_code == 200 and r.json()["status"] == "error" and r.json()["reason"] == "model down"
    assert get_audit_logs(limit=1)[0]["execution_status"] == "error"


# ---------------------------------------------------------------- RUN-03 / RUN-04: pipeline efficiency and state

def test_graph_is_compiled_once():
    assert workflow.build_graph() is workflow.build_graph()


def test_schema_is_retrieved_once_per_request_and_identity_survives():
    fake_intent = MagicMock()
    fake_intent.analyze.return_value = MagicMock(model_dump=lambda: {"tables": ["customers"], "metrics": [], "group_by": [], "filters": [], "limit": 5})
    fake_gen = MagicMock()
    fake_gen.generate_sql.return_value = "SELECT name FROM customers LIMIT 5;"
    with patch("app.graph.workflow.IntentAnalysisAgent", return_value=fake_intent), \
         patch("app.graph.workflow.SQLGenerationAgent", return_value=fake_gen), \
         patch("app.graph.workflow.get_rag_schema_context", return_value="customers(name)") as retrieve:
        out = workflow.build_graph().invoke({"user_query": "show customers", "user_id": "emp-1", "role": "employee"})
    assert retrieve.call_count == 1                                   # was 2 (intent node + generation node)
    assert fake_gen.generate_sql.call_args.kwargs["schema_context"] == "customers(name)"
    assert out["user_id"] == "emp-1" and out["role"] == "employee"    # declared on AgentState, so it survives
    assert out["results"]["rows"][0]["name"] == "Alice"


# ---------------------------------------------------------------- RUN-06: structure

def test_lifespan_initialises_the_database_and_health_is_public(temp_demo_db):
    from app.main import app
    with TestClient(app) as client:  # entering the context runs startup
        assert client.get("/health").json() == {"status": "ok"}
    assert "employees" in table_counts(temp_demo_db)


def test_application_uses_no_deprecated_startup_hooks():
    for name in ("main.py",):
        assert "on_event" not in open(os.path.join(PROJECT_ROOT, "app", name), encoding="utf-8").read()


def test_main_module_is_small_and_routers_own_the_endpoints():
    lines = open(os.path.join(PROJECT_ROOT, "app", "main.py"), encoding="utf-8").read().splitlines()
    assert len(lines) < 80
    assert not any(l.lstrip().startswith("@app.") for l in lines)


def test_sql_extraction_accepts_every_set_operation(sql_agent):
    for op in ("INTERSECT", "EXCEPT", "UNION", "UNION ALL"):
        raw = f"SELECT region FROM customers {op} SELECT region FROM customers WHERE name = 'a'"
        assert sql_agent._extract_sql(raw).startswith("SELECT region FROM customers " + op)


def test_intent_tolerates_structured_filters_and_null_limit():
    """Regression: models return filters as objects and limit as null; that used to reject valid questions."""
    from app.agents.intent_agent import QueryIntent
    intent = QueryIntent.model_validate_json(
        '{"tables": ["airlines"], "metrics": [], "group_by": [], '
        '"filters": [{"column": "airlines.Airline", "operator": "=", "value": "JetBlue Airways"}], "limit": null}')
    assert intent.filters == ["airlines.Airline = 'JetBlue Airways'"]
    assert intent.limit is None
