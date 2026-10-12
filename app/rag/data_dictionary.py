"""
SentinelSQL Enterprise Data Dictionary & Business Glossary
----------------------------------------------------------
Connects SentinelSQL to enterprise business semantics, metric definitions,
and dbt docs / data catalogs.

Solves the business-to-technical translation problem:
- Translates business concepts (e.g., "Net Revenue", "Active Customers", "Gross Margin", "VIP Tier")
  into explicit, deterministic SQL formulas and WHERE clauses.
- Injects relevant metric definitions into the SQL Generator prompt.
- Supports manual admin CRUD as well as dbt manifest.json / catalog import.
- Uses semantic vector matching + keyword extraction to find relevant terms for any prompt.
"""

import os
import re
import sqlite3
import threading
from typing import List, Dict, Any, Optional
from app.rag.vector_store import EmbeddedVectorStore
from app.utils.logger import get_logger

logger = get_logger("sentinel.dictionary")

_VECTOR_STORE: Optional[EmbeddedVectorStore] = None
_INITIALIZED: set = set()
_init_lock = threading.Lock()


def _db_path() -> str:
    from app.db.database import internal_sqlite_path
    return internal_sqlite_path("DICTIONARY_DB_PATH")


def _vector_store() -> EmbeddedVectorStore:
    """Dedicated persistent vector store for the business glossary (created on first use)."""
    global _VECTOR_STORE
    cache_file = os.getenv("DICTIONARY_VECTOR_CACHE", ".dictionary_vectors.json")
    if _VECTOR_STORE is None or _VECTOR_STORE.cache_file != cache_file:
        _VECTOR_STORE = EmbeddedVectorStore(cache_file=cache_file)
    return _VECTOR_STORE


def get_vector_store() -> EmbeddedVectorStore:
    _ensure_init()
    return _vector_store()


def _connect() -> sqlite3.Connection:
    _ensure_init()
    return sqlite3.connect(_db_path())


# Business terms for the bundled demo database. Every formula references columns that exist in
# app/db/schema.sql and app/db/demo_seed.py (a test enforces this: the old seeds referenced
# orders.total_amount, customers.status, products.cost, ... which do not exist, so the glossary
# actively pushed the model toward "no such column" errors; alignment_plan DAT-07).
DEFAULT_GLOSSARY_SEEDS = [
    {
        "term": "Net Revenue",
        "definition": "Revenue from customer orders after subtracting refunds paid out on returned products.",
        "formula_or_rule": "SUM(orders.amount) - (SELECT COALESCE(SUM(returns.refund_amount), 0) FROM returns)",
        "related_tables": "orders, returns",
        "category": "metric",
    },
    {
        "term": "Active Customer",
        "definition": "A customer who has placed at least one order.",
        "formula_or_rule": "customers.customer_id IN (SELECT orders.customer_id FROM orders)",
        "related_tables": "customers, orders",
        "category": "filter",
    },
    {
        "term": "Average Order Value (AOV)",
        "definition": "The average amount of a customer order.",
        "formula_or_rule": "AVG(orders.amount)",
        "related_tables": "orders",
        "category": "metric",
    },
    {
        "term": "VIP Customer",
        "definition": "High-value customer whose cumulative lifetime order spend is at least 3,000.",
        "formula_or_rule": "SUM(orders.amount) >= 3000   (use as HAVING after GROUP BY customer)",
        "related_tables": "customers, orders",
        "category": "segment",
    },
    {
        "term": "Open Invoice",
        "definition": "An invoice that has been issued but not yet paid.",
        "formula_or_rule": "invoices.status = 'open'",
        "related_tables": "invoices",
        "category": "filter",
    },
    {
        "term": "Escalated Ticket",
        "definition": "A support ticket with high priority that is not yet closed.",
        "formula_or_rule": "support_tickets.priority = 'high' AND support_tickets.status <> 'closed'",
        "related_tables": "support_tickets",
        "category": "filter",
    },
    {
        "term": "Slow Delivery",
        "definition": "A shipment that took more than 5 days from shipping to delivery.",
        "formula_or_rule": "julianday(shipments.delivered_date) - julianday(shipments.shipped_date) > 5",
        "related_tables": "shipments",
        "category": "filter",
    },
    {
        "term": "Senior Employee",
        "definition": "An employee hired before 1 January 2020.",
        "formula_or_rule": "employees.hire_date < '2020-01-01'",
        "related_tables": "employees",
        "category": "filter",
    },
    {
        "term": "Top Performer",
        "definition": "An employee whose average performance review rating is at least 4.",
        "formula_or_rule": "AVG(performance_reviews.rating) >= 4   (use as HAVING after GROUP BY employee)",
        "related_tables": "employees, performance_reviews",
        "category": "segment",
    },
]


