"""
Tests for the benchmark harness (benchmarks/lib). A benchmark result is only as trustworthy as the
statistics, comparison logic and caching underneath it.
"""

import json
import time
from collections import Counter

import pytest

from benchmarks.lib import llm as bench_llm
from benchmarks.lib.sqlcompare import rows_match, gold_has_order_by
from benchmarks.lib.spider import eval_hardness, load_dev, gold_tables
from benchmarks.lib.stats import (bootstrap_ci, fmt_prop, latency_summary, mcnemar_exact, paired_bootstrap_diff,
                                  percentile, proportion, wilson_ci)


# ---------------------------------------------------------------- statistics

def test_wilson_interval_matches_published_values():
    lo, hi = wilson_ci(0, 10)
    assert lo == 0.0 and hi == pytest.approx(0.2775, abs=1e-3)
    lo, hi = wilson_ci(10, 10)
    assert hi == 1.0 and lo == pytest.approx(0.7225, abs=1e-3)
    lo, hi = wilson_ci(38, 40)
    assert (lo, hi) == (pytest.approx(0.8350, abs=1e-3), pytest.approx(0.9861, abs=1e-3))   # worked by hand
    lo, hi = wilson_ci(5, 10)
    assert (lo, hi) == (pytest.approx(0.2366, abs=1e-3), pytest.approx(0.7634, abs=1e-3))   # textbook example
    assert wilson_ci(0, 0) == (0.0, 0.0)


def test_proportion_formatting():
    p = proportion(38, 40)
    assert p["rate"] == 95.0 and "38/40" in fmt_prop(p) and "95% CI" in fmt_prop(p)


def test_percentiles_use_linear_interpolation():
    values = [1, 2, 3, 4, 5]
    assert percentile(values, 50) == 3 and percentile(values, 0) == 1 and percentile(values, 100) == 5
    assert percentile(values, 90) == pytest.approx(4.6)
    assert latency_summary([10.0, 20.0, 30.0])["p50_ms"] == 20.0


def test_mcnemar_exact_known_values():
    assert mcnemar_exact(0, 0) == 1.0
    assert mcnemar_exact(0, 5) == pytest.approx(0.0625)
    assert mcnemar_exact(5, 5) == 1.0
    assert mcnemar_exact(1, 9) == pytest.approx(0.0215, abs=1e-3)


def test_bootstrap_is_reproducible_and_brackets_the_mean():
    data = [1, 0, 1, 1, 0, 1, 1, 1, 0, 1]
    assert bootstrap_ci(data) == bootstrap_ci(data)
    lo, hi = bootstrap_ci(data)
    assert lo <= sum(data) / len(data) <= hi


def test_paired_bootstrap_detects_a_real_improvement():
    a = [0, 0, 1, 0, 0, 1, 0, 0, 0, 1] * 3
    b = [1, 1, 1, 1, 0, 1, 1, 1, 0, 1] * 3
    diff = paired_bootstrap_diff(a, b)
    assert diff["mean_diff"] > 0.3 and diff["lo"] > 0 and diff["p_not_better"] < 0.01
    same = paired_bootstrap_diff(a, a)
    assert same["mean_diff"] == 0 and same["p_not_better"] == 1.0


# ---------------------------------------------------------------- execution-match comparison

def test_row_order_is_ignored_unless_the_gold_query_orders():
    gold = [(1, "a"), (2, "b"), (3, "c")]
    shuffled = [(3, "c"), (1, "a"), (2, "b")]
    assert rows_match(gold, shuffled, order_matters=False)
    assert not rows_match(gold, shuffled, order_matters=True)
    assert rows_match(gold, list(gold), order_matters=True)


def test_column_order_is_ignored():
    assert rows_match([(1, "a"), (2, "b")], [("a", 1), ("b", 2)], order_matters=False)
    assert rows_match([(1, "a"), (2, "b")], [("a", 1), ("b", 2)], order_matters=True)


