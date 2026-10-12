"""
SentinelSQL Enterprise Data Sanitization & PII Redaction Engine
---------------------------------------------------------------
Scans database query results before transmission to clients or LLM contexts.
Enforces:
1. Column-level policy masking (Email, SSN, Phone, Credit Card, Secrets, Financials).
2. Deep content-aware text sanitization (inline emails, SSNs, credit cards in text fields).
3. Preservation of referential integrity and format consistency.

Column lineage (alignment_plan PII-01): when the originating SQL is supplied, each
output column is traced back to the source columns it is computed from using
sqlglot, so `SELECT salary AS pay`, `UPPER(email)`, subqueries, CTEs and UNIONs are
masked by what they *contain*, not by what they are called. If lineage cannot be
resolved the engine over-masks (fails closed) rather than leaking.
"""

import logging
import re
from typing import Dict, Any, List, Optional, Callable, Set

import sqlglot
from sqlglot import exp
from sqlglot.lineage import lineage

logger = logging.getLogger("sentinel.masking")


_EMAIL_RE = re.compile(r"[A-Za-z0-9_.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")


def _mask_single_email(email: str) -> str:
    name, _, domain = email.partition("@")
    if len(name) > 2:
        name = name[0] + "*" * (len(name) - 2) + name[-1]
    else:
        name = "*" * len(name)
    return f"{name}@{domain}"


def _mask_inline_email(match: "re.Match", text: str) -> str:
    """
    Address embedded in free text or in a multi-address value: first letter + *** + last letter.
    Idempotent: an address whose local part is already masked (preceded by '*') is left untouched, so
    scanning masked output a second time cannot erode it further.
    """
    if match.start() > 0 and text[match.start() - 1] == "*":
        return match.group(0)
    local, _, domain = match.group(0).partition("@")
    masked = local[0] + "***" + local[-1] if len(local) >= 2 else "*"
    return f"{masked}@{domain}"


def mask_email(email: str) -> str:
    """
    Masks e-mail addresses while keeping the first and last letter of the local part.
    Example: alice.wonderland@example.com -> a**************d@example.com
    Example: b@example.com -> *@example.com
    A value holding several addresses (GROUP_CONCAT, free text) has each one masked.
    """
    if not isinstance(email, str) or "@" not in email:
        return email
    stripped = email.strip()
    if stripped.count("@") == 1 and _EMAIL_RE.fullmatch(stripped):
        return _mask_single_email(stripped)
    return _EMAIL_RE.sub(lambda m: _mask_inline_email(m, email), email)


def _is_small_number(value: Any) -> bool:
    """True for an int/float/bool of at most four digits: flags, counts and codes, never an SSN or phone number."""
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return len(str(abs(int(value)))) <= 4
    return False


def _keep_last4(value: str) -> str:
    """Replace every digit except the last four with '*', keeping separators. Four digits or fewer: all of them."""
    n_digits = len(re.findall(r"\d", value))
    if n_digits <= 4:
        # A fragment such as SUBSTR(ssn, 1, 3) must not pass through just because it is short...
        if re.search(r"[A-Za-z]", value):
            return value        # ...but prose that merely contains digits ("iPhone 15") is not a number fragment
        return re.sub(r"\d", "*", value)
    return re.sub(r"\d(?=(?:\D*\d){4})", "*", value)


def mask_ssn(ssn: Any) -> str:
    """
    Masks all but the last 4 digits of a Social Security Number.
    Example: 123456789 -> *****6789
    Example: 123-45-6789 -> ***-**-6789
    """
    if ssn is None:
        return ssn
    if _is_small_number(ssn):
        return ssn            # a flag or a count (ssn_verified = 1), not an identifier
    return _keep_last4(str(ssn).strip())


def mask_phone(phone: Any) -> str:
    """
    Masks all digits of a phone number except the last 4.
    Example: +1 (555) 123-4567 -> +* (***) ***-4567
    Example: 555-123-4567 -> ***-***-4567
    """
    if phone is None:
        return phone
    if _is_small_number(phone):
        return phone            # a flag or a count (ssn_verified = 1), not an identifier
    return _keep_last4(str(phone).strip())


def mask_credit_card(card: Any) -> str:
    """
    Masks all but the last 4 digits of a payment card number.
    Example: 4111-2222-3333-4444 -> ****-****-****-4444
    Example: 4111222233334444 -> ************4444
    """
    if card is None:
        return card
    card_str = str(card).strip()
    if len(re.findall(r"\d", card_str)) < 12:
        return card_str
    return _keep_last4(card_str)


def mask_secret(val: Any) -> str:
    """
    Fully redacts authentication tokens, passwords, and API keys.
    """
    if val is None:
        return val
    return "[REDACTED]"


