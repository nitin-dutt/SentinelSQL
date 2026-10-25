"""
Runs the Plan A measurements one after another, resumable across days.

    python -m benchmarks.plan_a            # all runs, skipping those already complete
    python -m benchmarks.plan_a --status   # what is done

Every run writes its OWN result file, so earlier results are never overwritten:

  hold-out (100 fresh questions)        main sample (200 questions, prompt-tuned)
    spider_holdout                        spider                     (current prompt, no examples, no repair)
    spider_holdout_fewshot                spider_fewshot             (+ 3 retrieved Spider-train examples)
    spider_holdout_repair                                            (+ one repair attempt on a database error)
    spider_holdout_fewshot_repair                                    (both)
                                          spider_repair              (main sample + repair; one question triggers it)

The baselines are re-run on purpose: the generator prompt changed slightly (dialect wording) after the older Spider results were
saved, and a comparison is only valid when every arm uses the identical prompt. The raw-model arm and the intent calls come from the
response cache, so only the new generation calls cost quota. A run that stops on a quota or network problem is resumed next time.
"""

import json
import os
import subprocess
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PY = sys.executable
RESULTS = os.path.join(ROOT, "benchmarks", "results")
CHILD_LOG = os.path.join(ROOT, "benchmarks", ".work", "plan_a_child.log")

RUNS = [
    ("hold-out baseline", {"SPIDER_HOLDOUT": "1"}, "spider_holdout"),
    ("hold-out + few-shot", {"SPIDER_HOLDOUT": "1", "SPIDER_VARIANT": "fewshot"}, "spider_holdout_fewshot"),
    ("hold-out + repair", {"SPIDER_HOLDOUT": "1", "SPIDER_VARIANT": "repair"}, "spider_holdout_repair"),
    ("hold-out + few-shot + repair", {"SPIDER_HOLDOUT": "1", "SPIDER_VARIANT": "fewshot_repair"}, "spider_holdout_fewshot_repair"),
    ("main baseline", {}, "spider"),
    ("main + few-shot", {"SPIDER_VARIANT": "fewshot"}, "spider_fewshot"),
    ("main + repair", {"SPIDER_VARIANT": "repair"}, "spider_repair"),
]


def status_of(result_name: str):
    path = os.path.join(RESULTS, result_name + ".json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            r = json.load(f)
        return (r.get("meta") or {}).get("status", "complete"), len((r.get("records") or []))
    except Exception:
        return None


def say(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main() -> int:
    if "--status" in sys.argv:
        for name, _, result in RUNS:
            s = status_of(result)
            say(f"{name:30} {result:32} {'not started' if s is None else f'{s[0][:40]} ({s[1]} questions)'}")
        return 0

    for name, env, result in RUNS:
        s = status_of(result)
        if s and s[0] == "complete":
            say(f"skip {name}: already complete")
            continue
        for attempt in range(1, 4):
            say(f"start {name} (attempt {attempt}) -> {result}")
            with open(CHILD_LOG, "a", encoding="utf-8") as log:
                log.write(f"\n===== {name} attempt {attempt} {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                code = subprocess.call([PY, "-m", "benchmarks.run", "spider"], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                       env={**os.environ, "PYTHONUTF8": "1", **env})
            s = status_of(result)
            if s and s[0] == "complete":
                say(f"done {name}: {s[1]} questions")
                break
            if s and s[0].startswith("paused: daily quota"):
                say(f"STOP: {name} paused on the daily quota after {s[1]} questions; re-run later to resume")
                return 3
            say(f"{name} did not finish (exit {code}, status {s}); retrying in 30 s (a dropped connection is the usual cause)")
            time.sleep(30)
        else:
            say(f"STOP: {name} failed 3 times; see {CHILD_LOG}")
            return 2
    say("ALL RUNS COMPLETE")
    return 0


if __name__ == "__main__":
    code = main()
    say("PLAN_A_ENDED")
    sys.exit(code)
