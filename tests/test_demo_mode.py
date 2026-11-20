"""Public-demo guardrails: rate limit, daily budget, write blocking, shared logins. All inert unless DEMO_MODE=true."""

import pytest
from fastapi.testclient import TestClient

from app import demo
from app.cache.semantic_cache import get_semantic_cache
from app.main import app
from tests.helpers import ADMIN, EMPLOYEE, pipeline_writing, use_pipeline

client = TestClient(app)

SAFE_SQL = "SELECT name FROM customers LIMIT 5"     # what the stand-in pipeline's "model" writes for every question


@pytest.fixture
def demo_on(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("DEMO_PUBLIC_USERS", "emp-1,admin-1")
    # The cache drops entries when the database's change token moves, and that token is re-read at most once a second. An earlier
    # test that wrote to the demo database within that second made a fresh entry look stale here; always reading it removes the race.
    monkeypatch.setenv("DATA_VERSION_CHECK_SECONDS", "0")
    get_semantic_cache().clear()  # a leftover cached answer would be served for free and hide the budget check
    demo.budget.reset()
    demo.limiter.reset()
    yield
    demo.budget.reset()
    demo.limiter.reset()


def ask(text="Show five customers"):
    return client.post("/query", json={"query": text}, headers=EMPLOYEE)


def test_nothing_changes_when_demo_mode_is_off(monkeypatch):
    monkeypatch.delenv("DEMO_MODE", raising=False)
    assert client.get("/demo/info").json() == {"demo": False}
    long_question = "customers " * 100
    with use_pipeline(pipeline_writing(SAFE_SQL)):
        assert ask(long_question).status_code == 200


def test_info_lists_only_the_named_public_logins(demo_on):
    info = client.get("/demo/info").json()
    assert info["demo"] is True
    assert {item["user_id"] for item in info["logins"]} == {"emp-1", "admin-1"}
    assert all(item["token"] for item in info["logins"])
    assert info["questions_remaining_today"] == info["daily_cap"]


def test_info_never_lists_a_token_that_was_not_named(demo_on, monkeypatch):
    monkeypatch.setenv("DEMO_PUBLIC_USERS", "")
    assert client.get("/demo/info").json()["logins"] == []


def test_the_daily_budget_stops_questions_that_need_the_model(demo_on, monkeypatch):
    monkeypatch.setenv("DEMO_DAILY_QUERY_CAP", "2")
    monkeypatch.setenv("DEMO_RATE_PER_MINUTE", "100")
    with use_pipeline(pipeline_writing(SAFE_SQL)):
        assert ask("alpha: list every customer in Berlin").status_code == 200
        assert ask("bravo: total salary of the engineering team").status_code == 200
        blocked = ask("charlie: which products are out of stock")
    assert blocked.status_code == 429
    assert "budget" in blocked.json()["detail"]
    assert client.get("/demo/info").json()["questions_remaining_today"] == 0


def test_a_cached_answer_is_still_served_after_the_budget_is_spent(demo_on, monkeypatch):
    monkeypatch.setenv("DEMO_DAILY_QUERY_CAP", "1")
    monkeypatch.setenv("DEMO_RATE_PER_MINUTE", "100")
    question = "yankee: names of our oldest customers"
    with use_pipeline(pipeline_writing(SAFE_SQL)):
        assert ask(question).status_code == 200
        assert ask("zulu: average order value by month").status_code == 429
        again = ask(question)
    assert again.status_code == 200 and again.json()["cached"] is True


def test_the_per_visitor_rate_limit(demo_on, monkeypatch):
    monkeypatch.setenv("DEMO_RATE_PER_MINUTE", "2")
    monkeypatch.setenv("DEMO_DAILY_QUERY_CAP", "100")
    with use_pipeline(pipeline_writing(SAFE_SQL)):
        codes = [ask(f"rate question {i}: " + "xyz"[i % 3] * (i + 3)).status_code for i in range(4)]
    assert codes == [200, 200, 429, 429]


def test_the_rate_limiter_forgets_after_a_minute():
    limiter = demo._RateLimiter()
    assert limiter.allow("a", 1, now=0) and not limiter.allow("a", 1, now=10)
    assert limiter.allow("b", 1, now=10)  # another visitor is unaffected
    assert limiter.allow("a", 1, now=61)


def test_forwarded_address_is_used_only_when_the_proxy_is_trusted(demo_on, monkeypatch):
    monkeypatch.setenv("DEMO_RATE_PER_MINUTE", "1")
    monkeypatch.setenv("DEMO_DAILY_QUERY_CAP", "100")
    with use_pipeline(pipeline_writing(SAFE_SQL)):
        spoof = lambda n: client.post("/query", json={"query": f"spoof attempt {n} customers"}, headers={**EMPLOYEE, "X-Forwarded-For": f"9.9.9.{n}"})
        assert [spoof(n).status_code for n in (1, 2)] == [200, 429]  # header ignored by default: same visitor
        demo.limiter.reset()
        monkeypatch.setenv("DEMO_TRUST_PROXY", "true")
        assert [spoof(n).status_code for n in (3, 4)] == [200, 200]  # trusted: different addresses


def test_admin_changes_are_refused_but_reading_still_works(demo_on):
    assert client.delete("/admin/audit-logs", headers=ADMIN).status_code == 403
    assert client.delete("/admin/cache", headers=ADMIN).status_code == 403
    assert client.post("/admin/dictionary", json={}, headers=ADMIN).status_code == 403
    assert client.get("/admin/queue", headers=ADMIN).status_code == 200


def test_long_questions_are_refused(demo_on):
    response = ask("customers " * 100)
    assert response.status_code == 422


def test_schema_overview_describes_tables_and_flags_the_sensitive_ones():
    response = client.get("/schema/overview", headers=EMPLOYEE)
    assert response.status_code == 200
    tables = {t["name"]: t for t in response.json()["tables"]}
    assert len(tables) >= 21
    assert tables["salaries"]["sensitive"] and tables["payment_cards"]["sensitive"] and not tables["customers"]["sensitive"]
    assert tables["customers"]["description"]
    assert {"name", "type", "primary_key"} <= set(tables["customers"]["columns"][0])
    assert tables["orders"]["foreign_keys"]


def test_schema_overview_requires_a_token():
    assert client.get("/schema/overview").status_code == 401


def test_demo_info_offers_example_questions(demo_on):
    assert len(client.get("/demo/info").json()["examples"]) >= 3


def test_a_blank_question_is_refused_before_it_reaches_the_model():
    with use_pipeline(pipeline_writing(SAFE_SQL)) as pipeline:
        assert client.post("/query", json={"query": "   "}, headers=EMPLOYEE).status_code == 422
        assert client.post("/query", json={"query": ""}, headers=EMPLOYEE).status_code == 422
    assert pipeline.states == []


def test_review_decisions_are_not_rate_limited(demo_on, monkeypatch):
    monkeypatch.setenv("DEMO_RATE_PER_MINUTE", "1")
    codes = [client.post("/hitl/decision", json={"run_id": f"missing-{n}", "decision": "reject"}, headers=ADMIN).status_code for n in range(4)]
    assert codes == [200, 200, 200, 200]
