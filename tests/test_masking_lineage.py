"""
Regression tests for alignment_plan G2 (PII-01 .. PII-04): masking follows column
lineage, embedded PII is caught in any text column, the sensitive-table policy comes
from config, and non-SQLite read-only sessions are enforced.
"""

import pytest

from app.db.database import execute_readonly_query, session_read_only_statements
from app.utils.masking import apply_data_masking, mask_text_content
from app.utils.sql_parser import parse_and_validate_ast, is_sensitive_table, load_sensitive_patterns


def run(sql):
    return execute_readonly_query(sql)


def cells(result, column):
    return [row[column] for row in result["rows"]]


# ---------------------------------------------------------------- PII-01: lineage

@pytest.mark.parametrize("sql,column", [
    ("SELECT email AS contact FROM customers", "contact"),
    ("SELECT UPPER(email) AS shout FROM customers", "shout"),
    ("SELECT email || '' AS e2 FROM customers", "e2"),
    ("SELECT t.c FROM (SELECT email AS c FROM customers) t", "c"),
    ("WITH x AS (SELECT email AS m FROM customers) SELECT m FROM x", "m"),
    ("SELECT c.email AS addr FROM customers c JOIN orders o ON c.customer_id = o.customer_id", "addr"),
])
def test_email_is_masked_however_it_is_reached(sql, column):
    values = cells(run(sql), column)
    assert values, "query returned no rows"
    assert not any(v and v.lower() in {"alice@example.com", "bob@example.com", "charlie@example.com", "diana@example.com"}
                   for v in values), values
    assert all("*" in v for v in values if v)


@pytest.mark.parametrize("sql,column", [
    ("SELECT salary AS pay FROM salaries", "pay"),
    ("SELECT salary * 2 AS double_pay FROM salaries", "double_pay"),
    ("SELECT t.p FROM (SELECT salary AS p FROM salaries) t", "p"),
    ("SELECT AVG(salary) AS avg_pay FROM salaries", "avg_pay"),
    ("SELECT employee_name AS n, salary AS s FROM salaries", "s"),
])
def test_salary_is_redacted_however_it_is_reached(sql, column):
    assert set(cells(run(sql), column)) == {"[CONFIDENTIAL]"}


def test_union_masks_both_branches_by_position():
    result = run("SELECT email AS v FROM customers UNION SELECT employee_name FROM salaries")
    emails = [v for v in cells(result, "v") if "@" in v]
    assert emails and all("*" in v for v in emails)


def test_non_sensitive_columns_are_left_alone():
    result = run("SELECT name AS who, region FROM customers ORDER BY customer_id")
    assert cells(result, "who") == ["Alice", "Bob", "Charlie", "Diana"]
    assert cells(result, "region") == ["North", "South", "West", "East"]


def test_count_of_a_sensitive_column_is_not_masked():
    assert cells(run("SELECT COUNT(email) AS n FROM customers"), "n") == [4]
    assert cells(run("SELECT COUNT(DISTINCT salary) AS n FROM salaries"), "n") == [3]


def test_name_based_masking_still_applies_without_sql():
    out = apply_data_masking({"columns": ["email", "salary"], "rows": [{"email": "alice.smith@example.com", "salary": 5}]})
    assert out["rows"][0]["email"] == "a*********h@example.com"
    assert out["rows"][0]["salary"] == "[CONFIDENTIAL]"


def test_unresolvable_lineage_falls_back_to_masking_not_leaking():
    # Columns do not exist in the schema, so lineage cannot resolve them; the query-wide
    # column names still mark the output as sensitive.
    out = apply_data_masking(
        {"columns": ["x"], "rows": [{"x": "bob@example.com"}]},
        sql="SELECT ghost_table.email AS x FROM ghost_table",
    )
    assert "*" in out["rows"][0]["x"]


def test_masking_does_not_blow_up_on_unparseable_sql():
    out = apply_data_masking({"columns": ["email"], "rows": [{"email": "bob@example.com"}]}, sql="SELEC nonsense (((")
    assert "*" in out["rows"][0]["email"]  # name-based layer still protects


def test_overbroad_column_patterns_no_longer_match_ordinary_names():
    out = apply_data_masking({"columns": ["company", "author"],
                              "rows": [{"company": "Acme Inc", "author": "Jane Roe"}]})
    assert out["rows"][0] == {"company": "Acme Inc", "author": "Jane Roe"}


# ---------------------------------------------------------------- PII-02: embedded PII anywhere

@pytest.mark.parametrize("text,leaked", [
    ("call me on (555) 123-4567 tomorrow", "123-4567"),
    ("phone 555-123-4567", "555-123-4567"),
    ("+1 555.123.4567", "555.123.4567"),
    ("ssn is 123-45-6789 ok", "123-45-6789"),
    ("card 4111 2222 3333 4444 on file", "4111 2222 3333"),
    ("reach bob.smith@example.com now", "bob.smith"),
])
def test_content_scanner_catches_embedded_pii(text, leaked):
    assert leaked not in mask_text_content(text)


