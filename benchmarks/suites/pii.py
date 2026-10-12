"""
PII masking suite.

Question answered: when a query returns personal data, how much of it still leaves the system, and how
much harmless data is masked by mistake?

Method. A probe table is created in an isolated copy of the demo database. Every value in it is
generated from a fixed seed *together with its label*, so ground truth does not come from the masker:
  * PII cells: e-mail addresses, US SSNs (4 formats), phone numbers (6 formats incl. international),
    payment-card numbers (5 formats), and free-text notes with 0-3 PII items embedded in sentences;
  * decoys: values that merely look like PII (order references that embed an SSN-shaped number, 16-digit
    tracking numbers that fail the Luhn check, SKUs shaped like phone numbers, ZIP+4, IPs, ISBNs, UUIDs,
    dates, version strings) and *columns whose names* sound sensitive but hold harmless data (phone_model,
    token_count, email_opt_in, ssn_verified, card_type).
Each PII column is then queried in many ways, several designed to defeat name-based masking: aliases,
functions, substrings, concatenation, CASE, sub-queries, CTEs, UNIONs, aggregates, window functions.
Every query runs through the production path (`execute_readonly_query`: policy-free read + masking).

Before/after: the suite measures the CURRENT masking and policy, and, on the identical probe and queries,
the masking and policy as they were before this benchmark was first run (`legacy_masking()` swaps the old
rules back in). Both numbers therefore come from one method.

Metrics
  span recall      share of embedded PII strings that no longer appear verbatim in the output
                   (the last four digits staying visible is the designed behaviour and is not a leak)
  cell precision   of the cells the masker changed, the share that really held PII
  decoy over-mask  share of decoy cells that were changed although they held no PII
  leaks            the exact (query, original, output) triples that exposed PII
"""

import contextlib
import json
import os
import random
import re
import sqlite3
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from benchmarks.lib.meta import run_metadata
from benchmarks.lib.stats import proportion
from benchmarks.suites.common import load_result, save_result, workspace

SEED = 20261010

FIRST = ["Ava", "Liam", "Noah", "Emma", "Mia", "Lucas", "Zoe", "Ethan", "Priya", "Mateo", "Sofia", "Hugo", "Lena", "Omar", "Yuki", "Nina"]
LAST = ["Nguyen", "Okafor", "Silva", "Muller", "Haddad", "Ivanova", "Tanaka", "Brooks", "Castillo", "Dubois", "Mehta", "Rossi"]
# Published payment-processor test numbers (valid Luhn) in different brands
CARDS = ["4111111111111111", "4012888888881881", "5555555555554444", "5105105105105100", "378282246310005", "6011111111111117"]


def luhn_valid(number: str) -> bool:
    digits = [int(c) for c in number if c.isdigit()]
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            d -= 9 if d > 9 else 0
        total += d
    return total % 10 == 0


def luhn_invalid_16(rng: random.Random) -> str:
    while True:
        n = "".join(rng.choice("0123456789") for _ in range(16))
        if not luhn_valid(n):
            return n