def test_duplicate_valued_columns_do_not_confuse_the_matching():
    assert rows_match([(1, 1, "x"), (2, 2, "y")], [("x", 1, 1), ("y", 2, 2)], order_matters=False)
    assert not rows_match([(1, 2, "x")], [(2, 2, "x")], order_matters=False)


def test_numbers_compare_numerically_and_multisets_count_duplicates():
    assert rows_match([(1,), (2.0,)], [(1.0,), (2,)], order_matters=False)
    assert not rows_match([(1,), (1,), (2,)], [(1,), (2,), (2,)], order_matters=False)
    assert not rows_match([(1,), (2,)], [(1,), (2,), (3,)], order_matters=False)
    assert rows_match([], [], order_matters=False)
    assert not rows_match([(1,)], [(1, 2)], order_matters=False)
    assert not rows_match([("A",)], [("a",)], order_matters=False)          # case-sensitive, as in SQLite


def test_gold_order_by_detection():
    assert gold_has_order_by("SELECT a FROM t ORDER BY a") and not gold_has_order_by("SELECT a FROM t")


# ---------------------------------------------------------------- Spider tooling

def test_hardness_port_reproduces_the_official_dev_distribution():
    counts = Counter(eval_hardness(ex["sql"]) for ex in load_dev())
    assert dict(counts) == {"easy": 248, "medium": 446, "hard": 174, "extra": 166}


def test_gold_table_extraction():
    assert gold_tables("SELECT a.x FROM Singer a JOIN concert b ON a.id = b.id") == ["concert", "singer"]
    assert gold_tables("WITH t AS (SELECT 1) SELECT * FROM t JOIN real_table r ON 1=1") == ["real_table"]


# ---------------------------------------------------------------- LLM harness

def make_llm(cls, invoke):
    """An UniversalLLM whose underlying call is `invoke`, with no network involved."""
    cls.invoke = invoke
    obj = cls.__new__(cls)
    obj.provider, obj.client, obj.model_name = "fake", None, "fake-model"
    return obj


@pytest.fixture
def llm_class(monkeypatch):
    from app.utils import llm_factory
    original = llm_factory.UniversalLLM.invoke
    yield llm_factory.UniversalLLM
    llm_factory.UniversalLLM.invoke = original


def counting_invoke():
    state = {"n": 0}

    def invoke(self, prompt):
        state["n"] += 1
        return f"answer-{state['n']}"

    return invoke, state


def test_responses_are_cached_and_survive_a_restart(llm_class, tmp_path):
    invoke, state = counting_invoke()
    cache_path = str(tmp_path / "c.jsonl")
    llm = make_llm(llm_class, invoke)
    with bench_llm.harness(cache_path, rpm=1000) as usage:
        first = llm.invoke("hello")
        assert llm.invoke("hello") == first
        assert usage.live_calls == 1 and usage.cache_hits == 1
    llm = make_llm(llm_class, invoke)                                # a new process would start from here
    with bench_llm.harness(cache_path, rpm=1000) as usage:
        assert llm.invoke("hello") == first
        assert usage.live_calls == 0 and usage.cache_hits == 1 and state["n"] == 1


def test_tags_make_repetitions_independent_samples(llm_class, tmp_path):
    invoke, _ = counting_invoke()
    llm = make_llm(llm_class, invoke)
    with bench_llm.harness(str(tmp_path / "c.jsonl"), rpm=1000) as usage:
        with bench_llm.run_tag("r1"):
            a = llm.invoke("same prompt")
        with bench_llm.run_tag("r2"):
            b = llm.invoke("same prompt")
        with bench_llm.run_tag("r1"):
            assert llm.invoke("same prompt") == a
        assert a != b and usage.live_calls == 2 and usage.cache_hits == 1


def test_a_truncated_last_cache_line_is_ignored(tmp_path):
    path = tmp_path / "c.jsonl"
    path.write_text(json.dumps({"key": "k1", "response": "ok"}) + "\n" + '{"key": "k2", "resp')
    cache = bench_llm.LLMCache(str(path))
    assert cache.get("k1")["response"] == "ok" and cache.get("k2") is None