@pytest.mark.parametrize("text", [
    "Order 2025-01-05 shipped", "Invoice INV-123-456 total 1,200.50", "version 3.11.7",
    "UUID 550e8400-e29b-41d4-a716-446655440000", "range 100-200",
])
def test_content_scanner_leaves_ordinary_text_alone(text):
    assert mask_text_content(text) == text


def test_embedded_pii_is_masked_in_an_arbitrarily_named_column():
    out = apply_data_masking({"columns": ["remarks_blob", "wild_alias"],
                              "rows": [{"remarks_blob": "SSN 123-45-6789", "wild_alias": "tel 555-123-4567"}]})
    assert "123-45-6789" not in out["rows"][0]["remarks_blob"]
    assert "555-123-4567" not in out["rows"][0]["wild_alias"]


# ---------------------------------------------------------------- PII-03: sensitivity config

@pytest.mark.parametrize("sql", [
    "SELECT name FROM employees LIMIT 5",
    "SELECT n FROM credit_cards LIMIT 5",
    "SELECT n FROM main.Salaries LIMIT 5",
    "SELECT table_name FROM information_schema.tables LIMIT 5",
    'SELECT x FROM "payroll" LIMIT 5',
    "SELECT tablename FROM pg_catalog.pg_tables LIMIT 5",
])
def test_sensitive_tables_require_human_review(sql):
    res = parse_and_validate_ast(sql)
    assert res.is_valid and res.requires_human and res.risk_level == "HIGH"


def test_ordinary_tables_do_not_trigger_review():
    res = parse_and_validate_ast("SELECT name FROM customers LIMIT 5")
    assert res.is_valid and not res.requires_human


def test_policy_is_read_from_the_config_file(tmp_path, monkeypatch):
    cfg = tmp_path / "policy.json"
    cfg.write_text('{"sensitive_tables": ["customers"]}')
    monkeypatch.setenv("SENSITIVITY_CONFIG", str(cfg))
    assert is_sensitive_table("customers")
    assert not is_sensitive_table("salaries")
    assert parse_and_validate_ast("SELECT name FROM customers LIMIT 5").requires_human


def test_every_table_named_in_the_docs_is_in_the_config():
    patterns = set(load_sensitive_patterns())
    for table in ["salaries", "employees", "compensation", "payroll", "credentials"]:
        assert table in patterns


# ---------------------------------------------------------------- PII-04: read-only sessions

def test_non_sqlite_sessions_are_forced_read_only():
    assert any("READ ONLY" in s for s in session_read_only_statements("postgresql"))
    assert any("READ ONLY" in s for s in session_read_only_statements("mysql"))
    assert session_read_only_statements("sqlite") == []


def test_sqlite_readonly_engine_rejects_writes_at_the_engine_level():
    # Bypass the regex guard in execute_readonly_query to prove the engine itself refuses.
    from sqlalchemy import text
    from app.db.database import readonly_engine
    with pytest.raises(Exception):
        with readonly_engine.connect() as conn:
            conn.execute(text("INSERT INTO customers (name) VALUES ('x')"))
            conn.commit()


# ---------------------------------------------------------------- star projections

def test_star_over_an_aliased_subquery_is_still_masked():
    result = run("SELECT * FROM (SELECT salary AS pay, employee_name FROM salaries) t")
    assert set(cells(result, "pay")) == {"[CONFIDENTIAL]"}
    assert "Eve Adams" in cells(result, "employee_name")


def test_plain_star_and_qualified_star_still_mask_by_column():
    assert set(cells(run("SELECT * FROM salaries"), "salary")) == {"[CONFIDENTIAL]"}
    joined = run("SELECT c.*, o.amount FROM customers c JOIN orders o ON c.customer_id = o.customer_id")
    assert all("*" in e for e in cells(joined, "email"))
    assert set(cells(joined, "amount")) <= {1200, 3400, 800, 5600, 2300}


# ---------------------------------------------------------------- findings of the PII benchmark (BEN-05)

from app.utils.masking import luhn_valid, mask_email, mask_phone, mask_ssn  # noqa: E402


def test_luhn_checksum():
    assert luhn_valid("4111111111111111") and luhn_valid("378282246310005") and luhn_valid("5555555555554444")
    assert not luhn_valid("4111111111111112") and not luhn_valid("1234567812345678")


