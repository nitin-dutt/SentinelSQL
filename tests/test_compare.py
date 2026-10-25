from benchmarks.compare import compare_results


def rec(index, tier, correct, **extra):
    return {"index": index, "hardness": tier, "question": f"q{index}", "governed": {"correct": correct, **extra}}


def test_paired_comparison_counts_only_discordant_questions():
    base = {"records": [rec(1, "easy", True), rec(2, "easy", False), rec(3, "hard", False), rec(4, "hard", True), rec(5, "hard", False)]}
    var = {"records": [rec(1, "easy", True), rec(2, "easy", True), rec(3, "hard", True), rec(4, "hard", False), rec(5, "hard", False)]}
    c = compare_results(base, var)
    assert c["n_shared"] == 5
    assert c["base"]["k"] == 2 and c["variant"]["k"] == 3 and c["difference_points"] == 20.0
    assert c["only_variant_correct"] == 2 and c["only_base_correct"] == 1
    assert c["by_tier"]["hard"] == {"n": 3, "base": 1, "variant": 1}
    assert c["fixed_by_variant"] == ["q2", "q3"] and c["broken_by_variant"] == ["q4"]


def test_only_questions_answered_in_both_files_are_compared():
    base = {"records": [rec(1, "easy", True), rec(2, "easy", True)]}
    var = {"records": [rec(2, "easy", False), rec(3, "easy", True)]}
    c = compare_results(base, var)
    assert c["n_shared"] == 1 and c["only_base_correct"] == 1


def test_no_shared_questions_gives_none():
    assert compare_results({"records": [rec(1, "easy", True)]}, {"records": [rec(9, "easy", True)]}) is None
    assert compare_results(None, None) is None


def test_repair_attempts_that_fixed_the_answer_are_counted():
    base = {"records": [rec(1, "easy", False), rec(2, "easy", False)]}
    var = {"records": [rec(1, "easy", True, repaired=True), rec(2, "easy", False, repaired=True)]}
    c = compare_results(base, var)
    assert c["repairs_attempted"] == 2 and c["repairs_that_fixed"] == 1