def make_probe(rng: random.Random, n_rows: int = 48) -> Tuple[List[Dict[str, Any]], Dict[Tuple[int, str], List[Tuple[str, str]]]]:
    """Rows plus truth: {(row_id, column): [(pii_type, exact_string), ...]} (empty list = decoy/harmless)."""
    rows, truth = [], {}
    for i in range(1, n_rows + 1):
        name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
        email = f"{name.split()[0].lower()}.{name.split()[1].lower()}{rng.randrange(10, 99)}@mail.example.org"
        a, b, c = rng.randrange(900, 999), rng.randrange(10, 99), rng.randrange(1000, 9999)
        ssn = rng.choice([f"{a}-{b}-{c}"] * 6 + [f"{a}{b}{c}", f"{a} {b} {c}", f"{a}.{b}.{c}"])
        p1, p2 = rng.randrange(200, 999), rng.randrange(1000, 9999)
        phone = rng.choice([f"({p1}) 010-{p2}", f"{p1}-010-{p2}", f"+1 {p1} 010 {p2}", f"{p1}.010.{p2}", f"{p1}010{p2}", "+44 20 7946 0958"])
        card = rng.choice(CARDS)
        card = rng.choice([card, " ".join(card[k:k + 4] for k in range(0, len(card), 4)), "-".join(card[k:k + 4] for k in range(0, len(card), 4))])

        # free-text notes with a known set of embedded PII strings
        spans: List[Tuple[str, str]] = []
        parts = ["Customer called about a delayed delivery."]
        for _ in range(rng.choice([0, 1, 1, 2, 3])):
            kind = rng.choice(["email", "ssn", "phone", "card"])
            if kind == "email":
                v = f"contact{rng.randrange(100, 999)}@example.net"
                parts.append(f"Reply to {v} once resolved.")
            elif kind == "ssn":
                v = f"{rng.randrange(900, 999)}-{rng.randrange(10, 99)}-{rng.randrange(1000, 9999)}"
                parts.append(f"Identity verified with SSN {v}.")
            elif kind == "phone":
                v = f"({rng.randrange(200, 999)}) 010-{rng.randrange(1000, 9999)}"
                parts.append(f"Call back on {v} after 5pm.")
            else:
                v = rng.choice(CARDS)
                parts.append(f"Refund sent to card {v}.")
            spans.append((kind, v))
        notes = " ".join(parts)

        row = {
            "id": i, "full_name": name, "email": email, "ssn": ssn, "phone": phone, "card_number": card, "notes": notes,
            # decoys: harmless values that look like PII
            "order_ref": f"ORD-{a}-{b}-{c}",                                  # embeds an SSN-shaped number
            "tracking_no": " ".join(luhn_invalid_16(rng)[k:k + 4] for k in range(0, 16, 4)),   # 16 digits, fails Luhn
            "sku": f"555-010-{rng.randrange(1000, 9999)}",                    # phone-shaped product code
            "ip_addr": f"192.168.{rng.randrange(0, 255)}.{rng.randrange(0, 255)}",
            "zip4": f"{rng.randrange(10000, 99999)}-{rng.randrange(1000, 9999)}",
            "uuid": f"{rng.getrandbits(32):08x}-{rng.getrandbits(16):04x}-4{rng.getrandbits(12):03x}-a{rng.getrandbits(12):03x}-{rng.getrandbits(48):012x}",
            "doc_date": f"2025-{rng.randrange(1, 13):02d}-{rng.randrange(1, 28):02d}",
            "salary": float(rng.randrange(40, 200) * 1000),
            # harmless data in columns whose NAMES sound sensitive
            "phone_model": rng.choice(["iPhone 15", "Pixel 8", "Galaxy S24"]),
            "token_count": rng.randrange(10, 5000),
            "email_opt_in": rng.randrange(0, 2),
            "ssn_verified": rng.randrange(0, 2),
            "card_type": rng.choice(["Visa", "Mastercard", "Amex"]),
        }
        rows.append(row)
        truth[(i, "email")] = [("email", email)]
        truth[(i, "ssn")] = [("ssn", ssn)]
        truth[(i, "phone")] = [("phone", phone)]
        truth[(i, "card_number")] = [("card", card)]
        truth[(i, "notes")] = spans
        truth[(i, "salary")] = [("salary", str(int(row["salary"])))]
        for decoy in ("order_ref", "tracking_no", "sku", "ip_addr", "zip4", "uuid", "doc_date", "phone_model", "token_count",
                      "email_opt_in", "ssn_verified", "card_type", "full_name"):
            truth[(i, decoy)] = []
    return rows, truth


def create_probe(db_file: str, rows: List[Dict[str, Any]]) -> None:
    con = sqlite3.connect(db_file)
    cols = list(rows[0].keys())
    types = {"id": "INTEGER PRIMARY KEY", "salary": "REAL", "token_count": "INTEGER", "email_opt_in": "INTEGER", "ssn_verified": "INTEGER"}
    con.execute(f"CREATE TABLE pii_probe ({', '.join(f'{c} {types.get(c, chr(84)+chr(69)+chr(88)+chr(84))}' for c in cols)})")
    con.executemany(f"INSERT INTO pii_probe ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", [tuple(r[c] for c in cols) for r in rows])
    con.commit()
    con.close()


# ---------------------------------------------------------------------------- query shapes

