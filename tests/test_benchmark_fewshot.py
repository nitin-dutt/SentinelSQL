"""The few-shot pool drawn from the Spider train split: reproducible, balanced, and unable to leak dev data into a prompt."""

import pytest

from benchmarks.lib import fewshot
from benchmarks.lib import spider as sp

pytestmark = pytest.mark.skipif(not __import__("os").path.exists(fewshot.TRAIN_JSON), reason="Spider train split not downloaded")


@pytest.fixture(scope="module")
def pool():
    return fewshot.build_pool()


def test_the_pool_has_the_planned_size_and_equal_tiers(pool):
    assert len(pool) == fewshot.POOL_SIZE == 500
    for tier in fewshot.TIERS:
        assert sum(e["hardness"] == tier for e in pool) == 125


def test_the_pool_is_reproducible(pool):
    assert fewshot.build_pool() == pool
    assert fewshot.build_pool(seed=8) != pool


def test_no_dev_database_or_dev_question_can_reach_a_prompt(pool):
    dev = sp.load_dev()
    fewshot.assert_disjoint_from_dev(pool, dev)
    dev_questions = {d["question"].strip().lower() for d in dev}
    assert not [e for e in pool if e["question"].lower() in dev_questions]


def test_the_disjointness_check_really_fails_when_a_database_is_shared(pool):
    leaky = pool + [{"db_id": sp.load_dev()[0]["db_id"], "question": "x", "sql": "SELECT 1", "hardness": "easy", "index": -1}]
    with pytest.raises(AssertionError):
        fewshot.assert_disjoint_from_dev(leaky)


def test_example_sql_is_whitespace_normalised(pool):
    assert fewshot.normalise_sql("SELECT  a   FROM t WHERE x  >  5") == "SELECT a FROM t WHERE x > 5"
    assert all("  " not in e["sql"] and "\n" not in e["sql"] for e in pool)
