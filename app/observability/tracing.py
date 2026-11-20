"""
Per-request tracing: where the time, the tokens and the money go.

One trace per request. Inside it, each pipeline stage is a span (cache, router, retrieval, intent, generation, gate,
execution, repair, planner, explore) and each model call is recorded with its model, latency and token counts.
Traces are stored in a small SQLite file (TRACE_DB_PATH, default traces.db), separate from the audited log and from the
queried database, and summarised for the admin dashboard (p50/p95 per stage, tokens per request, cost per 1,000 requests).

Design choices
  * No dependency and no network: the tracer is a context variable plus SQLite. The shape (trace, named spans with a
    start offset and a duration, attributes) is the OpenTelemetry one, so an exporter can be added without touching the
    call sites.
  * Tracing never breaks a request: every write is wrapped, and with TRACING_ENABLED=false everything is a no-op.
  * Token counts come from the provider's usage metadata when it is present; otherwise they are estimated from the text
    length (4 characters per token) and the trace is flagged `tokens_estimated`, so an estimate is never shown as a fact.
  * Cost needs prices, and prices change. They are read from LLM_PRICE_INPUT_PER_MTOK / LLM_PRICE_OUTPUT_PER_MTOK (US dollars
    per million tokens) or, per model, from LLM_PRICES_JSON. With no price configured the cost is reported as unknown
    (None), never as zero.
  * The question text is stored (admins only can read it), cut to 300 characters. Result rows are never stored.
"""

import contextlib
import contextvars
import json
import sqlite3
import threading
import time
from typing import Any, Dict, Iterator, List, Optional
from uuid import uuid4

from app.utils.logger import get_logger
from app.utils.sqlite_store import SqliteStore
from app.settings import settings

logger = get_logger("sentinel.tracing")

# Stage names in pipeline order (the dashboard shows them in this order; unknown names are appended).
STAGES = ("cache", "planner", "explore", "retrieval", "router", "intent", "generation", "gate", "execution", "repair")
MODEL_STAGES = ("planner", "explore", "router", "intent", "generation", "repair")   # stages whose time is mostly model time

_current: contextvars.ContextVar[Optional["Trace"]] = contextvars.ContextVar("sentinel_trace", default=None)
_stack: contextvars.ContextVar[tuple] = contextvars.ContextVar("sentinel_trace_stack", default=())   # names of the open spans


def tracing_enabled() -> bool:
    return settings.tracing.enabled


def _path() -> str:
    return settings.stores.trace_db


def _max_rows() -> int:
    return settings.tracing.max_rows


# ---------------------------------------------------------------------------------------------- prices

def price_for(model: str) -> Optional[Dict[str, float]]:
    """{'input': $/1M tokens, 'output': $/1M tokens} for a model, or None when no price is configured."""
    raw = settings.models.prices_json
    if raw:
        try:
            table = json.loads(raw)
            entry = table.get(model) or table.get("*")
            if entry is not None:
                return {"input": float(entry["input"]), "output": float(entry["output"])}
        except (ValueError, KeyError, TypeError, AttributeError):
            logger.warning("LLM_PRICES_JSON is not valid: expected {\"model\": {\"input\": n, \"output\": n}}")
    try:
        cin, cout = settings.models.price_input_per_mtok, settings.models.price_output_per_mtok
        if cin and cout:
            return {"input": float(cin), "output": float(cout)}
    except ValueError:
        logger.warning("LLM_PRICE_INPUT_PER_MTOK / LLM_PRICE_OUTPUT_PER_MTOK must be numbers")
    return None


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> Optional[float]:
    price = price_for(model)
    if price is None:
        return None
    return (input_tokens * price["input"] + output_tokens * price["output"]) / 1_000_000