def _sync_system_seeds(cursor: sqlite3.Cursor) -> None:
    """
    Keeps the rows owned by the code (`created_by = 'system'`) identical to DEFAULT_GLOSSARY_SEEDS:
    inserts missing terms, corrects ones whose definition changed (e.g. the old seeds that referenced
    non-existent columns) and removes system terms that are no longer seeded. Terms created or edited
    by an administrator carry another `created_by` and are never touched.
    """
    wanted = {seed["term"]: seed for seed in DEFAULT_GLOSSARY_SEEDS}
    changes = 0
    existing = {row[1]: row for row in cursor.execute("SELECT id, term, formula_or_rule, definition, created_by FROM data_dictionary")}
    for term, seed in wanted.items():
        row = existing.get(term)
        if row is None:
            cursor.execute(
                "INSERT INTO data_dictionary (term, definition, formula_or_rule, related_tables, category, created_by) "
                "VALUES (?, ?, ?, ?, ?, 'system')",
                (term, seed["definition"], seed["formula_or_rule"], seed["related_tables"], seed["category"]))
            changes += 1
        elif row[4] == "system" and (row[2] != seed["formula_or_rule"] or row[3] != seed["definition"]):
            cursor.execute(
                "UPDATE data_dictionary SET definition = ?, formula_or_rule = ?, related_tables = ?, category = ? WHERE id = ?",
                (seed["definition"], seed["formula_or_rule"], seed["related_tables"], seed["category"], row[0]))
            changes += 1
    for term, row in existing.items():
        if row[4] == "system" and term not in wanted:
            cursor.execute("DELETE FROM data_dictionary WHERE id = ?", (row[0],))
            changes += 1
    if changes:
        logger.info("Reconciled %d system glossary terms with the current seeds", changes)