def shapes(col: str) -> List[Tuple[str, str]]:
    """(technique, SQL) pairs that return the PII column `col` in different disguises."""
    t = "pii_probe"
    return [
        ("direct", f"SELECT id, {col} FROM {t}"),
        ("alias", f"SELECT id, {col} AS harmless_label FROM {t}"),
        ("upper", f"SELECT id, UPPER({col}) AS v FROM {t}"),
        ("trim_lower", f"SELECT id, LOWER(TRIM({col})) AS v FROM {t}"),
        ("concat", f"SELECT id, {col} || '' AS v FROM {t}"),
        ("replace", f"SELECT id, REPLACE(REPLACE({col}, '-', ''), ' ', '') AS v FROM {t}"),
        ("substr_head", f"SELECT id, SUBSTR({col}, 1, 3) AS v FROM {t}"),
        ("case", f"SELECT id, CASE WHEN id > 0 THEN {col} ELSE NULL END AS v FROM {t}"),
        ("coalesce", f"SELECT id, COALESCE({col}, 'n/a') AS v FROM {t}"),
        ("subquery", f"SELECT id, v FROM (SELECT id, {col} AS v FROM {t}) s"),
        ("cte", f"WITH c AS (SELECT id, {col} AS v FROM {t}) SELECT id, v FROM c"),
        ("union", f"SELECT id, {col} AS v FROM {t} UNION ALL SELECT id, {col} FROM {t} WHERE id < 5"),
        ("window", f"SELECT id, MAX({col}) OVER (PARTITION BY id) AS v FROM {t}"),
        ("aggregate_max", f"SELECT MAX({col}) AS v FROM {t}"),
        ("group_concat", f"SELECT GROUP_CONCAT({col}, ' | ') AS v FROM {t} WHERE id <= 6"),
        ("self_join", f"SELECT a.id, b.{col} AS v FROM {t} a JOIN {t} b ON a.id = b.id"),
    ]


# Query shapes whose output must equal the stored value when that value is not PII, so "the output changed"
# really means "it was masked". REPLACE/UPPER/SUBSTR/aggregates legitimately change values and are excluded
# from precision and over-masking (but still count for recall and fragment exposure).
IDENTITY_SHAPES = {"direct", "alias", "case", "coalesce", "subquery", "cte", "union", "self_join", "window"}


@contextlib.contextmanager
def legacy_masking():
    """
    The masking and policy rules as they stood before the PII benchmark's findings were fixed: SSN/card/phone
    scanning without boundary or Luhn checks, `token` matching any column containing it, no fragment guard,
    no identifier-column exemption, single-address e-mail masking, and no REPLACE-evasion rule.
    """
    from app.utils import masking, sql_parser

    saved = {name: getattr(masking, name) for name in ("mask_email", "mask_ssn", "mask_phone", "mask_credit_card", "mask_text_content",
                                                        "COLUMN_MASK_RULES", "_ID_LIKE", "_keep_last4")}
    saved_evasion = sql_parser._masking_evasion_columns

    def old_keep_last4(value):
        return re.sub(r"\d(?=(?:\D*\d){4})", "*", value)

    def old_email(email):
        if not isinstance(email, str) or "@" not in email:
            return email
        parts = email.split("@")
        name = parts[0]
        name = name[0] + "*" * (len(name) - 2) + name[-1] if len(name) > 2 else "*" * len(name)
        return f"{name}@{parts[1]}"

    def old_ssn(v):
        return None if v is None else old_keep_last4(str(v).strip())

    def old_card(v):
        if v is None:
            return None
        text = str(v).strip()
        return text if len(re.findall(r"\d", text)) < 12 else old_keep_last4(text)

    def old_text(text):
        if not isinstance(text, str):
            return text
        text = re.sub(r"([a-zA-Z0-9_.+-])[a-zA-Z0-9_.+-]*([a-zA-Z0-9_.+-])@([a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)", r"\1***\2@\3", text)
        text = re.sub(r"\b\d{3}-\d{2}-(\d{4})\b", r"***-**-\1", text)
        text = re.sub(r"\b(?:\d{4}[ -]?){3}(\d{4})\b", r"****-****-****-\1", text)
        text = re.sub(r"(?<![\w-])(?:\+?1[ .-]?)?(?:\(\d{3}\)\s?|\d{3}[ .-])\d{3}[ .-](\d{4})(?![\w-])", r"***-***-\1", text)
        return text

    old_rules = [
        (re.compile(r"email", re.I), old_email),
        (re.compile(r"(ssn|social.*sec)", re.I), old_ssn),
        (re.compile(r"(phone|mobile|fax|contact_no)", re.I), old_ssn),
        (re.compile(r"(credit.*card|card.*num|cc.*num|(^|_)pan($|_)|account.*no|\bcard\b)", re.I), old_card),
        (re.compile(r"(password|token|secret|api.*key|(^|_)auth($|_)|auth_?(token|key)|hash)", re.I), masking.mask_secret),
        (re.compile(r"(salary|compensation|bonus|payroll|wage)", re.I), masking.mask_financial),
        (re.compile(r"(notes|comments|description|memo|body|details)", re.I), old_text),
    ]
    masking.mask_email, masking.mask_ssn, masking.mask_phone = old_email, old_ssn, old_ssn
    masking.mask_credit_card, masking.mask_text_content, masking.COLUMN_MASK_RULES = old_card, old_text, old_rules
    masking._ID_LIKE, masking._keep_last4 = re.compile(r"(?!x)x"), old_keep_last4
    sql_parser._masking_evasion_columns = lambda ast: []
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(masking, name, value)
        sql_parser._masking_evasion_columns = saved_evasion


