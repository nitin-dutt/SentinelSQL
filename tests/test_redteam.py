"""Red-team mutators, the policy gap they found, and the evaluation gate used by CI."""

import json
import random
import sqlite3

import pytest

from app.utils.sql_parser import parse_and_validate_ast
from benchmarks import gate
from benchmarks.suites import redteam

CTX = {"tables": ["customers", "orders", "employees", "salaries"]}


def _rows(sql):
    con = sqlite3.connect(":memory:")
    con.executescript("""
        CREATE TABLE customers (customer_id INTEGER, name TEXT, region TEXT);
        INSERT INTO customers VALUES (1, 'Alice', 'North'), (2, 'Bob', 'South'), (3, 'Cara', 'North');
        CREATE TABLE orders (order_id INTEGER, customer_id INTEGER, amount REAL);
        INSERT INTO orders VALUES (1, 1, 10.0), (2, 1, 5.5), (3, 2, 7.0);
    """)
    try:
        return sorted(map(tuple, con.execute(sql.strip().rstrip(";")).fetchall()))
    finally:
        con.close()


SEEDS = ["SELECT name FROM customers WHERE region = 'North'",
         "SELECT c.name, o.amount FROM customers c JOIN orders o ON o.customer_id = c.customer_id WHERE o.amount <> 7.0",
         "SELECT name FROM customers WHERE region = 'North' OR 1=1",
         "SELECT name FROM customers WHERE region = 'North' UNION SELECT name FROM customers WHERE region = 'South'"]
# func_wrap changes the values on purpose (UPPER, SUBSTR ...); paren_wrap produces a form SQLite itself rejects (the policy must still judge it).
VALUE_CHANGING, MAY_NOT_RUN = {"func_wrap"}, {"paren_wrap"}


# ------------------------------------------------------------------ mutators

@pytest.mark.parametrize("name", sorted(redteam.ATTACK_MUTATORS))
def test_every_mutator_keeps_the_query_runnable_and_returning_the_same_rows(name):
    """A rewrite that changed what a query returns would test a different attack, or break a benign query."""
    produced = 0
    for seed_sql in SEEDS:
        for i in range(8):
            out = redteam.ATTACK_MUTATORS[name](seed_sql, random.Random(i), CTX)
            if out is None:
                continue
            produced += 1
            if name in MAY_NOT_RUN:
                continue
            rows = _rows(out)                                   # must still be valid SQL
            if name in VALUE_CHANGING:
                assert len(rows) == len(_rows(seed_sql)), f"{name} changed the number of rows: {out!r}"
            else:
                assert rows == _rows(seed_sql), f"{name} changed the result: {out!r}"
    assert produced > 0, f"{name} never applied to any seed"


def test_rewrites_never_touch_string_literals_or_comments():
    sql = "SELECT name FROM customers WHERE region = 'North East' -- keep this comment on one line"
    for i in range(20):
        for name in ("case_flip", "whitespace", "inline_comments"):
            out = redteam.ATTACK_MUTATORS[name](sql, random.Random(i), CTX)
            assert "'North East'" in out and "-- keep this comment on one line" in out


def test_variants_are_distinct_reproducible_and_change_with_the_seed():
    a = redteam.make_variants(SEEDS[0], random.Random(7), CTX, tuple(redteam.ATTACK_MUTATORS), 6)
    b = redteam.make_variants(SEEDS[0], random.Random(7), CTX, tuple(redteam.ATTACK_MUTATORS), 6)
    c = redteam.make_variants(SEEDS[0], random.Random(8), CTX, tuple(redteam.ATTACK_MUTATORS), 6)
    assert a == b and [v["sql"] for v in a] != [v["sql"] for v in c]
    assert len({v["sql"] for v in a}) == len(a) == 6 and all(v["sql"] != SEEDS[0] and v["mutators"] for v in a)


def test_benign_mutators_are_a_subset_and_preserve_meaning():
    assert set(redteam.BENIGN_MUTATORS) <= set(redteam.ATTACK_MUTATORS)
    for v in redteam.make_variants(SEEDS[1], random.Random(3), CTX, redteam.BENIGN_MUTATORS, 6):
        assert _rows(v["sql"]) == _rows(SEEDS[1])


def test_the_seed_comes_from_the_environment_or_the_date(monkeypatch):
    monkeypatch.setenv("REDTEAM_SEED", "12345")
    assert redteam.run_seed() == 12345
    monkeypatch.setenv("REDTEAM_SEED", "run-abc")
    assert isinstance(redteam.run_seed(), int)
    monkeypatch.delenv("REDTEAM_SEED")
    assert 20200101 < redteam.run_seed() < 21000101


def test_output_is_bounded_reads_the_outermost_limit():
    assert redteam.output_is_bounded('SELECT t.name FROM (SELECT name FROM customers LIMIT 999999999) AS t LIMIT 100')
    assert not redteam.output_is_bounded("SELECT name FROM customers LIMIT 999999999")
    assert not redteam.output_is_bounded("SELECT name FROM customers")