def _ensure_init() -> None:
    """Creates the table (and, for the demo database, the starter glossary) on first use. Idempotent."""
    path = _db_path()
    if path in _INITIALIZED:
        return
    with _init_lock:
        if path in _INITIALIZED:
            return
        _INITIALIZED.add(path)  # set first: _reindex_dictionary_vectors() calls back into get_all_terms()
        try:
            with sqlite3.connect(path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS data_dictionary (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        term TEXT NOT NULL UNIQUE,
                        definition TEXT NOT NULL,
                        formula_or_rule TEXT NOT NULL,
                        related_tables TEXT DEFAULT '',
                        category TEXT DEFAULT 'metric',
                        created_by TEXT DEFAULT 'admin',
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                conn.commit()

                from app.db.database import seed_demo_data_enabled
                if seed_demo_data_enabled():
                    _sync_system_seeds(cursor)
                    conn.commit()
            _reindex_dictionary_vectors()
        except Exception as e:
            _INITIALIZED.discard(path)
            logger.warning("Failed to initialise data_dictionary: %s", e)


def _reindex_dictionary_vectors():
    """Syncs SQLite terms into the vector store: adds/updates current terms and drops vectors of deleted ones."""
    terms = get_all_terms()
    store = _vector_store()
    for t in terms:
        content = f"Term: {t['term']} | Definition: {t['definition']} | Formula: {t['formula_or_rule']} | Tables: {t.get('related_tables', '')}"
        store.add_or_update_document(doc_id=str(t["id"]), content=content, metadata=t)
    live = {str(t["id"]) for t in terms}
    for stale in [doc_id for doc_id in store.documents if doc_id not in live]:
        del store.documents[stale]   # a term removed or replaced while this index was not looking
    store.save_cache()


def get_all_terms() -> List[Dict[str, Any]]:
    """Returns all data dictionary terms."""
    try:
        with _connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM data_dictionary ORDER BY term ASC")
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        logger.warning(f"[Data Dictionary Warning] Failed to fetch terms: {e}")
        return []


def add_term(
    term: str,
    definition: str,
    formula_or_rule: str,
    related_tables: str = "",
    category: str = "metric",
    created_by: str = "admin",
) -> int:
    """Adds or updates a business glossary term and indexes it in the vector store."""
    clean_term = term.strip()
    clean_def = definition.strip()
    clean_formula = formula_or_rule.strip()
    clean_tables = related_tables.strip()

    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO data_dictionary (term, definition, formula_or_rule, related_tables, category, created_by)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(term) DO UPDATE SET
                definition = excluded.definition,
                formula_or_rule = excluded.formula_or_rule,
                related_tables = excluded.related_tables,
                category = excluded.category,
                created_by = excluded.created_by
        """, (clean_term, clean_def, clean_formula, clean_tables, category, created_by))
        conn.commit()
        term_id = cursor.lastrowid

    # Update vector index
    doc_id = str(term_id)
    content = f"Term: {clean_term} | Definition: {clean_def} | Formula: {clean_formula} | Tables: {clean_tables}"
    _vector_store().add_or_update_document(
        doc_id=doc_id,
        content=content,
        metadata={
            "id": term_id,
            "term": clean_term,
            "definition": clean_def,
            "formula_or_rule": clean_formula,
            "related_tables": clean_tables,
            "category": category,
            "created_by": created_by
        }
    )
    _vector_store().save_cache()
    return term_id


def delete_term(term_id: int) -> bool:
    """Deletes a business term by ID from database and vector index."""
    try:
        with _connect() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM data_dictionary WHERE id = ?", (term_id,))
            conn.commit()
            deleted = cursor.rowcount > 0

        # Remove from vector index
        doc_id = str(term_id)
        if doc_id in _vector_store().documents:
            del _vector_store().documents[doc_id]
            _vector_store().save_cache()

        return deleted
    except Exception as e:
        logger.warning(f"[Data Dictionary Warning] Failed to delete term {term_id}: {e}")
        return False


def find_relevant_terms(query: str, top_k: int = 3, threshold: float = 0.0) -> List[Dict[str, Any]]:
    """
    Finds business glossary terms matching a user query using dual strategy:
    1. Lexical / Keyword detection (supports singular & plural variations)
    2. Semantic vector cosine similarity
    """
    if not query or not query.strip():
        return []

    query_lower = query.lower()
    matched_ids = set()
    results = []

    # 1. Lexical / Exact phrase matching on term names (with singular/plural support)
    all_terms = get_all_terms()
    for t in all_terms:
        term_name = t["term"].lower()
        # Handle singular and plural forms (e.g. 'active customer' and 'active customers')
        base_name = term_name[:-1] if term_name.endswith('s') else term_name
        pattern = r'\b' + re.escape(base_name) + r's?\b'

        if re.search(pattern, query_lower):
            matched_ids.add(t["id"])
            results.append({
                "term": t["term"],
                "definition": t["definition"],
                "formula_or_rule": t["formula_or_rule"],
                "related_tables": t.get("related_tables", ""),
                "match_reason": "exact_term_match"
            })

    # 2. Semantic vector similarity search
    if len(results) < top_k:
        vector_matches = _vector_store().search(query=query, top_k=top_k, threshold=threshold)
        for vm in vector_matches:
            meta = vm.get("metadata", {})
            term_id = meta.get("id")
            if term_id and term_id not in matched_ids:
                matched_ids.add(term_id)
                results.append({
                    "term": meta.get("term", vm.get("content")),
                    "definition": meta.get("definition", ""),
                    "formula_or_rule": meta.get("formula_or_rule", ""),
                    "related_tables": meta.get("related_tables", ""),
                    "match_reason": f"semantic_similarity_{vm.get('score', 0)}"
                })
            if len(results) >= top_k:
                break

    return results


def format_dictionary_context(query: str, top_k: int = 3) -> str:
    """
    Formats matching business glossary terms into an explicit instruction block
    for the SQL generation LLM.
    """
    relevant = find_relevant_terms(query=query, top_k=top_k)
    if not relevant:
        return ""

    lines = ["Enterprise Business Glossary & Metric Rules:"]
    for item in relevant:
        term_line = f"- Term: \"{item['term']}\""
        if item.get("definition"):
            term_line += f" | Definition: {item['definition']}"
        lines.append(term_line)
        lines.append(f"  SQL Formula / Condition: {item['formula_or_rule']}")
        if item.get("related_tables"):
            lines.append(f"  Applicable Tables: {item['related_tables']}")

    return "\n".join(lines).strip()


def import_dbt_manifest(manifest: Dict[str, Any], created_by: str = "dbt_importer") -> int:
    """
    Imports metrics and model column descriptions from a dbt manifest.json.
    Enables automatic ingestion of modern enterprise data stack definitions.
    """
    count = 0
    # 1. Ingest dbt semantic layer / metrics
    metrics = manifest.get("metrics", {})
    for metric_name, mdata in metrics.items():
        label = mdata.get("label") or metric_name.replace("_", " ").title()
        description = mdata.get("description", "Imported from dbt metric definition")
        calc = mdata.get("type", "") + f"({mdata.get('sql', '*')})"
        add_term(
            term=label,
            definition=description,
            formula_or_rule=calc,
            related_tables=mdata.get("model", ""),
            category="metric",
            created_by=created_by
        )
        count += 1

    # 2. Ingest model business descriptions
    nodes = manifest.get("nodes", {})
    for node_id, node in nodes.items():
        if node.get("resource_type") == "model":
            model_name = node.get("name", "")
            description = node.get("description", "")
            if description and len(description.strip()) > 10:
                add_term(
                    term=f"{model_name} Model",
                    definition=description,
                    formula_or_rule=f"SELECT * FROM {model_name}",
                    related_tables=model_name,
                    category="model",
                    created_by=created_by
                )
                count += 1

    return count