def estimate_tokens(text: str) -> int:
    """A rough count used only when the provider reports none: about 4 characters per token."""
    return max(1, len(text or "") // 4) if text else 0


# ---------------------------------------------------------------------------------------------- the trace

class Trace:
    def __init__(self, channel: str, user_id: Optional[str], role: Optional[str], question: str):
        self.trace_id = uuid4().hex
        self.channel = channel
        self.user_id = user_id
        self.role = role
        self.question = (question or "")[:300]
        self.started_wall = time.time()
        self._t0 = time.perf_counter()
        self._lock = threading.Lock()
        self.spans: List[Dict[str, Any]] = []
        self.model_calls: List[Dict[str, Any]] = []
        self.attrs: Dict[str, Any] = {}
        self.status = "unknown"

    def _now_ms(self) -> float:
        return (time.perf_counter() - self._t0) * 1000

    def current_stage(self) -> Optional[str]:
        stack = _stack.get()
        return stack[0] if stack else None          # the outermost open span is the stage

    @contextlib.contextmanager
    def span(self, name: str, **attrs: Any) -> Iterator[Dict[str, Any]]:
        record: Dict[str, Any] = {"name": name, "start_ms": round(self._now_ms(), 2), "ms": 0.0, "depth": len(_stack.get())}
        if attrs:
            record["attrs"] = dict(attrs)
        token = _stack.set(_stack.get() + (name,))
        try:
            yield record
        except BaseException as e:
            record["error"] = type(e).__name__
            raise
        finally:
            _stack.reset(token)
            record["ms"] = round(self._now_ms() - record["start_ms"], 2)
            with self._lock:
                self.spans.append(record)

    def add_model_call(self, model: str, latency_ms: float, input_tokens: int, output_tokens: int, estimated: bool,
                       ok: bool = True) -> None:
        call = {"stage": self.current_stage() or "other", "model": model, "ms": round(latency_ms, 2),
                "input_tokens": int(input_tokens), "output_tokens": int(output_tokens), "estimated": bool(estimated), "ok": ok,
                "cost_usd": cost_usd(model, input_tokens, output_tokens)}
        with self._lock:
            self.model_calls.append(call)

    def totals(self) -> Dict[str, Any]:
        calls = self.model_calls
        costs = [c["cost_usd"] for c in calls]
        return {
            "model_calls": len(calls),
            "model_ms": round(sum(c["ms"] for c in calls), 2),
            "input_tokens": sum(c["input_tokens"] for c in calls),
            "output_tokens": sum(c["output_tokens"] for c in calls),
            "tokens_estimated": any(c["estimated"] for c in calls),
            # unknown as soon as one priced call is missing a price; 0.0 only when there really were no calls
            "cost_usd": (0.0 if not calls else None if any(c is None for c in costs) else round(sum(costs), 8)),
        }


def current_trace() -> Optional[Trace]:
    return _current.get()


@contextlib.contextmanager
def start_trace(channel: str, user_id: Optional[str] = None, role: Optional[str] = None, question: str = "") -> Iterator[Optional[Trace]]:
    """Opens a trace for one request and stores it when the block ends. Yields None when tracing is off."""
    if not tracing_enabled():
        yield None
        return
    trace = Trace(channel, user_id, role, question)
    token = _current.set(trace)
    stack_token = _stack.set(())
    try:
        yield trace
    except BaseException as e:
        if trace.status == "unknown":
            trace.status = "exception"
            trace.attrs["exception"] = type(e).__name__
        raise
    finally:
        _current.reset(token)
        _stack.reset(stack_token)
        try:
            _store(trace, round(trace._now_ms(), 2))
        except Exception as e:                       # tracing must never break a request
            logger.warning("Could not store trace: %s: %s", type(e).__name__, e)


@contextlib.contextmanager
def span(name: str, **attrs: Any) -> Iterator[Optional[Dict[str, Any]]]:
    """A named stage inside the current trace. Does nothing when no trace is open."""
    trace = _current.get()
    if trace is None:
        yield None
        return
    with trace.span(name, **attrs) as record:
        yield record


def set_status(status: str, **attrs: Any) -> None:
    trace = _current.get()
    if trace is not None:
        trace.status = status
        trace.attrs.update({k: v for k, v in attrs.items() if v is not None})


def set_attr(key: str, value: Any) -> None:
    trace = _current.get()
    if trace is not None:
        trace.attrs[key] = value


def current_trace_id() -> Optional[str]:
    trace = _current.get()
    return trace.trace_id if trace is not None else None


def record_model_call(model: str, latency_ms: float, prompt: str = "", output: str = "", usage: Optional[Dict[str, Any]] = None,
                      ok: bool = True) -> None:
    """Called by the model client after every call. `usage` is the provider's token metadata when it has any."""
    trace = _current.get()
    if trace is None:
        return
    usage = usage or {}
    tin, tout = usage.get("input_tokens"), usage.get("output_tokens")
    estimated = tin is None or tout is None
    if estimated:
        tin, tout = estimate_tokens(prompt), estimate_tokens(output)
    trace.add_model_call(model, latency_ms, int(tin or 0), int(tout or 0), estimated, ok)


def usage_from_response(response: Any) -> Optional[Dict[str, int]]:
    """Token counts from a LangChain message (`usage_metadata`), or None."""
    meta = getattr(response, "usage_metadata", None)
    if isinstance(meta, dict) and meta.get("input_tokens") is not None and meta.get("output_tokens") is not None:
        return {"input_tokens": int(meta["input_tokens"]), "output_tokens": int(meta["output_tokens"])}
    return None


# ---------------------------------------------------------------------------------------------- storage

def _create(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS traces (
            trace_id TEXT PRIMARY KEY,
            ts REAL NOT NULL,
            channel TEXT NOT NULL,
            user_id TEXT,
            role TEXT,
            question TEXT,
            status TEXT NOT NULL,
            total_ms REAL NOT NULL,
            model_ms REAL NOT NULL,
            model_calls INTEGER NOT NULL,
            input_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL,
            tokens_estimated INTEGER NOT NULL,
            cost_usd REAL,
            detail_json TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_traces_ts ON traces(ts)")


_STORE = SqliteStore(_path, _create)
_connect = _STORE.connect


def _store(trace: Trace, total_ms: float) -> None:
    totals = trace.totals()
    detail = {"spans": sorted(trace.spans, key=lambda s: s["start_ms"]), "model_calls": trace.model_calls, "attrs": trace.attrs}
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO traces VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (trace.trace_id, trace.started_wall, trace.channel, trace.user_id, trace.role, trace.question, trace.status, total_ms,
             totals["model_ms"], totals["model_calls"], totals["input_tokens"], totals["output_tokens"],
             int(totals["tokens_estimated"]), totals["cost_usd"], json.dumps(detail, default=str)),
        )
        conn.execute("DELETE FROM traces WHERE trace_id NOT IN (SELECT trace_id FROM traces ORDER BY ts DESC LIMIT ?)", (_max_rows(),))
        conn.commit()
    finally:
        conn.close()


def _row(row: sqlite3.Row, detail: bool) -> Dict[str, Any]:
    out = {k: row[k] for k in row.keys() if k != "detail_json"}
    out["tokens_estimated"] = bool(out["tokens_estimated"])
    parsed = json.loads(row["detail_json"])
    if detail:
        out.update({"spans": parsed["spans"], "calls": parsed["model_calls"], "attrs": parsed["attrs"]})
    else:
        out["stages_ms"] = stage_times(parsed["spans"])
    return out


def stage_times(spans: List[Dict[str, Any]]) -> Dict[str, float]:
    """Milliseconds per stage: top-level spans only, summed when a stage ran more than once (a repair re-validates)."""
    out: Dict[str, float] = {}
    for s in spans:
        if s.get("depth", 0) == 0:
            out[s["name"]] = round(out.get(s["name"], 0.0) + s["ms"], 2)
    return out


def list_traces(limit: int = 50, channel: Optional[str] = None, status: Optional[str] = None) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit), 500))
    sql, args = "SELECT * FROM traces", []
    clauses = [(c, v) for c, v in (("channel = ?", channel), ("status = ?", status)) if v]
    if clauses:
        sql += " WHERE " + " AND ".join(c for c, _ in clauses)
        args += [v for _, v in clauses]
    conn = _connect()
    try:
        rows = conn.execute(sql + " ORDER BY ts DESC LIMIT ?", (*args, limit)).fetchall()
    finally:
        conn.close()
    return [_row(r, detail=False) for r in rows]