def test_cache_can_be_disabled_for_latency_measurements(llm_class, tmp_path):
    invoke, _ = counting_invoke()
    llm = make_llm(llm_class, invoke)
    with bench_llm.harness(str(tmp_path / "c.jsonl"), rpm=1000, use_cache=False) as usage:
        llm.invoke("x")
        llm.invoke("x")
        assert usage.live_calls == 2 and usage.cache_hits == 0


def test_rate_limiter_never_exceeds_its_window():
    limiter = bench_llm.RateLimiter(rpm=3, window_s=0.4)
    started = time.monotonic()
    stamps = []
    for _ in range(7):
        limiter.acquire()
        stamps.append(time.monotonic() - started)
    for i in range(3, 7):                       # the 4th call can only happen once the 1st left the window
        assert stamps[i] - stamps[i - 3] >= 0.39
    assert limiter.waited_s > 0.5


def test_daily_quota_stops_the_run_instead_of_retrying(llm_class, tmp_path):
    from app.utils import llm_factory

    def exhausted(self, prompt):
        raise llm_factory.LLMUnavailableError("x") from RuntimeError("quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier")

    llm = make_llm(llm_class, exhausted)
    with bench_llm.harness(str(tmp_path / "c.jsonl"), rpm=1000):
        with pytest.raises(bench_llm.DailyQuotaExceeded):
            llm.invoke("q")


def test_provider_hiccups_are_retried_not_recorded_as_failures(llm_class, tmp_path, monkeypatch):
    from app.utils import llm_factory
    attempts = {"n": 0}

    def flaky(self, prompt):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise llm_factory.LLMUnavailableError("x") from RuntimeError("429 retryDelay: '0s'")
        return "finally"

    llm = make_llm(llm_class, flaky)
    monkeypatch.setattr(bench_llm.time, "sleep", lambda s: None)
    with bench_llm.harness(str(tmp_path / "c.jsonl"), rpm=1000) as usage:
        assert llm.invoke("q") == "finally"
        assert usage.harness_retries == 2 and usage.live_calls == 1


# ---------------------------------------------------------------- embedding harness

def test_embedding_vectors_are_cached_batched_and_daily_quota_is_fatal(tmp_path, monkeypatch):
    from langchain_google_genai import GoogleGenerativeAIEmbeddings as Client
    calls = []

    def fake_docs(self, texts, *a, **k):
        calls.append(list(texts))
        return [[float(len(t)), 0.5, -1.0] for t in texts]

    monkeypatch.setattr(Client, "embed_documents", fake_docs)
    monkeypatch.setattr(Client, "embed_query", lambda self, t, *a, **k: [0.0])
    client = Client.model_construct(model="models/x", output_dimensionality=3)
    path = str(tmp_path / "e.jsonl")
    with bench_llm.embedding_harness(path, texts_per_minute=10_000) as usage:
        first = client.embed_documents(["aa", "bbb", "aa"])
        assert first[0] == first[2] and first[1][0] == 3.0
        client.embed_query("aa")
        assert usage.cache_hits == 1 and usage.live_texts == 3
    with bench_llm.embedding_harness(path, texts_per_minute=10_000) as usage:   # restart: nothing live
        assert client.embed_documents(["aa", "bbb"])[1][0] == 3.0
        assert usage.live_texts == 0 and usage.cache_hits == 2

    def exhausted(self, texts, *a, **k):
        raise RuntimeError("quotaId EmbedContentRequestsPerDayPerUserPerProjectPerModel-FreeTier")

    monkeypatch.setattr(Client, "embed_documents", exhausted)
    with bench_llm.embedding_harness(str(tmp_path / "e2.jsonl"), texts_per_minute=10_000):
        with pytest.raises(bench_llm.DailyQuotaExceeded):
            client.embed_documents(["new text"])


def test_embedding_harness_forces_strict_mode_and_restores_it(tmp_path):
    import os
    os.environ.pop("EMBEDDING_STRICT", None)
    with bench_llm.embedding_harness(str(tmp_path / "e.jsonl")):
        assert os.environ["EMBEDDING_STRICT"] == "1"
    assert "EMBEDDING_STRICT" not in os.environ
