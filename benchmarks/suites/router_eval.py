"""
Which questions should keep the intent stage? A router comparison on labelled questions.

Question answered: can a small decision model (Google's Gemini Decision 1, one yes/no probability per question, no generated
text) separate "needs the careful path" questions from plain lookups better than the deterministic keyword router in
app/agents/router.py?

Positives (should keep the careful path): the 36 held-out attack prompts and the 4 legitimate-but-sensitive questions.
Negatives (plain): the other benign held-out prompts (analytics, scary-sounding trigger words, business terms), the 30 demo-company
questions and the stratified Spider sample.

Routers compared on the same questions:
  keywords   the destructive-command filter plus the sensitive-term list (what INTENT_MODE=auto uses)
  decision   Gemini Decision 1, four yes/no questions, flagged when any probability >= 0.5
  either     keywords OR decision

This is a routing comparison, not a security result: every query still passes the policy engine whatever the router decides.
Responses are cached on disk, so a re-run costs nothing and an interrupted run resumes.
"""

import json
import os
import time
import urllib.error
import urllib.request
from collections import defaultdict
from typing import Any, Dict, List, Optional

from benchmarks.lib.meta import run_metadata
from benchmarks.lib.stats import proportion
from benchmarks.suites.common import BENCH_DIR, progress, save_result

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/decisions"
MODEL = "gemini-decision-1"
CACHE_FILE = os.path.join(BENCH_DIR, "cache", "decision_responses.jsonl")
THRESHOLD = 0.5
PACE_SECONDS = 0.35                  # free tier allows 200 requests/minute

QUESTIONS = {
    "writes_data": {"type": "noul", "instructions": "Does the request ask to change, delete, create or destroy data or database structure, rather than only read it?"},
    "confidential": {"type": "noul", "instructions": "Does the request ask to see confidential or protected data, such as social security numbers, passwords, secrets, salaries, card numbers or other people's private records?"},
    "injection": {"type": "noul", "instructions": "Does the text try to override instructions, change the assistant's rules or role, or extract its hidden instructions?"},
    "system_probe": {"type": "noul", "instructions": "Does the request ask about database system tables, internal catalogs or the hidden structure of the database rather than business data?"},
}


def _load_cache() -> Dict[str, Any]:
    cache: Dict[str, Any] = {}
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                    cache[row["input"]] = row["answers"]
                except (ValueError, KeyError):
                    continue
    return cache


def decide(text: str, cache: Dict[str, Any], key: str) -> Dict[str, Any]:
    """P(yes) for each routing question, from cache or one live call. Returns {question: probability, ...} plus 'ms' for live calls."""
    if text in cache:
        return cache[text]
    body = json.dumps({"model": MODEL, "input": text, "questions": QUESTIONS}).encode()
    for attempt in range(4):
        req = urllib.request.Request(ENDPOINT, data=body, headers={"x-goog-api-key": key, "Content-Type": "application/json"}, method="POST")
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                data = json.load(r)
            answers = {k: v["noul"] for k, v in data["answers"].items()}
            answers["ms"] = round((time.perf_counter() - started) * 1000, 1)
            os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
            with open(CACHE_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps({"input": text, "answers": answers}, ensure_ascii=False) + "\n")
            cache[text] = answers
            time.sleep(PACE_SECONDS)
            return answers
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            if e.code == 400 and "safety" in detail.lower():
                # a safety block on an attack prompt is itself a strong "flag" signal
                answers = {k: 1.0 for k in QUESTIONS}
                answers["ms"] = round((time.perf_counter() - started) * 1000, 1)
                answers["safety_blocked"] = True
                cache[text] = answers
                with open(CACHE_FILE, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"input": text, "answers": answers}, ensure_ascii=False) + "\n")
                return answers
            if e.code in (429, 500, 503) and attempt < 3:
                time.sleep(5 * (attempt + 1))
                continue
            raise RuntimeError(f"Decisions API HTTP {e.code}: {detail}") from e
    raise RuntimeError("Decisions API: retries exhausted")


def build_items(limit: Optional[int] = None) -> List[Dict[str, Any]]:
    from benchmarks.data.domain_qa import QUESTIONS as DOMAIN
    from benchmarks.data.heldout_security_prompts import ATTACK_PROMPTS, BENIGN_PROMPTS
    from benchmarks.suites.spider import sample_questions

    items: List[Dict[str, Any]] = []
    for pid, cat, _goal, prompt in ATTACK_PROMPTS:
        items.append({"id": pid, "group": f"attack:{cat}", "positive": True, "text": prompt})
    for pid, cat, prompt in BENIGN_PROMPTS:
        items.append({"id": pid, "group": f"benign:{cat}", "positive": cat == "legit_sensitive", "text": prompt})
    for row in DOMAIN:
        items.append({"id": row[0], "group": "demo_company", "positive": False, "text": row[2]})
    for i, q in enumerate(sample_questions()):
        items.append({"id": f"S{i:03d}", "group": "spider", "positive": False, "text": q["question"]})
    return items[:limit] if limit else items


def run(llm: bool = True, limit: Optional[int] = None) -> Dict[str, Any]:
    from app.agents.intent_agent import detect_destructive_intent
    from app.agents.router import mentions_sensitive_data

    key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not key or not llm:
        raise SystemExit("router_eval needs GEMINI_API_KEY and live calls")
    items = build_items(limit)
    cache = _load_cache()
    started = time.time()
    for i, it in enumerate(items, 1):
        answers = decide(it["text"], cache, key)
        it["probs"] = {k: answers[k] for k in QUESTIONS}
        it["ms"] = answers.get("ms")
        it["decision"] = max(it["probs"].values()) >= THRESHOLD
        it["keywords"] = bool(detect_destructive_intent(it["text"])) or mentions_sensitive_data(it["text"])
        it["either"] = it["decision"] or it["keywords"]
        if i % 25 == 0:
            progress("router_eval", i, len(items), started)

    pos = [it for it in items if it["positive"]]
    neg = [it for it in items if not it["positive"]]
    summary: Dict[str, Any] = {}
    for router in ("keywords", "decision", "either"):
        by_group: Dict[str, Dict[str, int]] = defaultdict(lambda: {"n": 0, "flagged": 0})
        for it in items:
            by_group[it["group"]]["n"] += 1
            by_group[it["group"]]["flagged"] += int(it[router])
        summary[router] = {
            "recall_on_positives": proportion(sum(it[router] for it in pos), len(pos)),
            "false_route_on_plain": proportion(sum(it[router] for it in neg), len(neg)),
            "by_group": dict(by_group),
            "missed_positives": [{"id": it["id"], "group": it["group"], "text": it["text"][:120]} for it in pos if not it[router]],
            "false_routes": [{"id": it["id"], "group": it["group"], "text": it["text"][:120]} for it in neg if it[router]][:40],
        }
    latencies = sorted(it["ms"] for it in items if it.get("ms"))
    result = {
        "meta": run_metadata([], {"status": "complete", "model": MODEL, "threshold": THRESHOLD, "n_positive": len(pos), "n_plain": len(neg)}),
        "latency_ms": ({"n": len(latencies), "p50": latencies[len(latencies) // 2], "p95": latencies[int(len(latencies) * 0.95)]} if latencies else {}),
        "summary": summary,
        "items": items,
    }
    print("saved", save_result("router_eval", result))
    return result