def get_trace(trace_id: str) -> Optional[Dict[str, Any]]:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM traces WHERE trace_id = ?", (trace_id,)).fetchone()
    finally:
        conn.close()
    return _row(row, detail=True) if row else None


def clear_traces() -> int:
    conn = _connect()
    try:
        n = conn.execute("DELETE FROM traces").rowcount
        conn.commit()
    finally:
        conn.close()
    return n


# ---------------------------------------------------------------------------------------------- summary

def percentile(values: List[float], q: float) -> Optional[float]:
    """Linear-interpolated percentile (q in 0..100); None for an empty list."""
    if not values:
        return None
    data = sorted(values)
    if len(data) == 1:
        return round(data[0], 2)
    pos = (len(data) - 1) * q / 100
    lo = int(pos)
    hi = min(lo + 1, len(data) - 1)
    return round(data[lo] + (data[hi] - data[lo]) * (pos - lo), 2)


def _dist(values: List[float]) -> Dict[str, Any]:
    return {"n": len(values), "p50_ms": percentile(values, 50), "p95_ms": percentile(values, 95),
            "mean_ms": round(sum(values) / len(values), 2) if values else None}


def summary(hours: float = 24.0, channel: Optional[str] = None) -> Dict[str, Any]:
    """
    Aggregates the stored traces of the last `hours`. Latency and cost figures are computed over requests that reached the
    model pipeline (cache hits are counted and timed separately, because a 1 ms cache hit would hide the real p50).
    """
    since = time.time() - max(0.01, float(hours)) * 3600
    conn = _connect()
    try:
        sql, args = "SELECT * FROM traces WHERE ts >= ?", [since]
        if channel:
            sql += " AND channel = ?"
            args.append(channel)
        rows = conn.execute(sql + " ORDER BY ts", args).fetchall()
    finally:
        conn.close()

    traces = [_row(r, detail=True) for r in rows]
    cached = [t for t in traces if t["status"] == "cached"]
    piped = [t for t in traces if t["status"] != "cached"]

    per_stage: Dict[str, List[float]] = {}
    for t in piped:
        for name, ms in stage_times(t["spans"]).items():
            per_stage.setdefault(name, []).append(ms)
    order = [s for s in STAGES if s in per_stage] + sorted(s for s in per_stage if s not in STAGES)
    stages = [{"stage": s, "kind": "model" if s in MODEL_STAGES else "gateway", **_dist(per_stage[s]),
               "share_of_requests_pct": round(100 * len(per_stage[s]) / len(piped), 1)} for s in order]

    per_model: Dict[str, Dict[str, Any]] = {}
    for t in piped:
        for c in t["calls"]:
            m = per_model.setdefault(c["model"], {"model": c["model"], "calls": 0, "input_tokens": 0, "output_tokens": 0, "ms": [], "cost_usd": 0.0, "priced": True})
            m["calls"] += 1
            m["input_tokens"] += c["input_tokens"]
            m["output_tokens"] += c["output_tokens"]
            m["ms"].append(c["ms"])
            if c.get("cost_usd") is None:
                m["priced"] = False
            else:
                m["cost_usd"] += c["cost_usd"]
    models = [{"model": m["model"], "calls": m["calls"], "input_tokens": m["input_tokens"], "output_tokens": m["output_tokens"],
               "p50_ms": percentile(m["ms"], 50), "p95_ms": percentile(m["ms"], 95),
               "cost_usd": round(m["cost_usd"], 6) if m["priced"] else None} for m in per_model.values()]

    n = len(piped)
    costs = [t["cost_usd"] for t in piped]
    cost_known = bool(piped) and all(c is not None for c in costs)
    statuses: Dict[str, int] = {}
    for t in traces:
        statuses[t["status"]] = statuses.get(t["status"], 0) + 1
    return {
        "window_hours": hours,
        "requests": len(traces),
        "pipeline_requests": n,
        "cache_hits": len(cached),
        "cache_hit_pct": round(100 * len(cached) / len(traces), 1) if traces else None,
        "statuses": statuses,
        "total_ms": _dist([t["total_ms"] for t in piped]),
        "model_ms": _dist([t["model_ms"] for t in piped]),
        "gateway_ms": _dist([max(0.0, t["total_ms"] - t["model_ms"]) for t in piped]),
        "cache_hit_ms": _dist([t["total_ms"] for t in cached]),
        "stages": stages,
        "models": models,
        "model_calls_per_request": round(sum(t["model_calls"] for t in piped) / n, 2) if n else None,
        "tokens_per_request": ({"input": round(sum(t["input_tokens"] for t in piped) / n, 1),
                                "output": round(sum(t["output_tokens"] for t in piped) / n, 1)} if n else None),
        "tokens_estimated": any(t["tokens_estimated"] for t in piped),
        "cost_per_1000_requests_usd": round(1000 * sum(costs) / n, 4) if cost_known else None,
        "price_configured": cost_known or (not piped and price_for("*") is not None),
    }
