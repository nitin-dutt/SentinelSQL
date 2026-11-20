"""Per-request tracing: stage spans, model-call accounting, cost, storage and the admin summary."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.observability import tracing
from app.utils.llm_factory import UniversalLLM
from tests.helpers import ADMIN, EMPLOYEE, MCP

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_traces(monkeypatch):
    tracing.clear_traces()
    from app.cache.semantic_cache import get_semantic_cache
    get_semantic_cache().clear()
    yield
    tracing.clear_traces()


def _fake_model(prompt):
    """Stands in for the provider: intent JSON for the intent prompt, SQL for the generator prompt, with token counts."""
    if "extracts structured query intent" in prompt:
        text = '{"tables": ["customers"], "metrics": ["COUNT(*)"], "group_by": [], "filters": [], "limit": null}'
    else:
        text = "SELECT COUNT(*) AS total FROM customers;"
    return SimpleNamespace(content=text, usage_metadata={"input_tokens": 400, "output_tokens": 20, "total_tokens": 420})


# ------------------------------------------------------------------ the tracer itself

def test_no_trace_means_no_op():
    with tracing.span("gate") as record:
        assert record is None
    tracing.record_model_call("m", 5.0, "prompt", "out")      # must not raise
    tracing.set_status("completed")
    assert tracing.current_trace_id() is None and tracing.list_traces() == []


def test_spans_model_calls_and_storage():
    with tracing.start_trace("WEB_UI", "emp-1", "employee", "How many customers?") as trace:
        with tracing.span("generation"):
            tracing.record_model_call("model-a", 120.0, usage={"input_tokens": 1000, "output_tokens": 50})
            with tracing.span("voting"):
                pass
        with tracing.span("gate"):
            pass
        tracing.set_status("completed", rows=1)
    stored = tracing.get_trace(trace.trace_id)
    assert stored["status"] == "completed" and stored["channel"] == "WEB_UI" and stored["attrs"]["rows"] == 1
    assert [s["name"] for s in stored["spans"]] == ["generation", "voting", "gate"]
    assert [s["depth"] for s in stored["spans"]] == [0, 1, 0]
    assert stored["calls"][0]["stage"] == "generation"           # attributed to the stage, not the nested span
    assert stored["input_tokens"] == 1000 and stored["output_tokens"] == 50 and stored["tokens_estimated"] is False
    assert set(tracing.stage_times(stored["spans"])) == {"generation", "gate"}


def test_tokens_are_estimated_and_flagged_when_the_provider_reports_none():
    with tracing.start_trace("WEB_UI") as trace:
        with tracing.span("generation"):
            tracing.record_model_call("model-a", 10.0, prompt="x" * 400, output="y" * 40)
    stored = tracing.get_trace(trace.trace_id)
    assert stored["input_tokens"] == 100 and stored["output_tokens"] == 10 and stored["tokens_estimated"] is True


def test_cost_is_unknown_without_a_price_and_computed_with_one(monkeypatch):
    assert tracing.cost_usd("model-a", 1_000_000, 1_000_000) is None
    monkeypatch.setenv("LLM_PRICE_INPUT_PER_MTOK", "0.10")
    monkeypatch.setenv("LLM_PRICE_OUTPUT_PER_MTOK", "0.40")
    assert tracing.cost_usd("model-a", 1_000_000, 500_000) == pytest.approx(0.30)
    monkeypatch.setenv("LLM_PRICES_JSON", '{"model-b": {"input": 1, "output": 2}}')
    assert tracing.cost_usd("model-b", 1_000_000, 1_000_000) == pytest.approx(3.0)
    assert tracing.cost_usd("model-a", 1_000_000, 0) == pytest.approx(0.10)      # not in the table: the flat price applies


def test_a_request_with_an_unpriced_call_has_unknown_cost_not_zero():
    with tracing.start_trace("WEB_UI") as trace:
        tracing.record_model_call("model-a", 10.0, usage={"input_tokens": 10, "output_tokens": 10})
    assert tracing.get_trace(trace.trace_id)["cost_usd"] is None
    assert tracing.summary()["cost_per_1000_requests_usd"] is None


def test_tracing_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("TRACING_ENABLED", "false")
    with tracing.start_trace("WEB_UI") as trace:
        assert trace is None
        with tracing.span("gate") as record:
            assert record is None
    assert tracing.list_traces() == []


def test_an_exception_is_recorded_and_re_raised():
    with pytest.raises(RuntimeError):
        with tracing.start_trace("WEB_UI"):
            with tracing.span("execution"):
                raise RuntimeError("boom")
    stored = tracing.get_trace(tracing.list_traces()[0]["trace_id"])
    assert stored["status"] == "exception" and stored["spans"][0]["error"] == "RuntimeError"


def test_a_storage_failure_never_breaks_the_request():
    with patch.object(tracing, "_store", side_effect=OSError("disk full")):
        with tracing.start_trace("WEB_UI"):
            pass                                                   # no exception escapes


def test_old_traces_are_rotated_out(monkeypatch):
    monkeypatch.setenv("TRACE_MAX_ROWS", "100")
    for _ in range(105):
        with tracing.start_trace("WEB_UI"):
            pass
    assert len(tracing.list_traces(limit=500)) == 100


def test_percentile():
    assert tracing.percentile([], 50) is None
    assert tracing.percentile([7], 95) == 7
    assert tracing.percentile([10, 20, 30, 40], 50) == 25
    assert tracing.percentile(list(range(1, 101)), 95) == pytest.approx(95.05)


def test_summary_separates_cache_hits_and_reports_stages(monkeypatch):
    monkeypatch.setenv("LLM_PRICE_INPUT_PER_MTOK", "1")
    monkeypatch.setenv("LLM_PRICE_OUTPUT_PER_MTOK", "1")
    for _ in range(3):
        with tracing.start_trace("WEB_UI"):
            with tracing.span("generation"):
                tracing.record_model_call("model-a", 50.0, usage={"input_tokens": 900, "output_tokens": 100})
            with tracing.span("gate"):
                pass
            tracing.set_status("completed")
    with tracing.start_trace("WEB_UI"):
        with tracing.span("cache"):
            pass
        tracing.set_status("cached")
    s = tracing.summary()
    assert s["requests"] == 4 and s["pipeline_requests"] == 3 and s["cache_hits"] == 1 and s["cache_hit_pct"] == 25.0
    assert [x["stage"] for x in s["stages"]] == ["generation", "gate"]
    assert s["stages"][0]["kind"] == "model" and s["stages"][1]["kind"] == "gateway" and s["stages"][0]["n"] == 3
    assert s["tokens_per_request"] == {"input": 900.0, "output": 100.0} and s["model_calls_per_request"] == 1
    assert s["cost_per_1000_requests_usd"] == pytest.approx(1.0)          # 1,000 tokens at $1 per million, times 1,000 requests
    assert s["models"][0]["model"] == "model-a" and s["models"][0]["calls"] == 3


# ------------------------------------------------------------------ the model client

def test_the_model_client_records_latency_and_provider_token_counts():
    llm = UniversalLLM("gemini", client=SimpleNamespace(invoke=_fake_model), model_name="model-x")
    with tracing.start_trace("WEB_UI") as trace:
        with tracing.span("generation"):
            assert llm.invoke("write sql") == "SELECT COUNT(*) AS total FROM customers;"
    call = tracing.get_trace(trace.trace_id)["calls"][0]
    assert call["model"] == "model-x" and call["input_tokens"] == 400 and call["output_tokens"] == 20
    assert call["estimated"] is False and call["ok"] is True and call["stage"] == "generation"


def test_a_failed_model_call_is_recorded(monkeypatch):
    monkeypatch.setenv("LLM_MAX_RETRIES", "0")

    def boom(prompt):
        raise ValueError("bad request")
    llm = UniversalLLM("gemini", client=SimpleNamespace(invoke=boom), model_name="model-x")
    with tracing.start_trace("WEB_UI") as trace:
        with pytest.raises(Exception):
            llm.invoke("write sql")
    assert tracing.get_trace(trace.trace_id)["calls"][0]["ok"] is False


# ------------------------------------------------------------------ end to end through the real pipeline

def test_query_endpoint_traces_every_stage_of_the_real_pipeline(monkeypatch):
    monkeypatch.setenv("LLM_PRICE_INPUT_PER_MTOK", "0.10")
    monkeypatch.setenv("LLM_PRICE_OUTPUT_PER_MTOK", "0.40")
    with patch.object(UniversalLLM, "_invoke_with_retries", side_effect=lambda prompt: _fake_model(prompt)):
        response = client.post("/query", json={"query": "How many customers do we have in total?"}, headers=EMPLOYEE)
    body = response.json()
    assert response.status_code == 200 and body["status"] == "completed" and body["trace_id"]

    trace = client.get(f"/admin/observability/traces/{body['trace_id']}", headers=ADMIN).json()
    stages = tracing.stage_times(trace["spans"])
    assert {"cache", "retrieval", "router", "intent", "generation", "gate", "execution"} <= set(stages)
    assert [c["stage"] for c in trace["calls"]] == ["intent", "generation"]
    assert trace["input_tokens"] == 800 and trace["output_tokens"] == 40
    assert trace["cost_usd"] == pytest.approx((800 * 0.10 + 40 * 0.40) / 1_000_000)
    assert trace["status"] == "completed" and trace["user_id"] == "emp-1"

    # the same question again is a cache hit: one more trace, no model call
    again = client.post("/query", json={"query": "How many customers do we have in total?"}, headers=EMPLOYEE).json()
    assert again["cached"] is True
    summary = client.get("/admin/observability/summary", headers=ADMIN).json()
    assert summary["enabled"] is True and summary["requests"] == 2 and summary["cache_hits"] == 1 and summary["pipeline_requests"] == 1
    assert summary["model_calls_per_request"] == 2 and summary["cost_per_1000_requests_usd"] is not None

    listing = client.get("/admin/observability/traces?limit=10", headers=ADMIN).json()
    assert len(listing) == 2 and listing[0]["status"] == "cached" and "stages_ms" in listing[0]


def test_mcp_submissions_are_traced_with_gate_and_execution():
    out = client.post("/mcp/submit", json={"sql": "SELECT name FROM customers LIMIT 3", "rationale": "list names"}, headers=MCP).json()
    assert out["status"] == "completed"
    trace = tracing.list_traces(channel="MCP_AGENT")[0]
    assert trace["status"] == "completed" and {"gate", "execution"} <= set(trace["stages_ms"]) and trace["model_calls"] == 0


def test_observability_endpoints_are_admin_only():
    for path in ("/admin/observability/summary", "/admin/observability/traces", "/admin/observability/traces/abc"):
        assert client.get(path, headers=EMPLOYEE).status_code == 403
        assert client.get(path).status_code == 401
    assert client.get("/admin/observability/traces/does-not-exist", headers=ADMIN).status_code == 404
