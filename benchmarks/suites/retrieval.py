"""
Schema-RAG retrieval suite (the claim behind "99% fewer context tokens").

Compression means nothing unless the tables the question needs survive it, so this suite measures both:

  * recall    for each Spider dev question, are ALL the tables its gold query uses inside the tables the
              retriever would put in the prompt?
  * tokens    how many tokens does that retrieved prompt cost, versus sending the whole schema?

Data are Yale Spider's own schemas pooled into one big namespace (every table id is `<database>__<table>`,
as in a warehouse with one schema per domain): 81 tables for the 20 dev databases ("dev" pool) and 876 tables
for all 166 databases ("all" pool). The questions come from the stratified Spider sample, and gold tables are
parsed from each gold SQL, not hand-labelled.

The system under test is the production `SchemaRAGRetriever` (Gemini embeddings, top-k + foreign-key closure).
For context it is compared with two baselines that use the same closure step: lexical TF-IDF over the same
table documents, and random selection. The sweep over k and the closure on/off pair show where each
design choice earns its keep.

Pools are run separately because the free embedding tier allows 1,000 requests per day; every vector is cached,
so a re-run only embeds what is missing.
"""

import json
import math
import os
import random
import re
import time
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional

import numpy as np

from benchmarks.lib import spider as sp
from benchmarks.lib.meta import run_metadata
from benchmarks.lib.stats import latency_summary, proportion
from benchmarks.suites.common import CACHE_DIR, EMBEDDING_CACHE, WORK_DIR, load_result, progress, save_result, workspace

K_SWEEP = [1, 2, 3, 4, 5, 6, 8]
PRODUCT_K = 3
TOKEN_SAMPLE = 30
TOKEN_CACHE = os.path.join(CACHE_DIR, "token_counts.json")


# ---------------------------------------------------------------------------- pools

def build_pool(db_ids: List[str], tables_meta: Dict[str, Any]) -> Dict[str, Any]:
    tables, relationships = {}, []
    for db in db_ids:
        meta = tables_meta[db]
        names = [f"{db}__{t.lower()}" for t in meta["table_names_original"]]
        columns: Dict[int, List[Dict[str, str]]] = defaultdict(list)
        col_of = {}
        for col_id, ((t_idx, col), ctype) in enumerate(zip(meta["column_names_original"], meta["column_types"])):
            if t_idx >= 0:
                columns[t_idx].append({"name": col, "type": ctype.upper()})
                col_of[col_id] = (t_idx, col)
        table_fks: Dict[int, List[str]] = defaultdict(list)
        for a, b in meta["foreign_keys"]:
            (ta, ca), (tb, cb) = col_of[a], col_of[b]
            rel = f"{names[ta]}.{ca} -> {names[tb]}.{cb}"
            relationships.append(rel)
            table_fks[ta].append(rel)
        for t_idx, name in enumerate(names):
            tables[name] = {"columns": columns[t_idx], "foreign_keys": table_fks[t_idx],
                            "text_repr": f"{name}({', '.join(c['name'] for c in columns[t_idx])})"}
    text = "\n".join(t["text_repr"] for t in tables.values())
    if relationships:
        text += "\n\nRelationships:\n" + "\n".join(relationships)
    return {"tables": tables, "relationships": relationships, "formatted_context": text}


def prompt_text(schema: Dict[str, Any], chosen: List[str], relationships: List[str]) -> str:
    lines = [schema["tables"][t]["text_repr"] for t in chosen]
    return "\n".join(lines + (["", "Relationships:", *relationships] if relationships else []))


# ---------------------------------------------------------------------------- tokens

_token_cache: Optional[Dict[str, int]] = None


