"""
SentinelSQL AST Query Validation & Risk Engine
---------------------------------------------
Uses sqlglot to parse incoming SQL into an Abstract Syntax Tree (AST).
Provides deterministic query governance:
1. Rejects multiple statements (stacking attacks).
2. Guarantees root statement is strictly a SELECT (no DDL/DML in CTEs or subqueries).
3. Detects referenced tables and classifies risk tiers.
4. Injects a safety LIMIT clause if none was provided.
"""

import fnmatch
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import List, Optional
import sqlglot
from sqlglot import exp

# Sensitivity tiers live in app/policy/sensitivity.json (alignment_plan PII-03).
_DEFAULT_SENSITIVITY_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "policy", "sensitivity.json")


@lru_cache(maxsize=8)
def _read_policy_list(path: str, mtime: float, key: str) -> tuple:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return tuple(str(p).strip().lower() for p in data.get(key, []) if str(p).strip())


def _policy_list(key: str) -> List[str]:
    """Reads a list from the policy file (re-read when the file changes). A missing or broken file raises."""
    path = os.getenv("SENSITIVITY_CONFIG", _DEFAULT_SENSITIVITY_PATH)
    return list(_read_policy_list(path, os.path.getmtime(path), key))


def load_sensitive_patterns() -> List[str]:
    return _policy_list("sensitive_tables")


def load_blocked_functions() -> List[str]:
    return _policy_list("blocked_functions")


@lru_cache(maxsize=4)
def _fingerprint(path: str, mtime: float, limits: tuple) -> str:
    with open(path, "rb") as f:
        digest = hashlib.sha256(f.read())
    digest.update(repr(limits).encode("utf-8"))
    return digest.hexdigest()[:12]


def get_policy_fingerprint() -> str:
    """
    Short hash of everything that decides what a query may do (the policy file and the row limits).
    The semantic cache stores it with each entry, so tightening a policy immediately stops previously
    cached answers from being served (alignment_plan CAC-03).
    """
    path = os.getenv("SENSITIVITY_CONFIG", _DEFAULT_SENSITIVITY_PATH)
    return _fingerprint(path, os.path.getmtime(path), (DEFAULT_SAFETY_LIMIT, MAX_PERMITTED_LIMIT))


_STRIPPING_FUNCTIONS = {"replace", "translate", "regexp_replace"}
_FREE_TEXT_COLUMN = re.compile(r"(notes?|comments?|description|memo|body|details|message|remarks?|text)", re.I)


def _masking_evasion_columns(ast: exp.Expression) -> List[str]:
    """Free-text columns that are passed through REPLACE/TRANSLATE/REGEXP_REPLACE."""
    found = []
    for func in ast.find_all(exp.Func):
        if _function_name(func) in _STRIPPING_FUNCTIONS or isinstance(func, exp.RegexpReplace):
            for col in func.find_all(exp.Column):
                if _FREE_TEXT_COLUMN.search(col.name or ""):
                    found.append(col.name.lower())
    return sorted(set(found))


def _function_name(node: exp.Func) -> str:
    return (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).lower()


_COMPARISONS = {exp.EQ: lambda x, y: x == y, exp.NEQ: lambda x, y: x != y, exp.GT: lambda x, y: x > y,
                exp.GTE: lambda x, y: x >= y, exp.LT: lambda x, y: x < y, exp.LTE: lambda x, y: x <= y}


def _literal_value(node: exp.Expression):
    if isinstance(node, exp.Literal):
        return float(node.this) if not node.is_string else str(node.this)
    if isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal) and not node.this.is_string:
        return -float(node.this.this)
    return None


def _is_tautology(node: exp.Expression) -> bool:
    """
    True for a condition that is always true by construction: TRUE, a non-zero number, a comparison between
    two literals that holds (1=1, 'a'='a', 2>1, 1<>2), or a column compared with itself (id = id).
    """
    if isinstance(node, exp.Boolean):
        return bool(node.this)
    if isinstance(node, exp.Paren):
        return _is_tautology(node.this)
    if isinstance(node, exp.Not):
        inner = node.this.this if isinstance(node.this, exp.Paren) else node.this
        return isinstance(inner, exp.Boolean) and not inner.this
    if isinstance(node, exp.Literal) and not node.is_string:
        return float(node.this) != 0
    for kind, holds in _COMPARISONS.items():
        if isinstance(node, kind):
            left, right = _literal_value(node.left), _literal_value(node.right)
            if left is not None and right is not None and type(left) is type(right):
                return holds(left, right)
            if isinstance(node, exp.EQ) and isinstance(node.left, exp.Column) and node.left == node.right:
                return True
    return False