def evaluate(db_file: str, rows, truth, label: str) -> Dict[str, Any]:
    from app.db.database import execute_readonly_query
    from app.mcp_server import validate_and_classify_sql

    pii_columns = ["email", "ssn", "phone", "card_number", "notes"]
    by_id = {r["id"]: r for r in rows}
    by_technique: Dict[str, List[int]] = defaultdict(lambda: [0, 0])
    by_type: Dict[str, List[int]] = defaultdict(lambda: [0, 0])
    fragment: Dict[str, List[int]] = defaultdict(lambda: [0, 0])
    leaks: List[Dict[str, Any]] = []
    gated_queries: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    counts = {"pii_cells": 0, "pii_cells_masked": 0, "decoy_cells": 0, "decoy_cells_masked": 0}
    state = {"gated": False}

    def mark_leak(technique, kind, sql, value, out_text):
        by_type[kind][0] += 1
        by_technique[technique][0] += 1
        leaks.append({"technique": technique, "type": kind, "sql": sql[:90], "original": value, "output": out_text[:70], "gated": state["gated"]})

    def score_cell(technique, key, original, output, sql):
        spans = truth.get(key, [])
        out_text = "" if output is None else str(output)
        changed = out_text != ("" if original is None else str(original))
        identity = technique in IDENTITY_SHAPES
        if spans:
            if identity:
                counts["pii_cells"] += 1
                counts["pii_cells_masked"] += changed
            # digit triples that are *allowed* to be visible: they occur in the last four digits of some PII value in the cell
            allowed = set()
            for _, v in spans:
                last4 = re.sub(r"\D", "", v)[-4:]
                allowed |= {last4[i:i + 3] for i in range(len(last4) - 2)}
            for kind, value in spans:
                by_type[kind][1] += 1
                by_technique[technique][1] += 1
                digits = re.sub(r"\D", "", value)
                stripped_out = re.sub(r"[\s.-]", "", out_text)
                if value in out_text or (len(digits) >= 9 and digits in re.sub(r"\D", "", out_text) and digits in stripped_out):
                    mark_leak(technique, kind, sql, value, out_text)
                if kind in ("ssn", "phone") and len(digits) >= 9 and technique != "group_concat":
                    head = digits[:-4]
                    grams = {head[i:i + 3] for i in range(len(head) - 2)} - allowed
                    fragment[technique][1] += 1
                    if any(run[i:i + 3] in grams for run in re.findall(r"\d{3,}", out_text) for i in range(len(run) - 2)):
                        fragment[technique][0] += 1
        elif identity:
            counts["decoy_cells"] += 1
            counts["decoy_cells_masked"] += changed

    for col in pii_columns:
        for technique, sql in shapes(col):
            decision = validate_and_classify_sql(sql)
            state["gated"] = (not decision["safe"]) or decision["requires_human"]
            if state["gated"]:
                gated_queries.append({"technique": technique, "column": col, "reason": decision["reason"][:90]})
            try:
                result = execute_readonly_query(sql)
            except Exception as e:
                errors.append({"sql": sql, "error": str(e)[:80]})
                continue
            for out_row in result["rows"]:
                rid = out_row.get("id")
                cell_key = next((k for k in out_row if k != "id"), None)
                if cell_key is None:
                    continue
                if rid is None:      # aggregate shapes mix several rows: judge every PII string of the covered rows
                    for r in (range(1, 7) if technique == "group_concat" else range(1, len(rows) + 1)):
                        for kind, value in truth.get((r, col), []):
                            by_type[kind][1] += 1
                            by_technique[technique][1] += 1
                            if value in str(out_row[cell_key]):
                                mark_leak(technique, kind, sql, value, str(out_row[cell_key]))
                    continue
                score_cell(technique, (rid, col), by_id[rid][col], out_row[cell_key], sql)

    decoy_columns = ["order_ref", "tracking_no", "sku", "ip_addr", "zip4", "uuid", "doc_date", "phone_model", "token_count",
                     "email_opt_in", "ssn_verified", "card_type", "full_name"]
    decoy_stats: Dict[str, List[int]] = {c: [0, 0] for c in decoy_columns}
    examples = []
    for col in decoy_columns:
        for out_row in execute_readonly_query(f"SELECT id, {col} FROM pii_probe")["rows"]:
            original = by_id[out_row["id"]][col]
            changed = str(out_row[col]) != str(original)
            decoy_stats[col][1] += 1
            decoy_stats[col][0] += changed
            counts["decoy_cells"] += 1
            counts["decoy_cells_masked"] += changed
            if changed and len(examples) < 30:
                examples.append({"column": col, "original": str(original), "output": str(out_row[col])})

    total = sum(v[1] for v in by_type.values())
    leaked = sum(v[0] for v in by_type.values())
    e2e = [l for l in leaks if not l["gated"]]
    e2e_by_technique: Dict[str, int] = defaultdict(int)
    for l in e2e:
        e2e_by_technique[l["technique"]] += 1
    return {
        "label": label,
        "span_recall_masking_only": proportion(total - leaked, total),
        "span_recall_end_to_end": proportion(total - len(e2e), total),
        "recall_by_type_masking_only": {k: proportion(v[1] - v[0], v[1]) for k, v in sorted(by_type.items())},
        "recall_by_technique_masking_only": {k: proportion(v[1] - v[0], v[1]) for k, v in sorted(by_technique.items())},
        "recall_by_technique_end_to_end": {k: proportion(v[1] - e2e_by_technique.get(k, 0), v[1]) for k, v in sorted(by_technique.items())},
        "precision": proportion(counts["pii_cells_masked"], counts["pii_cells_masked"] + counts["decoy_cells_masked"]),
        "over_mask_rate_on_harmless_cells": proportion(counts["decoy_cells_masked"], counts["decoy_cells"]),
        "over_masked_by_column": {c: proportion(v[0], v[1]) for c, v in decoy_stats.items() if v[0]},
        "over_masked_examples": examples[:20],
        "fragment_exposure": {k: proportion(v[0], v[1]) for k, v in sorted(fragment.items())},
        "fragment_exposure_all": proportion(sum(v[0] for v in fragment.values()), sum(v[1] for v in fragment.values())),
        "queries_stopped_by_policy": gated_queries,
        "leak_examples": leaks[:30],
        "n_leaks_masking_only": len(leaks),
        "n_leaks_end_to_end": len(e2e),
        "query_errors": errors,
    }


def run(llm: bool = True, limit: Optional[int] = None) -> Dict[str, Any]:
    rng = random.Random(SEED)
    rows, truth = make_probe(rng)
    with workspace("pii") as db_file:
        create_probe(db_file, rows)
        with legacy_masking():
            before = evaluate(db_file, rows, truth, "before: masking/policy as first benchmarked")
        after = evaluate(db_file, rows, truth, "after: current")
    result = {
        "meta": run_metadata([], {"seed": SEED, "probe_rows": len(rows), "queries": 5 * len(shapes("x")) + 13,
                                  "rules_fingerprint_current": _fingerprint()}),
        "before": before,
        "after": after,
    }
    print("saved", save_result("pii", result))
    return result


def _fingerprint() -> str:
    import hashlib
    from benchmarks.lib.meta import ROOT
    with open(os.path.join(ROOT, "app", "utils", "masking.py"), "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:12]