def count_tokens(text: str) -> Dict[str, Any]:
    """Gemini's own tokenizer through the API (cached); falls back to chars/4 and says so."""
    global _token_cache
    import hashlib
    if _token_cache is None:
        _token_cache = json.load(open(TOKEN_CACHE, encoding="utf-8")) if os.path.exists(TOKEN_CACHE) else {}
    key = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if key in _token_cache:
        return {"tokens": _token_cache[key], "tokenizer": "gemini-api"}
    try:
        from app.utils.llm_factory import get_llm
        n = int(get_llm("sql").client.get_num_tokens(text))
        _token_cache[key] = n
        os.makedirs(CACHE_DIR, exist_ok=True)
        json.dump(_token_cache, open(TOKEN_CACHE, "w", encoding="utf-8"))
        return {"tokens": n, "tokenizer": "gemini-api"}
    except Exception:
        return {"tokens": max(1, len(text) // 4), "tokenizer": "approx_chars_div_4"}


# ---------------------------------------------------------------------------- baselines

_WORD = re.compile(r"[a-z]+")


def tokenize(text: str) -> List[str]:
    out = []
    for w in _WORD.findall(text.lower().replace("_", " ")):
        out.append(w[:-1] if len(w) > 3 and w.endswith("s") else w)
    return out


class TfIdf:
    """Plain TF-IDF cosine over the same documents the embeddings see."""

    def __init__(self, docs: Dict[str, str]):
        self.ids = list(docs)
        toks = [tokenize(docs[i]) for i in self.ids]
        df = Counter(w for t in toks for w in set(t))
        self.vocab = {w: i for i, w in enumerate(df)}
        n = len(self.ids)
        self.idf = np.array([math.log((1 + n) / (1 + df[w])) + 1 for w in self.vocab])
        self.matrix = np.vstack([self._vec(t) for t in toks])

    def _vec(self, tokens: List[str]) -> np.ndarray:
        v = np.zeros(len(self.vocab))
        for w, c in Counter(tokens).items():
            if w in self.vocab:
                v[self.vocab[w]] = (1 + math.log(c)) * self.idf[self.vocab[w]]
        norm = np.linalg.norm(v)
        return v / norm if norm else v

    def rank(self, question: str, k: int) -> List[str]:
        scores = self.matrix @ self._vec(tokenize(question))
        return [self.ids[i] for i in np.argsort(-scores)[:k]]


# ---------------------------------------------------------------------------- evaluation

def evaluate_pool(pool_name: str, db_ids: List[str], tables_meta, questions, usage_hook) -> Dict[str, Any]:
    from app.rag.schema_retriever import SchemaRAGRetriever
    from app.rag.vector_store import get_embedding_engine

    schema = build_pool(db_ids, tables_meta)
    retriever = SchemaRAGRetriever(cache_file=os.path.join(WORK_DIR, f"schema_vectors_{pool_name}.json"), schema_provider=lambda: schema)
    engine = get_embedding_engine()
    started = time.time()

    print(f"  [{pool_name}] {len(schema['tables'])} tables: embedding table documents ...", flush=True)
    newly_embedded = retriever.index_schema()
    print(f"  [{pool_name}] {newly_embedded} embedded, prefetching {len(questions)} question vectors ...", flush=True)
    engine.embed_many([q["question"] for q in questions])

    docs = {tid: retriever.document_for(tid, meta, f"Database table storing {tid} records.") for tid, meta in schema["tables"].items()}
    tfidf = TfIdf(docs)
    all_ids = list(schema["tables"])
    expand = SchemaRAGRetriever.expand_with_foreign_keys
    rng = random.Random(7)
    max_k = max(K_SWEEP)

    rows = []
    search_ms = []
    for n, q in enumerate(questions, 1):
        progress(f"retrieval-{pool_name}", n, len(questions), started)
        gold = {f"{q['db_id']}__{t}" for t in sp.gold_tables(q["gold"])}
        t0 = time.perf_counter()
        ranked = [m["id"] for m in retriever.vector_store.search(q["question"], top_k=max_k, threshold=0.0)]
        search_ms.append((time.perf_counter() - t0) * 1000)
        tfidf_ranked = tfidf.rank(q["question"], max_k)
        product = retriever.retrieve_tables(q["question"], top_k=PRODUCT_K)

        row = {"index": q["index"], "db_id": q["db_id"], "hardness": q["hardness"], "gold": sorted(gold), "product_tables": product["tables"],
               "product_mode": product["mode"], "by_k": {}}
        for k in K_SWEEP:
            variants = {
                "embedding_no_closure": set(ranked[:k]),
                "embedding_closure": expand(ranked[:k], schema["relationships"])[0],
                "tfidf_closure": expand(tfidf_ranked[:k], schema["relationships"])[0],
                "random_closure": expand(rng.sample(all_ids, k), schema["relationships"])[0],
            }
            row["by_k"][k] = {name: {"full": gold <= got, "recall": len(gold & got) / len(gold), "n": len(got)} for name, got in variants.items()}
        rows.append(row)

    def curve(name: str) -> Dict[str, Any]:
        out = {}
        for k in K_SWEEP:
            cells = [r["by_k"][k][name] for r in rows]
            out[str(k)] = {"all_gold_tables_retrieved": proportion(sum(c["full"] for c in cells), len(cells)),
                           "mean_table_recall": round(100 * sum(c["recall"] for c in cells) / len(cells), 2),
                           "mean_tables_in_prompt": round(sum(c["n"] for c in cells) / len(cells), 2)}
        return out

    prod_full = [set(r["gold"]) <= set(r["product_tables"]) for r in rows]
    by_tier = defaultdict(list)
    for r, ok in zip(rows, prod_full):
        by_tier[r["hardness"]].append(ok)
    multi = [ok for r, ok in zip(rows, prod_full) if len(r["gold"]) > 1]
    single = [ok for r, ok in zip(rows, prod_full) if len(r["gold"]) == 1]

    # tokens: whole schema vs the retrieved prompt (sample)
    full_tokens = count_tokens(schema["formatted_context"])
    retrieved_tokens = []
    for r in rows[:TOKEN_SAMPLE]:
        chosen = set(r["product_tables"])
        rels = [rel for rel in schema["relationships"]
                if rel.split(".")[0].strip() in chosen and rel.split("->")[1].split(".")[0].strip() in chosen]
        retrieved_tokens.append(count_tokens(prompt_text(schema, r["product_tables"], rels)))
    tok_mean = sum(t["tokens"] for t in retrieved_tokens) / len(retrieved_tokens)

    failures = [{"question": q["question"], "gold": r["gold"], "retrieved": r["product_tables"]}
                for q, r, ok in zip(questions, rows, prod_full) if not ok][:25]
    return {
        "pool": pool_name, "n_databases": len(db_ids), "n_tables": len(schema["tables"]), "n_questions": len(rows),
        "newly_embedded_tables": newly_embedded,
        "product_default_top3_plus_fk_closure": {
            "all_gold_tables_retrieved": proportion(sum(prod_full), len(prod_full)),
            "single_table_questions": proportion(sum(single), len(single)),
            "multi_table_questions": proportion(sum(multi), len(multi)),
            "by_hardness": {t: proportion(sum(v), len(v)) for t, v in sorted(by_tier.items())},
            "mean_tables_in_prompt": round(sum(len(r["product_tables"]) for r in rows) / len(rows), 2),
            "fraction_of_pool_in_prompt_pct": round(100 * sum(len(r["product_tables"]) for r in rows) / len(rows) / len(schema["tables"]), 2),
            "retrieval_modes": dict(Counter(r["product_mode"] for r in rows)),   # "fallback" = nothing cleared the floor, whole schema sent
        },
        "tokens": {
            "tokenizer": full_tokens["tokenizer"], "full_schema_tokens": full_tokens["tokens"],
            "retrieved_prompt_tokens_mean": round(tok_mean, 1), "sample_size": len(retrieved_tokens),
            "reduction_pct": round(100 * (1 - tok_mean / full_tokens["tokens"]), 2),
        },
        "sweep_embedding_closure": curve("embedding_closure"),
        "sweep_embedding_no_closure": curve("embedding_no_closure"),
        "sweep_tfidf_closure": curve("tfidf_closure"),
        "sweep_random_closure": curve("random_closure"),
        "vector_search_latency": latency_summary(search_ms),
        "failures_sample": failures,
        "embedding_backend": engine.backend_id,
    }


def run(llm: bool = True, limit: Optional[int] = None) -> Dict[str, Any]:
    from app.rag.vector_store import EmbeddingUnavailableError
    from benchmarks.lib.llm import DailyQuotaExceeded, embedding_harness
    from benchmarks.suites.spider import sample_questions

    questions = sample_questions()
    if limit:
        questions = questions[:limit]
    tables_meta = {t["db_id"]: t for t in json.load(open(sp.TABLES_JSON, encoding="utf-8"))}
    dev_dbs = sorted({e["db_id"] for e in sp.load_dev()})
    pools = {"dev": dev_dbs, "all": sorted(tables_meta)}
    wanted = [p.strip() for p in os.getenv("RETRIEVAL_POOLS", "dev").split(",") if p.strip()]

    previous = load_result("retrieval") or {}
    results = dict(previous.get("pools", {}))
    status = "complete"
    with workspace("retrieval", seed=False):
        try:
            with embedding_harness(EMBEDDING_CACHE) as usage:
                for name in wanted:
                    print(f"Retrieval: pool '{name}' ({sum(len(tables_meta[d]['table_names_original']) for d in pools[name])} tables)", flush=True)
                    result = evaluate_pool(name, pools[name], tables_meta, questions, usage)
                    if str(result["embedding_backend"]).startswith("gemini"):
                        results[name] = result
                    else:
                        results[f"{name}.dry_run_offline"] = result
        except (DailyQuotaExceeded, EmbeddingUnavailableError) as e:
            status = f"paused: {type(e).__name__} (vectors cached; re-run to resume)"
            print("  " + status, flush=True)

    out = {"meta": run_metadata([sp.DEV_JSON, sp.TABLES_JSON], {"status": status, "pools_requested": wanted, "k_sweep": K_SWEEP, "product_k": PRODUCT_K}),
           "pools": results}
    print("saved", save_result("retrieval", out))
    return out