@pytest.mark.parametrize("text,hidden", [
    ("Refund sent to card 4111111111111111.", "4111111111111111"),
    ("Amex on file 378282246310005 expires soon", "378282246310005"),
    ("Pay with 5105 1051 0510 5100 today", "5105 1051 0510"),
    ("Emergency card backup: 4111-2222-3333-4444", "4111-2222-3333"),          # not Luhn-valid but a card keyword precedes it
])
def test_card_numbers_in_text_are_masked(text, hidden):
    assert hidden not in mask_text_content(text)


@pytest.mark.parametrize("text", [
    "Tracking 1234 5678 9012 3456 shipped",              # 16 digits, fails Luhn, no card keyword
    "Order ORD-905-42-6396 confirmed",                    # SSN-shaped number inside an identifier
    "Build 10.12.0.1234 deployed on 2025-01-05",
    "Invoice total 1,234,567.89",
])
def test_lookalikes_are_left_alone(text):
    assert mask_text_content(text) == text


@pytest.mark.parametrize("text,hidden", [
    ("Identity verified with SSN 905-42-6396 today", "905-42-6396"),
    ("SSN: 905426396", "905426396"),
    ("social security number 905 42 6396", "905 42 6396"),
    ("ssn 905.42.6396 on file", "905.42.6396"),
])
def test_ssns_are_found_by_shape_and_by_keyword(text, hidden):
    assert hidden not in mask_text_content(text)


@pytest.mark.parametrize("text,hidden", [
    ("call +44 20 7946 0958 now", "7946 0958"), ("reach me at (415) 010-2323", "415) 010"), ("tel +1 415 010 2323", "415 010"),
])
def test_international_and_us_phone_numbers_are_masked(text, hidden):
    out = mask_text_content(text)
    assert hidden not in out and out != text


def test_fragments_of_identifiers_are_fully_masked_but_flags_and_prose_are_not():
    assert mask_ssn("905") == "***" and mask_phone("4038") == "****"        # SUBSTR(ssn, 1, 3)
    assert mask_ssn(1) == 1 and mask_ssn(True) is True and mask_phone(0) == 0     # ssn_verified = 1
    assert mask_phone("iPhone 15") == "iPhone 15"                                 # phone_model
    assert mask_ssn("905-42-6396") == "***-**-6396" and mask_ssn(123456789) == "*****6789"


def test_a_value_with_several_addresses_masks_each_one():
    out = mask_email("alice@example.com | bob.smith@example.org")
    assert "alice" not in out and "bob.smith" not in out and out.count("@") == 2


def test_masking_is_idempotent():
    for text in ("mail a.b@example.com now", "SSN 905-42-6396 and card 4111111111111111", "(415) 010-2323"):
        once = mask_text_content(text)
        assert mask_text_content(once) == once


@pytest.mark.parametrize("column", ["token_count", "tokens_used", "max_tokens", "author", "company"])
def test_harmless_columns_with_sensitive_sounding_names_are_not_redacted(column):
    out = apply_data_masking({"columns": [column], "rows": [{column: 42}]})
    assert out["rows"][0][column] == 42 or out["rows"][0][column] == "42"


@pytest.mark.parametrize("column", ["token", "access_token", "refresh_token", "password", "api_key", "secret", "password_hash"])
def test_real_secret_columns_are_still_redacted(column):
    out = apply_data_masking({"columns": [column], "rows": [{column: "abc123"}]})
    assert out["rows"][0][column] == "[REDACTED]"


def test_identifier_columns_are_not_scanned_for_embedded_pii():
    row = {"sku": "555-010-1234", "order_ref": "ORD-905-42-6396", "tracking_no": "4111 1111 1111 1111", "notes": "call (415) 010-2323"}
    out = apply_data_masking({"columns": list(row), "rows": [row]})["rows"][0]
    assert out["sku"] == row["sku"] and out["order_ref"] == row["order_ref"]
    assert "415) 010" not in out["notes"]                       # free text is still scanned


def test_a_phone_column_is_masked_even_if_its_name_looks_like_an_id():
    out = apply_data_masking({"columns": ["phone_no"], "rows": [{"phone_no": "555-010-1234"}]})
    assert out["rows"][0]["phone_no"] == "***-***-1234"


@pytest.mark.parametrize("sql", [
    "SELECT REPLACE(notes, ' ', '') FROM support_tickets",
    "SELECT TRANSLATE(comments, '-', '') FROM t",
    "SELECT REPLACE(REPLACE(description, '-', ''), ' ', '') AS d FROM products",
])
def test_stripping_characters_from_free_text_is_sent_for_review(sql):
    res = parse_and_validate_ast(sql + " LIMIT 5")
    assert res.is_valid and res.requires_human and "defeat PII masking" in res.reason


def test_replace_on_ordinary_columns_is_not_flagged():
    res = parse_and_validate_ast("SELECT REPLACE(name, 'a', 'b') FROM customers LIMIT 5")
    assert res.is_valid and not res.requires_human