def _normalize_identifier(name: str) -> str:
    return (name or "").strip().strip('`"[]').lower()


def is_sensitive_table(name: str, schema: str = "") -> bool:
    """True if the bare or schema-qualified table name matches any configured pattern."""
    bare = _normalize_identifier(name)
    qualified = f"{_normalize_identifier(schema)}.{bare}" if schema else bare
    candidates = {bare, qualified}
    if schema:
        candidates.add(_normalize_identifier(schema))
    return any(fnmatch.fnmatchcase(c, pat) for pat in load_sensitive_patterns() for c in candidates)


DESTRUCTIVE_EXPRESSION_TYPES = (
    exp.Drop,
    exp.Delete,
    exp.Update,
    exp.Insert,
    exp.Alter,
    exp.Create,
    exp.Command,
    exp.Merge,
    exp.Pragma,
)

# MAX_ROWS is the LIMIT injected when a query has none; MAX_RESULT_ROWS is the ceiling a
# query may request (and the cap applied at execution). Both are read from the environment.
DEFAULT_SAFETY_LIMIT = int(os.getenv("MAX_ROWS", "100"))
MAX_PERMITTED_LIMIT = int(os.getenv("MAX_RESULT_ROWS", "1000"))


# UNION, INTERSECT and EXCEPT are all set operations over SELECTs (sqlglot: SetOperation). The first
# policy version only knew UNION and rejected INTERSECT/EXCEPT as "destructive" (found by the held-out set).
SET_OPERATION = getattr(exp, "SetOperation", exp.Union)
QUERY_ROOTS = (exp.Select, SET_OPERATION)


def _root_selects(node: exp.Expression) -> List[exp.Select]:
    """The SELECT branches of the outermost query (more than one for UNION / INTERSECT / EXCEPT)."""
    if isinstance(node, exp.Select):
        return [node]
    if isinstance(node, SET_OPERATION):
        return _root_selects(node.left) + _root_selects(node.right)
    return []


def _walk_outside_subqueries(node: exp.Expression):
    """Nodes of a projection, not descending into scalar sub-queries (their aggregates are their own)."""
    return node.walk(prune=lambda n: isinstance(n, (exp.Subquery, exp.Select)) and n is not node)


def _classify_projection(proj: exp.Expression):
    """
    (has_aggregate, has_bare_column) for one projection, or None when it should not be classified:
      * window functions (`SUM(x) OVER (...)`) are per-row, not aggregation;
      * a constant (`'note'`, `1`) next to an aggregate is valid SQL;
      * aggregates inside a scalar sub-query belong to that sub-query.
    """
    nodes = list(_walk_outside_subqueries(proj))
    if any(isinstance(n, exp.Window) for n in nodes):
        return None
    has_agg = any(isinstance(n, exp.AggFunc) for n in nodes)
    has_bare = any(isinstance(n, exp.Column) and not n.find_ancestor(exp.AggFunc) for n in nodes)
    return has_agg, has_bare


def _find_wildcard_projection(node: exp.Expression) -> Optional[str]:
    """
    Returns the offending projection text if any SELECT in the tree projects `*` or `alias.*`.
    COUNT(*) is fine: the star there is an argument, not a projection.
    """
    for select in node.find_all(exp.Select):
        for proj in select.expressions:
            inner = proj.this if isinstance(proj, exp.Alias) else proj
            if isinstance(inner, exp.Star) or (isinstance(inner, exp.Column) and isinstance(inner.this, exp.Star)):
                return inner.sql()
    return None


@dataclass
class ASTValidationResult:
    is_valid: bool
    sanitized_sql: str
    tables_accessed: List[str] = field(default_factory=list)
    requires_human: bool = False
    risk_level: str = "LOW"  # "LOW", "MEDIUM", "HIGH"
    reason: str = ""
    join_count: int = 0
    has_limit: bool = False
    confidence_score: int = 100
    score_breakdown: List[str] = field(default_factory=list)


