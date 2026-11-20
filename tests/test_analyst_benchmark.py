"""The analyst benchmark's data must be sound before any model is asked: gold queries run, pass the policy, and labels are balanced."""

from collections import Counter

from app.db.database import execute_readonly_query
from app.utils.sql_parser import parse_and_validate_ast
from benchmarks.data.analyst_conversations import CLARIFY, CONVERSATIONS
from benchmarks.suites import analyst as suite


def test_every_gold_query_runs_returns_rows_and_needs_no_review():
    for cid, turns in CONVERSATIONS:
        assert len(turns) >= 3, cid
        for message, gold in turns:
            verdict = parse_and_validate_ast(gold)
            assert verdict.is_valid and not verdict.requires_human, f"{cid}: {gold} -> {verdict.reason}"
            assert execute_readonly_query(gold)["rows"], f"{cid}: no rows for {gold}"


def test_follow_ups_do_not_stand_alone_and_gold_changes_each_turn():
    for cid, turns in CONVERSATIONS:
        golds = [g for _, g in turns]
        assert len(set(golds)) == len(golds), cid
        for message, _ in turns[1:]:
            assert len(message.split()) <= 12, f"{cid}: a follow-up should be short and depend on the conversation: {message!r}"


def test_clarify_labels_are_balanced_and_ids_unique():
    labels = Counter(should for _, _, should, _ in CLARIFY)
    assert labels[True] == labels[False] >= 8
    ids = [i for i, *_ in CLARIFY] + [c for c, _ in CONVERSATIONS]
    assert len(ids) == len(set(ids))


def test_follow_up_summary_and_pairing():
    def rec(follow, with_ok, without_ok=None):
        r = {"is_follow_up": follow, "with_memory": {"correct": with_ok, "asked_clarification": False, "status": "completed", "agent_steps": 0}}
        if without_ok is not None:
            r["no_memory"] = {"correct": without_ok}
        return r
    summary = suite.summarise_followups([rec(False, True), rec(True, True, False), rec(True, True, False), rec(True, False, False), rec(True, True, True)])
    assert summary["first_turns_correct"]["k"] == 1 and summary["follow_ups_correct_with_memory"]["k"] == 3
    assert summary["follow_ups_correct_without_memory"]["k"] == 1
    assert summary["paired"]["only_with_memory_correct"] == 2 and summary["paired"]["only_without_memory_correct"] == 0


def test_suite_does_nothing_without_a_model():
    assert suite.run(llm=False) == {}
