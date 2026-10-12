"""Administrator endpoints: review queue, audit log, cache, golden templates, business glossary."""

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import Actor, get_actor, require_roles, ROLE_ADMIN
from app.cache.semantic_cache import get_semantic_cache
from app.hitl.web_store import get_pending_runs
from app.utils.audit_logger import audit_anchor, clear_audit_logs, get_audit_logs, verify_audit_chain

router = APIRouter()
_admin = require_roles(ROLE_ADMIN)


class DictionaryTermRequest(BaseModel):
    term: str
    definition: str
    formula_or_rule: str
    related_tables: Optional[str] = ""
    category: Optional[str] = "metric"


# ------------------------------------------------------------------ review queue & audit log

@router.get("/admin/queue")
def get_admin_queue(actor: Actor = Depends(_admin)):
    runs = get_pending_runs()
    return [{"run_id": k, "state": v} for k, v in runs.items()]


@router.get("/admin/audit-logs")
def get_admin_audit_logs_endpoint(
    limit: int = 50,
    status: Optional[str] = None,
    ingress: Optional[str] = None,
    actor: Actor = Depends(_admin),
):
    return get_audit_logs(limit=limit, status_filter=status, ingress_filter=ingress)


@router.delete("/admin/audit-logs")
def clear_admin_audit_logs_endpoint(actor: Actor = Depends(_admin)):
    # The clear is itself recorded as the first row of the new log (see audit_logger.clear_audit_logs).
    deleted_count = clear_audit_logs(cleared_by=actor.user_id)
    return {"status": "success", "deleted_count": deleted_count,
            "message": f"Cleared {deleted_count} audit log records. The clear itself has been recorded."}


@router.get("/admin/audit-logs/verify")
def verify_admin_audit_chain_endpoint(anchor_head: Optional[str] = None, anchor_count: Optional[int] = None,
                                      actor: Actor = Depends(_admin)):
    """
    Recomputes the audit hash chain and reports the first row that fails verification. Pass `anchor_head` and
    `anchor_count` (from /admin/audit-logs/anchor, stored elsewhere) to also detect truncation and rewrites.
    """
    anchor = {"head_hash": anchor_head, "count": anchor_count} if anchor_head else None
    return verify_audit_chain(anchor=anchor)


@router.get("/admin/audit-logs/anchor")
def audit_anchor_endpoint(actor: Actor = Depends(_admin)):
    """The current chain head and length: record it somewhere the database's writer cannot reach."""
    return audit_anchor()


# ------------------------------------------------------------------ semantic cache

@router.get("/admin/cache/stats")
def get_admin_cache_stats_endpoint(actor: Actor = Depends(_admin)):
    return get_semantic_cache().get_stats()


@router.delete("/admin/cache")
def clear_admin_cache_endpoint(actor: Actor = Depends(_admin)):
    cleared_count = get_semantic_cache().clear()
    return {"status": "success", "cleared_count": cleared_count, "message": f"Cleared {cleared_count} cached entries."}


@router.get("/admin/embeddings")
def get_embedding_health_endpoint(actor: Actor = Depends(_admin)):
    """
    Health of the embedding layer: the shared engine (provider, degraded?, fallback count) plus how
    many stored vectors in each index were produced by a different backend (and will be re-embedded).
    """
    from app.rag.golden_store import get_vector_store as golden_store
    from app.rag.data_dictionary import get_vector_store as dictionary_store
    from app.rag.schema_retriever import _GLOBAL_RETRIEVER
    from app.rag.vector_store import get_embedding_engine
    return {
        "engine": get_embedding_engine().status(),
        "indexes": {
            "schema": _GLOBAL_RETRIEVER.vector_store.status(),
            "golden_queries": golden_store().status(),
            "data_dictionary": dictionary_store().status(),
        },
        "semantic_cache": {k: v for k, v in get_semantic_cache().get_stats().items()
                           if k in ("total_entries", "entries_with_other_embedding_backend")},
    }


# ------------------------------------------------------------------ golden query templates
# (golden_store / data_dictionary touch the database when imported, so they are imported lazily)

@router.get("/admin/golden-queries")
def get_golden_queries_endpoint(actor: Actor = Depends(_admin)):
    from app.rag.golden_store import get_all_golden_queries
    return get_all_golden_queries()


@router.delete("/admin/golden-queries/{query_id}")
def delete_golden_query_endpoint(query_id: int, actor: Actor = Depends(_admin)):
    from app.rag.golden_store import delete_golden_query
    if not delete_golden_query(query_id):
        raise HTTPException(status_code=404, detail="Golden template not found")
    get_semantic_cache().clear()  # templates steer SQL generation: cached answers may now be stale
    return {"status": "success", "message": f"Deleted golden query template #{query_id}"}


# ------------------------------------------------------------------ enterprise data dictionary

@router.get("/dictionary")
def get_public_dictionary_endpoint(actor: Actor = Depends(get_actor)):
    """Lets signed-in users and AI assistants browse the available business terms and metrics."""
    from app.rag.data_dictionary import get_all_terms
    return get_all_terms()


@router.get("/admin/dictionary")
def get_dictionary_terms_endpoint(actor: Actor = Depends(_admin)):
    from app.rag.data_dictionary import get_all_terms
    return get_all_terms()


@router.post("/admin/dictionary")
def create_or_update_dictionary_term_endpoint(req: DictionaryTermRequest, actor: Actor = Depends(_admin)):
    from app.rag.data_dictionary import add_term
    term_id = add_term(
        term=req.term,
        definition=req.definition,
        formula_or_rule=req.formula_or_rule,
        related_tables=req.related_tables or "",
        category=req.category or "metric",
        created_by=actor.user_id,
    )
    get_semantic_cache().clear()  # glossary formulas steer SQL generation: cached answers may now be stale
    return {"status": "success", "id": term_id, "message": f"Saved dictionary term '{req.term}'"}


@router.delete("/admin/dictionary/{term_id}")
def delete_dictionary_term_endpoint(term_id: int, actor: Actor = Depends(_admin)):
    from app.rag.data_dictionary import delete_term
    if not delete_term(term_id):
        raise HTTPException(status_code=404, detail="Dictionary term not found")
    get_semantic_cache().clear()
    return {"status": "success", "message": f"Deleted dictionary term #{term_id}"}


@router.post("/admin/dictionary/import-dbt")
def import_dbt_manifest_endpoint(manifest: Dict[str, Any], actor: Actor = Depends(_admin)):
    from app.rag.data_dictionary import import_dbt_manifest
    imported_count = import_dbt_manifest(manifest, created_by=f"dbt_{actor.user_id}")
    get_semantic_cache().clear()
    return {"status": "success", "imported_count": imported_count, "message": f"Imported {imported_count} terms from dbt manifest"}