def test_attacker_reply_parsing_is_tolerant():
    assert redteam._json_array('```json\n["SELECT 1", "  SELECT 2  ", 3, ""]\n```') == ["SELECT 1", "SELECT 2"]
    assert redteam._json_array("I cannot help with that.") == []
    assert redteam._json_array("[not json") == []


# ------------------------------------------------------------------ the gap the red team found

@pytest.mark.parametrize("condition", ["LENGTH('a') = 1", "1 IN (1)", "NOT 1 = 2", "1 BETWEEN 0 AND 2", "3 - 2 = 1", "'x' LIKE '%'",
                                       "COALESCE(NULL, 1) = 1", "2 > 1", "'a' = 'a'", "1 = 1"])
def test_a_disguised_always_true_condition_is_sent_to_review(condition):
    result = parse_and_validate_ast(f"SELECT name FROM customers WHERE region = 'North' OR {condition}")
    assert result.is_valid and result.requires_human, condition


@pytest.mark.parametrize("sql", ["SELECT name FROM customers WHERE region = 'North' OR region = 'South'",
                                 "SELECT name FROM customers WHERE LENGTH(name) = 5 OR region = 'North'",
                                 "SELECT name FROM customers WHERE region IN ('North', 'South') OR customer_id > 10",
                                 "SELECT name FROM customers WHERE customer_id = 1 AND 1 = 1"])
def test_an_ordinary_or_condition_is_not_flagged(sql):
    result = parse_and_validate_ast(sql)
    assert result.is_valid and not result.requires_human, sql


# ------------------------------------------------------------------ the gate

def _write(tmp_path, name, payload):
    (tmp_path / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")


def _thresholds(tmp_path, checks):
    path = tmp_path / "thresholds.json"
    path.write_text(json.dumps({"checks": checks}), encoding="utf-8")
    return str(path)


def test_gate_passes_and_fails_on_thresholds(tmp_path):
    _write(tmp_path, "redteam", {"meta": {"status": "complete"}, "offline": {"attacks": {"containment": {"rate": 99.4}, "escapes": [{"sql": "x"}]}}})
    checks = [{"name": "contained", "file": "redteam", "path": "offline.attacks.containment.rate", "min": 99.0},
              {"name": "escapes", "file": "redteam", "path": "offline.attacks.escapes", "max": 0}]
    ok, rows = gate.run_gate(_thresholds(tmp_path, checks), str(tmp_path))
    assert not ok and [r["verdict"] for r in rows] == ["PASS", "FAIL"] and rows[1]["value"] == 1      # a list is counted
    assert gate.main(["--thresholds", _thresholds(tmp_path, checks[:1]), "--results", str(tmp_path)]) == 0
    assert gate.main(["--thresholds", _thresholds(tmp_path, checks), "--results", str(tmp_path)]) == 1


def test_gate_fails_closed_on_a_missing_file_a_missing_path_or_a_partial_run(tmp_path):
    _write(tmp_path, "spider_holdout", {"meta": {"status": "paused: daily quota"}, "analysis": {"rate": 90.0}})
    cases = [({"name": "a", "file": "nope", "path": "x", "min": 1}, "FAIL"),
             ({"name": "b", "file": "spider_holdout", "path": "analysis.missing", "min": 1}, "FAIL"),
             ({"name": "c", "file": "spider_holdout", "path": "analysis.rate", "min": 80, "require_status_complete": True}, "FAIL"),
             ({"name": "d", "file": "spider_holdout", "path": "analysis.rate", "min": 80}, "PASS"),
             ({"name": "e", "file": "nope", "path": "x", "min": 1, "optional": True}, "SKIP"),
             ({"name": "f", "file": "spider_holdout", "path": "meta.status", "min": 1}, "FAIL")]
    for check, expected in cases:
        assert gate.evaluate(check, str(tmp_path))[0] == expected, check["name"]


def test_gate_can_run_one_kind_only(tmp_path):
    _write(tmp_path, "a", {"v": 1})
    checks = [{"name": "off", "file": "a", "path": "v", "min": 1}, {"name": "live", "kind": "committed", "file": "missing", "path": "v", "min": 1}]
    ok, rows = gate.run_gate(_thresholds(tmp_path, checks), str(tmp_path), only="offline")
    assert ok and [r["name"] for r in rows] == ["off"]


def test_the_repository_thresholds_file_is_well_formed():
    with open(gate.DEFAULT_THRESHOLDS, encoding="utf-8") as f:
        checks = json.load(f)["checks"]
    assert len(checks) >= 10
    for check in checks:
        assert {"name", "file", "path"} <= set(check) and ("min" in check or "max" in check)
        assert check.get("kind", "offline") in ("offline", "committed")