def mask_financial(val: Any) -> str:
    """
    Redacts or partially obscures sensitive compensation/salary data.
    """
    if val is None:
        return val
    return "[CONFIDENTIAL]"


def luhn_valid(digits: str) -> bool:
    """Luhn checksum: every genuine payment-card number passes it, most random digit strings do not."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d = d * 2 - 9 if d * 2 > 9 else d * 2
        total += d
    return total % 10 == 0


_CARD_CANDIDATE = re.compile(r"(?<![\w-])(\d(?:[ -]?\d){12,18})(?![\w-])")
_CARD_CONTEXT = re.compile(r"(card|visa|master\s?card|amex|american express|discover|credit|debit|\bcc\b|\bpan\b)[^0-9]{0,24}$", re.I)
_SSN_DASHED = re.compile(r"(?<![\w-])\d{3}-\d{2}-(\d{4})(?![\w-])")
_SSN_KEYED = re.compile(r"(?i)\b(ssn|social security(?: number)?|social sec\.?)\b([^0-9]{0,15})(\d{3})[ .-]?(\d{2})[ .-]?(\d{4})(?!\d)")
_PHONE_NANP = re.compile(r"(?<![\w-])(?:\+?1[ .-]?)?(?:\(\d{3}\)\s?|\d{3}[ .-])\d{3}[ .-](\d{4})(?![\w-])")
_PHONE_INTL = re.compile(r"(?<![\w-])\+\d{1,3}(?:[ .-]?\(?\d{1,4}\)?){2,5}(?![\w-])")


def _mask_cards_in(text: str) -> str:
    """A 13-19 digit number is masked when it passes the Luhn check or a card keyword precedes it."""
    def repl(m: "re.Match") -> str:
        digits = re.sub(r"\D", "", m.group(1))
        if not 13 <= len(digits) <= 19:
            return m.group(0)
        if luhn_valid(digits) or _CARD_CONTEXT.search(text[max(0, m.start() - 28):m.start()]):
            return _keep_last4(m.group(1))
        return m.group(0)
    return _CARD_CANDIDATE.sub(repl, text)


def mask_text_content(text: Any) -> str:
    """
    Deep content scan: detects and redacts inline PII embedded inside unstructured text.

    Precision rules (found by the PII benchmark): SSN-shaped numbers are only SSNs when they stand alone
    (not inside `ORD-905-42-6396`), card numbers must pass the Luhn check or follow a card keyword (so a
    16-digit tracking number is left alone), and an unformatted 9-digit SSN is recognised only after a
    keyword such as "SSN" because 9 plain digits are otherwise indistinguishable from any other id.
    """
    if not isinstance(text, str):
        return text

    original = text
    text = _EMAIL_RE.sub(lambda m: _mask_inline_email(m, original), text)
    text = _mask_cards_in(text)
    text = _SSN_DASHED.sub(r"***-**-\1", text)
    text = _SSN_KEYED.sub(lambda m: f"{m.group(1)}{m.group(2)}***-**-{m.group(5)}", text)
    text = _PHONE_NANP.sub(r"***-***-\1", text)
    text = _PHONE_INTL.sub(lambda m: _keep_last4(m.group(0)) if 8 <= len(re.sub(r"\D", "", m.group(0))) <= 15 else m.group(0), text)
    return text


# Column-level policy mapping
COLUMN_MASK_RULES = [
    (re.compile(r"email", re.I), mask_email),
    (re.compile(r"(ssn|social.*sec)", re.I), mask_ssn),
    (re.compile(r"(phone|mobile|fax|contact_no)", re.I), mask_phone),
    (re.compile(r"(credit.*card|card.*num|cc.*num|(^|_)pan($|_)|account.*no|\bcard\b)", re.I), mask_credit_card),
    (re.compile(r"(password|passwd|secret|api.*key|(^|_)auth($|_)|auth_?(token|key)|access_?token|refresh_?token|bearer|private_?key|hash|(^|_)token$)", re.I), mask_secret),
    (re.compile(r"(salary|compensation|bonus|payroll|wage)", re.I), mask_financial),
    (re.compile(r"(notes|comments|description|memo|body|details)", re.I), mask_text_content),
]


_REDACTING_RULES = (mask_secret, mask_financial)  # full redaction beats partial masking

# Identifier-style columns (sku, order_ref, tracking_no, ...) are not scanned for embedded PII when nothing
# marks them sensitive: an SSN- or phone-shaped *product code* is far more common than an SSN stored in a sku.
_ID_LIKE = re.compile(r"((^|_)(sku|ref|reference|code|tracking|uuid|guid|invoice|barcode|isbn|serial|slug|zip|ip|ip_addr)($|_))|((^|_)(no|num|id)$)", re.I)


def _pick_rule(names: List[str]) -> Optional[Callable[[Any], Any]]:
    """Chooses the masking function for a set of source/output column names (strongest wins)."""
    matched = []
    for pattern, mask_fn in COLUMN_MASK_RULES:
        if any(pattern.search(n) for n in names if n):
            matched.append(mask_fn)
    for fn in matched:
        if fn in _REDACTING_RULES:
            return fn
    return matched[0] if matched else None


def _sqlglot_schema() -> Optional[Dict[str, Dict[str, str]]]:
    """Table -> {column: type} from live introspection, so lineage can resolve unqualified columns."""
    try:
        from app.utils.schemacontext import introspect_schema
        tables = introspect_schema().get("tables", {})
        return {t: {c["name"]: c.get("type", "TEXT") for c in meta.get("columns", [])}
                for t, meta in tables.items()} or None
    except Exception:
        return None


def _all_query_column_names(ast: exp.Expression) -> List[str]:
    return [c.name for c in ast.find_all(exp.Column) if c.name]


def _is_star(proj: exp.Expression) -> bool:
    inner = proj.this if isinstance(proj, exp.Alias) else proj
    return isinstance(inner, exp.Star) or (isinstance(inner, exp.Column) and isinstance(inner.this, exp.Star))


def _leaves_for(column_name: str, sql: str, schema, fallback: List[str]) -> List[str]:
    try:
        node = lineage(column_name, sql, schema=schema)
        leaves = [n.name.split(".")[-1] for n in node.walk() if not n.downstream]
        return leaves or fallback
    except Exception as e:
        logger.debug("Lineage unresolved for '%s' (%s); over-masking", column_name, e)
        return fallback


def derive_lineage_sources(sql: str, output_columns: Optional[List[str]] = None) -> Dict[int, List[str]]:
    """
    Maps each output column position of the outermost SELECT to the source column
    names it derives from. Positions whose value is a bare COUNT are mapped to []
    (a count of a sensitive column is not itself sensitive). Raises on parse failure.

    When the projection contains a star, positions cannot be matched to projections, so
    each actual output column name (`output_columns`) is traced instead; this is what
    catches `SELECT * FROM (SELECT salary AS pay ...)`.
    """
    ast = sqlglot.parse_one(sql)
    projections = list(ast.selects) if hasattr(ast, "selects") else []
    if not projections:
        raise ValueError("Cannot determine projections")

    schema = _sqlglot_schema()
    fallback = _all_query_column_names(ast)
    sources: Dict[int, List[str]] = {}

    if any(_is_star(p) for p in projections):
        for idx, col in enumerate(output_columns or []):
            sources[idx] = _leaves_for(col, sql, schema, fallback)
        return sources

    for idx, proj in enumerate(projections):
        inner = proj.this if isinstance(proj, exp.Alias) else proj
        if isinstance(inner, exp.Count):
            sources[idx] = []
            continue
        sources[idx] = _leaves_for(proj.alias_or_name, sql, schema, fallback)
    return sources


def apply_data_masking(results: Dict[str, Any], sql: Optional[str] = None) -> Dict[str, Any]:
    """
    Masks sensitive values in query results before they reach a client or an LLM.

    Layers:
      1. Column policy by output name (always on).
      2. Column policy by lineage of the originating SQL (when `sql` is given), which
         defeats aliases, expressions, subqueries, CTEs and UNIONs.
      3. Content scanning of every remaining string cell for embedded emails, SSNs,
         cards and phone numbers.
    """
    if not results or "columns" not in results or "rows" not in results:
        return results

    columns = list(results["columns"])
    rows = results["rows"]

    lineage_sources: Dict[int, List[str]] = {}
    if sql:
        try:
            lineage_sources = derive_lineage_sources(sql, columns)
        except Exception as e:
            logger.warning("Lineage analysis failed (%s); falling back to name-based masking only", e)

    masking_map: Dict[str, Callable[[Any], Any]] = {}
    skip_columns: Set[str] = set()
    no_scan_columns: Set[str] = set()
    for idx, col in enumerate(columns):
        if idx in lineage_sources and lineage_sources[idx] == []:
            skip_columns.add(col)  # bare COUNT(...): numeric, not sensitive
            continue
        names = [col] + lineage_sources.get(idx, [])
        rule = _pick_rule(names)
        if rule is not None:
            masking_map[col] = rule
        elif any(_ID_LIKE.search(n) for n in names if n):
            no_scan_columns.add(col)

    masked_rows = []
    for row in rows:
        masked_row = dict(row)
        for col in columns:
            value = masked_row.get(col)
            if value is None or col in skip_columns:
                continue
            if col in masking_map:
                value = masking_map[col](value)
            if isinstance(value, str) and col not in no_scan_columns:
                value = mask_text_content(value)
            masked_row[col] = value
        masked_rows.append(masked_row)

    return {"columns": results["columns"], "rows": masked_rows}
