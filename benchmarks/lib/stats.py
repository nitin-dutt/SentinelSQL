"""
Statistics used by every suite. Small, dependency-free and unit-tested (tests/test_benchmark_lib.py),
because a benchmark is only as credible as the arithmetic under it.
"""

import math
import random
from typing import Callable, Dict, List, Sequence, Tuple


def wilson_ci(k: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    """95% Wilson score interval for a proportion k/n (well-behaved at 0% and 100%, unlike the normal one)."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def proportion(k: int, n: int) -> Dict[str, float]:
    """{'k','n','rate','lo','hi'} in percent, ready to print."""
    lo, hi = wilson_ci(k, n)
    return {"k": k, "n": n, "rate": round(100 * k / n, 2) if n else 0.0, "lo": round(100 * lo, 2), "hi": round(100 * hi, 2)}


def fmt_prop(p: Dict[str, float], digits: int = 1) -> str:
    """'95.0% (38/40, 95% CI 83.5-98.6)'"""
    return f"{p['rate']:.{digits}f}% ({p['k']}/{p['n']}, 95% CI {p['lo']:.{digits}f}-{p['hi']:.{digits}f})"


def percentile(values: Sequence[float], pct: float) -> float:
    """Linear-interpolated percentile (the same definition numpy uses by default)."""
    if not values:
        return float("nan")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * pct / 100
    lo, hi = math.floor(rank), math.ceil(rank)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (rank - lo)


def latency_summary(values_ms: Sequence[float]) -> Dict[str, float]:
    return {
        "n": len(values_ms),
        "mean_ms": round(sum(values_ms) / len(values_ms), 3) if values_ms else float("nan"),
        "p50_ms": round(percentile(values_ms, 50), 3),
        "p90_ms": round(percentile(values_ms, 90), 3),
        "p95_ms": round(percentile(values_ms, 95), 3),
        "p99_ms": round(percentile(values_ms, 99), 3),
        "min_ms": round(min(values_ms), 3) if values_ms else float("nan"),
        "max_ms": round(max(values_ms), 3) if values_ms else float("nan"),
    }


def bootstrap_ci(values: Sequence[float], stat: Callable[[Sequence[float]], float] = lambda v: sum(v) / len(v),
                 n_resamples: int = 5000, seed: int = 7) -> Tuple[float, float]:
    """Percentile bootstrap 95% CI for any statistic (default: the mean)."""
    if not values:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    n = len(values)
    stats = sorted(stat([values[rng.randrange(n)] for _ in range(n)]) for _ in range(n_resamples))
    return (stats[int(0.025 * n_resamples)], stats[int(0.975 * n_resamples) - 1])


def paired_bootstrap_diff(a: Sequence[float], b: Sequence[float], n_resamples: int = 5000, seed: int = 7) -> Dict[str, float]:
    """
    Mean of (b - a) over paired items with a bootstrap 95% CI and the fraction of resamples where the
    difference is <= 0 (a one-sided bootstrap p-value for 'b is not better than a').
    """
    assert len(a) == len(b) and a, "paired samples must be the same non-zero length"
    diffs = [y - x for x, y in zip(a, b)]
    rng = random.Random(seed)
    n = len(diffs)
    means = sorted(sum(diffs[rng.randrange(n)] for _ in range(n)) / n for _ in range(n_resamples))
    return {
        "mean_diff": sum(diffs) / n,
        "lo": means[int(0.025 * n_resamples)],
        "hi": means[int(0.975 * n_resamples) - 1],
        "p_not_better": sum(1 for m in means if m <= 0) / n_resamples,
    }


def mcnemar_exact(b: int, c: int) -> float:
    """
    Two-sided exact McNemar test for paired binary outcomes. b = items only system A got right,
    c = items only system B got right. Returns the p-value of 'the two systems are equally accurate'.
    """
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) / (2 ** n)
    return min(1.0, 2 * tail)
