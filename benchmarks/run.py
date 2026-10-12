"""
Benchmark driver:   python -m benchmarks.run <suite> [options]

Suites: security, pii, audit, cache_safety, retrieval, spider, ablation, latency, all
Live suites call Gemini (15 requests/minute free tier) and cache every response in benchmarks/cache/, so
an interrupted or quota-limited run resumes where it stopped and a finished run can be re-analysed offline.
"""

import argparse
import importlib
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

SUITES = ["security", "pii", "audit", "cache_safety", "retrieval", "spider", "ablation", "latency"]


def main() -> int:
    parser = argparse.ArgumentParser(description="SentinelSQL benchmark driver")
    parser.add_argument("suite", choices=SUITES + ["all"])
    parser.add_argument("--no-llm", action="store_true", help="run only the parts that need no model/embedding calls")
    parser.add_argument("--limit", type=int, default=None, help="cap items per live part (smoke runs)")
    args = parser.parse_args()

    chosen = SUITES if args.suite == "all" else [args.suite]
    for name in chosen:
        module = importlib.import_module(f"benchmarks.suites.{name}")
        kwargs = {}
        if "llm" in module.run.__code__.co_varnames[:module.run.__code__.co_argcount]:
            kwargs["llm"] = not args.no_llm
        if "limit" in module.run.__code__.co_varnames[:module.run.__code__.co_argcount]:
            kwargs["limit"] = args.limit
        print(f"\n=== {name} ===", flush=True)
        module.run(**kwargs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