def parse_and_validate_ast(raw_sql: str) -> ASTValidationResult:
    """
    Parses and inspects raw SQL through an Abstract Syntax Tree (AST).
    Provides deterministic query governance, risk classification,
    and mathematical confidence scoring (0-100%).
    """
    clean_sql = raw_sql.strip()

    # Step 1: Parse the SQL statements
    try:
        statements = sqlglot.parse(clean_sql)
    except Exception as e:
        return ASTValidationResult(
            is_valid=False,
            sanitized_sql=raw_sql,
            reason=f"SQL syntax parsing failed: {e}",
            risk_level="HIGH",
            confidence_score=0,
            score_breakdown=[f"Syntax Parse Error: {e}"]
        )

    # Step 2: Enforce Single-Statement Execution (Prevent Query Stacking)
    if len(statements) == 0:
        return ASTValidationResult(
            is_valid=False,
            sanitized_sql=raw_sql,
            reason="Empty SQL statement submitted.",
            risk_level="LOW",
            confidence_score=0,
            score_breakdown=["Empty query statement"]
        )
    if len(statements) > 1:
        return ASTValidationResult(
            is_valid=False,
            sanitized_sql=raw_sql,
            reason="Multiple stacked SQL statements detected. Only single-statement queries are permitted.",
            risk_level="HIGH",
            confidence_score=0,
            score_breakdown=["Query Stacking Attempt: Multiple statements separated by semicolon"]
        )

    ast = statements[0]

    # Step 3: Verify the root query is a SELECT or UNION of SELECTs
    if not isinstance(ast, QUERY_ROOTS):
        op_name = ast.key.upper() if hasattr(ast, "key") else "NON_SELECT"
        return ASTValidationResult(
            is_valid=False,
            sanitized_sql=raw_sql,
            reason=f"Destructive SQL operation detected: '{op_name}'. Only SELECT queries are permitted by SentinelSQL policy.",
            risk_level="HIGH",
            confidence_score=0,
            score_breakdown=[f"Non-SELECT Operation: Root statement is '{op_name}'"]
        )

    # Step 4: Deep tree inspection - verify no destructive nodes exist in subqueries or CTEs
    for node in ast.walk():
        if isinstance(node, DESTRUCTIVE_EXPRESSION_TYPES):
            node_name = node.key.upper() if hasattr(node, "key") else "DESTRUCTIVE"
            return ASTValidationResult(
                is_valid=False,
                sanitized_sql=raw_sql,
                reason=f"Destructive expression '{node_name}' found inside query tree. Execution blocked.",
                risk_level="HIGH",
                confidence_score=0,
                score_breakdown=[f"Subquery Destructive Node: '{node_name}' inside query AST"]
            )

    # Step 4b: Wildcard projections are blocked on every ingress (alignment_plan GRD-03).
    # Explicit columns are required so reviewers, masking and audit see exactly what leaves the database.
    wildcard = _find_wildcard_projection(ast)
    if wildcard:
        return ASTValidationResult(
            is_valid=False,
            sanitized_sql=raw_sql,
            reason=f"SELECT {wildcard} is not allowed: list the columns you need explicitly (the schema tool shows the available columns).",
            risk_level="HIGH",
            confidence_score=0,
            score_breakdown=[f"Wildcard Projection: '{wildcard}' is forbidden; explicit column projection required"]
        )

    # Step 4c: Functions that touch the filesystem/OS, open remote connections or stall the server
    # (time-based blind injection). Rejected outright; the list lives in app/policy/sensitivity.json.
    blocked = set(load_blocked_functions())
    for func in ast.find_all(exp.Func):
        fname = _function_name(func)
        if fname in blocked:
            return ASTValidationResult(
                is_valid=False,
                sanitized_sql=raw_sql,
                reason=f"Function '{fname}' is not permitted (file/OS access, remote connection or time-based attack primitive).",
                risk_level="HIGH",
                confidence_score=0,
                score_breakdown=[f"Blocked Function: '{fname}' is on the policy denylist"]
            )

    # Step 5: Extract tables accessed
    tables = []
    for table_expr in ast.find_all(exp.Table):
        if table_expr.name:
            tables.append(table_expr.name.lower())
    tables = list(dict.fromkeys(tables))  # Deduplicate while preserving order
    sensitive_found = []
    for table_expr in ast.find_all(exp.Table):
        if table_expr.name and is_sensitive_table(table_expr.name, table_expr.db):
            sensitive_found.append(table_expr.name.lower())
    sensitive_found = list(dict.fromkeys(sensitive_found))

    # Step 6: Inspect JOINs and check for Cartesian products
    joins = list(ast.find_all(exp.Join))
    join_count = len(joins)

    for join in joins:
        # Check if join lacks condition (Cartesian join) unless cross join is explicitly declared
        on_clause = join.args.get("on")
        using_clause = join.args.get("using")
        if not on_clause and not using_clause:
            return ASTValidationResult(
                is_valid=False,
                sanitized_sql=raw_sql,
                tables_accessed=tables,
                reason="Unconditional Cartesian product JOIN detected without ON or USING clause.",
                risk_level="HIGH",
                confidence_score=0,
                score_breakdown=["Cartesian Product: JOIN without ON/USING condition is forbidden"]
            )

    if join_count > 3:
        return ASTValidationResult(
            is_valid=False,
            sanitized_sql=raw_sql,
            tables_accessed=tables,
            reason=f"Query contains {join_count} JOINs, exceeding the maximum limit of 3.",
            risk_level="HIGH",
            confidence_score=0,
            score_breakdown=[f"Excessive Joins: {join_count} JOINs exceeds threshold of 3"]
        )

    # Step 6b: Check subquery nesting depth to prevent query planner exhaustion DoS
    subquery_depth = 0
    for sub_expr in ast.find_all(exp.Select):
        depth = 0
        curr = sub_expr.parent
        while curr:
            if isinstance(curr, exp.Select):
                depth += 1
            curr = curr.parent
        if depth > subquery_depth:
            subquery_depth = depth

    if subquery_depth > 3:
        return ASTValidationResult(
            is_valid=False,
            sanitized_sql=raw_sql,
            tables_accessed=tables,
            reason=f"Excessive subquery nesting depth ({subquery_depth} levels, max allowed is 3). Execution blocked to prevent Denial-of-Service.",
            risk_level="HIGH",
            confidence_score=0,
            score_breakdown=[f"Denial-of-Service Defense: Subquery depth of {subquery_depth} exceeds threshold of 3"]
        )

    # Step 7: Check and enforce LIMIT clause
    limit_node = ast.args.get("limit")
    has_limit = limit_node is not None
    sanitized_sql = clean_sql
    effective_limit = None
    excessive_limit_requested = False

    if not has_limit:
        ast = ast.limit(DEFAULT_SAFETY_LIMIT)
        sanitized_sql = ast.sql()
        effective_limit = DEFAULT_SAFETY_LIMIT
    else:
        try:
            limit_val_expr = limit_node.expression
            if isinstance(limit_val_expr, exp.Literal) and limit_val_expr.is_int:
                requested_limit = int(limit_val_expr.this)
                if requested_limit > MAX_PERMITTED_LIMIT:
                    ast.set("limit", exp.Limit(expression=exp.Literal.number(MAX_PERMITTED_LIMIT)))
                    sanitized_sql = ast.sql()
                    effective_limit = MAX_PERMITTED_LIMIT
                    excessive_limit_requested = True
                else:
                    effective_limit = requested_limit
        except Exception:
            effective_limit = DEFAULT_SAFETY_LIMIT

    # Step 8: Aggregation & GROUP BY check on every root SELECT branch (UNION branches included)
    has_proj_agg = False
    for branch in _root_selects(ast):
        branch_agg = False
        branch_non_agg = False
        for proj in branch.expressions:
            kind = _classify_projection(proj)
            if kind is None:
                continue
            branch_agg = branch_agg or kind[0]
            branch_non_agg = branch_non_agg or kind[1]
        has_proj_agg = has_proj_agg or branch_agg
        if branch_agg and branch_non_agg and not branch.args.get("group"):
            return ASTValidationResult(
                is_valid=False,
                sanitized_sql=raw_sql,
                tables_accessed=tables,
                reason="Invalid aggregation: Query projects non-aggregated columns alongside aggregate functions without a GROUP BY clause.",
                risk_level="HIGH",
                confidence_score=0,
                score_breakdown=["Ambiguous Aggregation: Non-aggregated columns projected alongside aggregate functions without GROUP BY"]
            )

    # Step 8b: Recursive CTEs can generate unbounded rows from nothing; they always need a human.
    has_recursive_cte = any(w.args.get("recursive") for w in ast.find_all(exp.With))

    # Step 9: Deterministic Confidence Scoring & Governance Tier Calculation
    score = 100
    score_breakdown = ["Base Score: 100% (Valid SQL AST Syntax)"]
    requires_human = False
    risk_level = "LOW"
    reason = "High confidence (100%) - Validated via deterministic AST safety rules"

    # Rule 1: Sensitive Table Tier (Mandatory Human Approval)
    sensitive_tables = sensitive_found
    if sensitive_tables:
        requires_human = True
        risk_level = "HIGH"
        score = 75
        reason = f"Query accesses sensitive table '{', '.join(sensitive_tables)}' (confidential table). Mandatory Human-in-the-Loop review required."
        score_breakdown.append(f"Sensitive Table Policy: '{', '.join(sensitive_tables)}' accesses capped at 75% (Mandatory Review)")

    # Rule 2: Multi-table Join Modifiers
    if join_count == 3:
        score = min(score, 85)
        requires_human = True
        if risk_level != "HIGH":
            risk_level = "MEDIUM"
            reason = f"Complex query with {join_count} JOINs detected. Human administrator review required."
        score_breakdown.append("Complexity Tier: 3 relational JOINs (Score: 85%, review required)")
    elif join_count == 2:
        if not sensitive_tables:
            score = 95
            score_breakdown.append("Relational Topology: 2 verified JOINs (Score: 95%, safe auto-execute)")
    elif join_count == 1:
        score_breakdown.append("Relational Topology: 1 verified JOIN (Score: 100%, safe auto-execute)")
    elif join_count == 0:
        score_breakdown.append("Relational Topology: Single-table scan (Score: 100%, safe auto-execute)")

    # Rule 3: LIMIT enforcement. A missing LIMIT is silently injected and the query still
    # auto-executes (score 90). Only an explicit, excessive LIMIT is capped and sent to review.
    if not has_limit:
        score = min(score, 90)
        score_breakdown.append(f"Safety Limit: Injected LIMIT {DEFAULT_SAFETY_LIMIT} (query lacked an explicit limit, score 90%, auto-execute)")
        if not requires_human:
            reason = f"Validated (90%): query lacked an explicit limit, SentinelSQL injected LIMIT {DEFAULT_SAFETY_LIMIT}."
    elif excessive_limit_requested:
        requires_human = True
        if risk_level != "HIGH":
            risk_level = "MEDIUM"
            reason = f"Query requested LIMIT exceeding threshold of {MAX_PERMITTED_LIMIT}. SentinelSQL capped to {MAX_PERMITTED_LIMIT} and flagged for review."
        score = min(score, 80)
        score_breakdown.append(f"Excessive Limit: Capped to {MAX_PERMITTED_LIMIT} (flagged for review)")
    else:
        score_breakdown.append(f"Safety Limit: Explicit LIMIT {effective_limit} verified")

    # Rule 5: OR-tautology (`... OR 1=1`): the classic injection that turns a filter into "match everything"
    if any(_is_tautology(o.left) or _is_tautology(o.right) for o in ast.find_all(exp.Or)):
        requires_human = True
        score = min(score, 70)
        if risk_level != "HIGH":
            risk_level = "MEDIUM"
            reason = "Always-true condition combined with OR (e.g. `OR 1=1`) detected: possible injection. Human administrator review required."
        score_breakdown.append("Tautology: OR with an always-true operand (score 70%, review required)")

    # Rule 6: character-stripping functions on free-text columns. Content scanners find PII by its shape and
    # its neighbouring words; REPLACE(notes, ' ', '') destroys both. A scanner cannot win that arms race, so
    # the attempt itself is sent for review (found by the PII benchmark, technique "replace").
    evasion = _masking_evasion_columns(ast)
    if evasion:
        requires_human = True
        score = min(score, 70)
        if risk_level != "HIGH":
            risk_level = "MEDIUM"
            reason = (f"Character-stripping function applied to free-text column(s) {', '.join(evasion)}: possible attempt to "
                      "defeat PII masking. Human administrator review required.")
        score_breakdown.append(f"Masking Evasion: REPLACE/TRANSLATE on {', '.join(evasion)} (score 70%, review required)")

    # Rule 4: Recursive CTE
    if has_recursive_cte:
        requires_human = True
        score = min(score, 80)
        if risk_level != "HIGH":
            risk_level = "MEDIUM"
            reason = "Recursive CTE detected: can generate unbounded rows. Human administrator review required."
        score_breakdown.append("Recursive CTE: WITH RECURSIVE flagged for review (score 80%)")

    if has_proj_agg:
        score_breakdown.append("Aggregation Verification: Validated aggregation and GROUP BY alignment")

    return ASTValidationResult(
        is_valid=True,
        sanitized_sql=sanitized_sql,
        tables_accessed=tables,
        requires_human=requires_human,
        risk_level=risk_level,
        reason=reason,
        join_count=join_count,
        has_limit=has_limit,
        confidence_score=score,
        score_breakdown=score_breakdown
    )
