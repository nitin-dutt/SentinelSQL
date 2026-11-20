"""
Evaluation gate:   python -m benchmarks.gate [--thresholds FILE] [--only offline|committed]

Reads result files in benchmarks/results/ and fails (exit code 1) when a number is on the wrong side of its threshold in
benchmarks/gate_thresholds.json. CI runs it after the offline suites, so a change that weakens the policy engine, the PII
masking or the red-team containment cannot be merged without someone seeing it and editing a threshold on purpose.

Two kinds of check:
  offline     the result file was produced in this run by a suite that needs no model (red team, security SQL ingress, PII,
              audit). These prove the code as it is now.
  committed   the result file comes from a live-model run that is committed to the repository (Spider accuracy, the
              natural-language security run). CI has no model key, so this only proves that the committed numbers were not
              regressed or replaced by an incomplete run; the live suites must be re-run by hand (or by a scheduled job
              with a key) for the numbers to move.

A check needs "file" (result name), "path" (dotted path to a number) and "min" and/or "max". Optional: "kind" (offline by
default), "require_status_complete" (the file's meta.status must be "complete"), "optional" (a missing file or path is
reported but does not fail).
"""

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(HERE, "results")
DEFAULT_THRESHOLDS = os.path.join(HERE, "gate_thresholds.json")


def dig(data: Any, path: str) -> Any:
    """Follows a dotted path through dicts and lists (`a.b.0.c`). Raises KeyError with the path that was missing."""
    current = data
    for part in path.split("."):
        try:
            current = current[int(part)] if isinstance(current, list) else current[part]
        except (KeyError, IndexError, ValueError, TypeError):
            raise KeyError(path)
    return current


def evaluate(check: Dict[str, Any], results_dir: str = RESULTS_DIR) -> Tuple[str, Optional[float], str]:
    """Returns (verdict, value, note) where verdict is PASS, FAIL or SKIP."""
    optional = bool(check.get("optional"))
    path = os.path.join(results_dir, check["file"] + ".json")
    if not os.path.exists(path):
        return ("SKIP" if optional else "FAIL"), None, "result file is missing"
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except ValueError:
        return "FAIL", None, "result file is not valid JSON"
    if check.get("require_status_complete"):
        status = str((data.get("meta") or {}).get("status", ""))
        if status != "complete":
            return "FAIL", None, f"run status is '{status[:40]}', not 'complete' (a partial run must not be quoted)"
    try:
        value = dig(data, check["path"])
    except KeyError:
        return ("SKIP" if optional else "FAIL"), None, "path not found in the result file"
    if isinstance(value, (list, dict)):
        value = len(value)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "FAIL", None, f"value is not a number: {value!r}"
    if "min" in check and value < check["min"]:
        return "FAIL", value, f"below the minimum {check['min']}"
    if "max" in check and value > check["max"]:
        return "FAIL", value, f"above the maximum {check['max']}"
    return "PASS", value, ""


def run_gate(thresholds_file: str = DEFAULT_THRESHOLDS, results_dir: str = RESULTS_DIR, only: Optional[str] = None) -> Tuple[bool, List[Dict[str, Any]]]:
    with open(thresholds_file, encoding="utf-8") as f:
        checks = json.load(f)["checks"]
    rows = []
    for check in checks:
        kind = check.get("kind", "offline")
        if only and kind != only:
            continue
        verdict, value, note = evaluate(check, results_dir)
        bound = " and ".join(f"{op} {check[key]}" for key, op in (("min", ">="), ("max", "<=")) if key in check)
        rows.append({"name": check["name"], "kind": kind, "verdict": verdict, "value": value, "bound": bound, "note": note})
    return all(r["verdict"] != "FAIL" for r in rows), rows


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="SentinelSQL evaluation gate")
    parser.add_argument("--thresholds", default=DEFAULT_THRESHOLDS)
    parser.add_argument("--results", default=RESULTS_DIR)
    parser.add_argument("--only", choices=["offline", "committed"], default=None)
    args = parser.parse_args(argv)

    ok, rows = run_gate(args.thresholds, args.results, args.only)
    width = max((len(r["name"]) for r in rows), default=10)
    print(f"{'check'.ljust(width)}  kind       value      required     result")
    for r in rows:
        value = "-" if r["value"] is None else f"{r['value']:g}"
        print(f"{r['name'].ljust(width)}  {r['kind']:<9}  {value:<9}  {r['bound']:<11}  {r['verdict']}{'  (' + r['note'] + ')' if r['note'] else ''}")
    failed = [r for r in rows if r["verdict"] == "FAIL"]
    print(f"\n{len(rows) - len(failed)} of {len(rows)} checks passed." + ("" if ok else "  GATE FAILED: " + "; ".join(r["name"] for r in failed)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
