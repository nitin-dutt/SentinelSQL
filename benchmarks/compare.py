"""
Paired comparison of Spider result files: the same questions answered by two configurations of the governed pipeline.

    python -m benchmarks.compare spider_holdout spider_holdout_fewshot

Only questions answered in BOTH files are compared. The test is McNemar's exact test on the discordant pairs (questions one
configuration got right and the other got wrong), which is the right test for two systems answering the same questions.
"""

import os
import sys
from typing import Any, Dict, Optional

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from benchmarks.lib.stats import mcnemar_exact, proportion  # noqa: E402
from benchmarks.suites.common import load_result  # noqa: E402


def compare_results(base: Dict[str, Any], variant: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Paired comparison of the GOVERNED arm in two result dicts, or None when they share no questions."""
    b = {r["index"]: r for r in (base or {}).get("records", [])}
    v = {r["index"]: r for r in (variant or {}).get("records", [])}
    shared = sorted(set(b) & set(v))
    if not shared:
        return None
    ok = lambda rec: bool(rec["governed"]["correct"])
    only_base = [i for i in shared if ok(b[i]) and not ok(v[i])]
    only_var = [i for i in shared if ok(v[i]) and not ok(b[i])]
    k_base, k_var = sum(ok(b[i]) for i in shared), sum(ok(v[i]) for i in shared)
    by_tier: Dict[str, Dict[str, int]] = {}
    for i in shared:
        t = by_tier.setdefault(b[i]["hardness"], {"n": 0, "base": 0, "variant": 0})
        t["n"] += 1
        t["base"] += ok(b[i])
        t["variant"] += ok(v[i])
    repaired = [i for i in shared if v[i]["governed"].get("repaired")]
    return {
        "n_shared": len(shared),
        "base": proportion(k_base, len(shared)),
        "variant": proportion(k_var, len(shared)),
        "difference_points": round(100 * (k_var - k_base) / len(shared), 1),
        "only_base_correct": len(only_base),
        "only_variant_correct": len(only_var),
        "mcnemar_p": round(mcnemar_exact(len(only_base), len(only_var)), 4),
        "by_tier": by_tier,
        "fixed_by_variant": [b[i]["question"] for i in only_var][:10],
        "broken_by_variant": [b[i]["question"] for i in only_base][:10],
        "repairs_attempted": len(repaired),
        "repairs_that_fixed": sum(1 for i in repaired if ok(v[i]) and not ok(b[i])),
    }


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    base, variant = load_result(sys.argv[1]), load_result(sys.argv[2])
    if base is None or variant is None:
        print("missing result file:", [n for n, r in ((sys.argv[1], base), (sys.argv[2], variant)) if r is None])
        return 1
    c = compare_results(base, variant)
    if not c:
        print("no shared questions")
        return 1
    f = lambda p: f"{p['rate']:.1f}% ({p['k']}/{p['n']}), 95% CI {p['lo']:.1f}-{p['hi']:.1f}%"
    print(f"{sys.argv[1]}  vs  {sys.argv[2]}   ({c['n_shared']} shared questions)")
    print("  baseline:", f(c["base"]))
    print("  variant :", f(c["variant"]), f"  ({c['difference_points']:+.1f} points)")
    print(f"  discordant: only baseline right {c['only_base_correct']}, only variant right {c['only_variant_correct']}  -> McNemar p = {c['mcnemar_p']}")
    print("  by tier:", {t: f"{v['base']}->{v['variant']} of {v['n']}" for t, v in c["by_tier"].items()})
    if c["repairs_attempted"]:
        print(f"  repair attempts: {c['repairs_attempted']}, that fixed the answer: {c['repairs_that_fixed']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
