"""
SentinelSQL Schema RAG Retriever
--------------------------------
Indexes live introspected database tables into the persistent vector store.
Dynamically retrieves relevant table schemas and their relational foreign keys
for incoming user questions, pruning irrelevant tables to reduce prompt tokens
and eliminate hallucination in large enterprise databases.
"""

import json
import os
from typing import Callable, Dict, Any, List, Set, Optional
from app.rag.vector_store import EmbeddedVectorStore
from app.utils.schemacontext import introspect_schema, get_schema_tables_dict


_DEFAULT_DESCRIPTIONS_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "db", "table_descriptions.json")


def load_table_descriptions() -> Dict[str, str]:
    """Per-table plain-language descriptions used to enrich embeddings. Missing file = no descriptions."""
    path = os.getenv("TABLE_DESCRIPTIONS_PATH", _DEFAULT_DESCRIPTIONS_PATH)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return {k.lower(): v for k, v in data.items() if not k.startswith("_") and isinstance(v, str)}


class SchemaRAGRetriever:
    """
    Semantic indexing and retrieval of relational database schemas.

    The schema comes from `schema_provider` (default: live introspection of the configured database),
    which lets the retriever be exercised on any schema, e.g. the pooled Spider benchmark databases.
    """
    def __init__(self, cache_file: str = ".schema_vectors.json",
                 schema_provider: Optional[Callable[..., Dict[str, Any]]] = None,
                 engine=None):
        self.vector_store = EmbeddedVectorStore(cache_file=cache_file, engine=engine)
        self._schema_provider = schema_provider
        self.indexed = False

    def _schema(self, force_refresh: bool = False) -> Dict[str, Any]:
        if self._schema_provider is not None:
            return self._schema_provider()
        return introspect_schema(force_refresh=force_refresh)

    @staticmethod
    def document_for(table_name: str, meta: Dict[str, Any], description: str) -> str:
        col_list = [f"{c['name']} ({c['type']})" for c in meta.get("columns", [])]
        doc_text = (
            f"Table Name: {table_name}\n"
            f"Description: {description}\n"
            f"Columns: {', '.join(col_list)}\n"
        )
        fks = meta.get("foreign_keys", [])
        if fks:
            doc_text += f"Foreign Keys: {', '.join(fks)}\n"
        return doc_text

    def index_schema(self, force_refresh: bool = False) -> int:
        """
        Embeds every table into the vector store (in batches). Returns how many tables were (re-)embedded;
        tables whose content and embedding backend are unchanged keep their stored vector.
        """
        tables = self._schema(force_refresh=force_refresh).get("tables", {})
        descriptions = load_table_descriptions()
        items = []
        for table_name, meta in tables.items():
            hint = descriptions.get(table_name.lower(), f"Database table storing {table_name} records.")
            items.append((
                table_name,
                self.document_for(table_name, meta, hint),
                {"table_name": table_name, "foreign_keys": meta.get("foreign_keys", []),
                 "text_repr": meta.get("text_repr", f"{table_name}()")},
            ))
        new_embeddings_count = self.vector_store.add_or_update_documents(items)
        if new_embeddings_count > 0:
            self.vector_store.save_cache()
        self.indexed = True
        return new_embeddings_count

    def retrieve_tables(self, query: str, top_k: int = 3, threshold: float = 0.15,
                        fk_closure: bool = True, small_schema_cutoff: int = 5) -> Dict[str, Any]:
        """
        Chooses the tables to put in the prompt: the top_k most similar tables plus (optionally) their
        direct foreign-key neighbours, which keeps join paths intact. Returns
        {"tables": [...], "relationships": [...], "matched": [...], "mode": "full|vector|fallback"}.
        """
        schema_data = self._schema()
        all_tables = schema_data.get("tables", {})
        all_fks = schema_data.get("relationships", [])

        # A tiny schema is cheaper to send whole than to search
        if len(all_tables) <= small_schema_cutoff:
            return {"tables": sorted(all_tables), "relationships": list(all_fks), "matched": [], "mode": "full"}

        if not self.indexed:
            self.index_schema()

        matched = [m["id"] for m in self.vector_store.search(query=query, top_k=top_k, threshold=threshold)]
        if not matched:  # nothing similar enough (or incompatible vectors): fall back to the whole schema, loudly
            return {"tables": sorted(all_tables), "relationships": list(all_fks), "matched": [], "mode": "fallback"}

        tables, relationships = self.expand_with_foreign_keys(matched, all_fks) if fk_closure else (set(matched), [])
        return {"tables": sorted(t for t in tables if t in all_tables),
                "relationships": relationships, "matched": matched, "mode": "vector"}

    @staticmethod
    def expand_with_foreign_keys(matched: List[str], all_fks: List[str]):
        """
        The matched tables plus every table that shares a foreign-key relationship with one of them, so a join
        path through a table the question never mentions is not lost. Returns (tables, relationships).
        Relationship format: "orders.customer_id -> customers.customer_id".
        """
        expanded = set(matched)
        relationships: List[str] = []
        for rel in all_fks:
            try:
                left = rel.split(".")[0].strip()
                right = rel.split("->")[1].split(".")[0].strip()
            except Exception:
                continue
            if left in matched or right in matched:
                expanded.update((left, right))
                relationships.append(rel)
        return expanded, list(dict.fromkeys(relationships))

    def retrieve_schema(self, query: str, top_k: int = 3, threshold: float = 0.15) -> str:
        """The schema text for the prompt (see `retrieve_tables` for how tables are chosen)."""
        schema_data = self._schema()
        all_tables = schema_data.get("tables", {})
        chosen = self.retrieve_tables(query, top_k=top_k, threshold=threshold)
        if chosen["mode"] in ("full", "fallback"):
            return schema_data.get("formatted_context", "")

        lines = [all_tables[t]["text_repr"] for t in chosen["tables"] if t in all_tables]
        if chosen["relationships"]:
            lines += ["", "Relationships:", *chosen["relationships"]]
        return "\n".join(lines).strip()


# Global singleton instance
_GLOBAL_RETRIEVER = SchemaRAGRetriever()


def get_rag_schema_context(query: Optional[str] = None, top_k: int = 3) -> str:
    """
    Main entry point for agent workflow.
    If query is provided and database is large, retrieves relevant tables via Schema RAG.
    Otherwise returns full introspected schema context.
    """
    if query:
        return _GLOBAL_RETRIEVER.retrieve_schema(query=query, top_k=top_k)
    return introspect_schema().get("formatted_context", "")
