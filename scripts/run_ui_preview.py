"""
Runs SentinelSQL on http://localhost:8030 with a STUB language model, to look at the UI without an API key or quota.

Everything except the model is real: the demo database, the policy gate, masking, the audit log, tracing, the analyst chat.
The stub answers from a few canned patterns (see `stub`), so use the suggested questions below. All data goes to a
`sentinel-ui-preview` folder in the system temp directory, which is emptied on every start.

APP_ENV=dev is set, so the launch page enters without a token (as employee or reviewer, switchable from the top bar).
The tokens preview-employee and preview-admin still work under "Have an access token instead?".

Questions that show each screen state:
  How many customers are there?            answered
  Who are the top customers?               the assistant asks a clarifying question; pick an option
  now break that down by region            a follow-up, rewritten into a standalone question
  What is the average salary?              held for human review
  Delete all orders placed in 2025         blocked
"""
import json
import os
import shutil
import sys
import tempfile
import time
from types import SimpleNamespace

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

work = os.path.join(tempfile.gettempdir(), "sentinel-ui-preview")
shutil.rmtree(work, ignore_errors=True)
os.makedirs(work, exist_ok=True)
os.chdir(work)
os.environ.update({
    "AUTH_TOKENS": "preview-employee:alice:employee,preview-admin:boss:admin,preview-agent:agent-1:external_ai",
    "DATABASE_URL": "sqlite:///" + os.path.join(work, "demo.db").replace(os.sep, "/"),
    "ACTIVE_DB": "",
    "AUDIT_DB_PATH": os.path.join(work, "audit.db"),
    "HITL_DB_PATH": os.path.join(work, "hitl_runs.db"),
    "TRACE_DB_PATH": os.path.join(work, "traces.db"),
    "CONVERSATION_DB_PATH": os.path.join(work, "conversations.db"),
    "EMBEDDING_PROVIDER": "offline",
    "GEMINI_API_KEY": "",
    "GOOGLE_API_KEY": "",
    "DEMO_MODE": "false",
    "APP_ENV": "dev",                        # no token step on the launch page; "connect your own database" accepts localhost
    "WORKSPACE_DIR": os.path.join(work, "workspaces"),
    "INTENT_MODE": "llm",
    "ANALYST_CLARIFY": "true",
    "ANALYST_EXPLORE": "true",
    "LLM_PRICE_INPUT_PER_MTOK": "0.10",      # example prices so the cost tiles show a number; not a real price list
    "LLM_PRICE_OUTPUT_PER_MTOK": "0.40",
    "UI_DIST_DIR": os.path.join(ROOT, "sentinel-frontend", "dist"),
})

from app.utils import llm_factory  # noqa: E402


def _question(prompt: str) -> str:
    for opening, closing in (("<new_message>", "</new_message>"), ("<question>", "</question>")):
        if opening in prompt:
            return prompt.split(opening, 1)[1].split(closing, 1)[0].strip()
    for marker in ("User question:", "Question:"):
        if marker in prompt:
            return prompt.split(marker, 1)[1].strip().splitlines()[0].strip()
    return prompt


def stub(prompt: str):
    """Canned replies keyed on which prompt this is (planner, explorer, intent, SQL) and on words in the question."""
    q = _question(prompt).lower()
    time.sleep(0.25 + 0.01 * (len(prompt) % 40))                     # so the latency charts have something to show
    if "You prepare questions for a text-to-SQL system" in prompt:
        if "the user's answer:" in q:
            answer = q.split("the user's answer:", 1)[1].splitlines()[0].strip()
            text = json.dumps({"standalone_question": f"Who are the top 5 customers by {answer}?", "needs_clarification": False})
        elif "top customers" in q:
            text = json.dumps({"standalone_question": "Who are the top customers?", "needs_clarification": True,
                               "clarifying_question": "Top by which measure?", "options": ["total order amount", "number of orders"]})
        elif "by region" in q:
            text = json.dumps({"standalone_question": "How many customers are there in each region?", "needs_clarification": False})
        else:
            text = json.dumps({"standalone_question": _question(prompt), "needs_clarification": False})
    elif "You may look around the database first with tools" in prompt:
        looked = "sample_values(" in prompt.split("<tool_results>", 1)[1]
        text = '{"done": true}' if looked or "region" not in q else '{"tool": "sample_values", "args": {"table": "customers", "column": "region"}}'
    elif "extracts structured query intent" in prompt:
        text = '{"tables": ["customers"], "metrics": [], "group_by": [], "filters": [], "limit": null}'
    elif "salary" in q:
        text = "SELECT AVG(salary) AS average_salary FROM salaries;"
    elif "each region" in q:
        text = "SELECT region, COUNT(*) AS customers FROM customers GROUP BY region ORDER BY customers DESC;"
    elif "number of orders" in q:
        text = ("SELECT c.name, COUNT(o.order_id) AS orders FROM customers c JOIN orders o ON o.customer_id = c.customer_id "
                "GROUP BY c.name ORDER BY orders DESC LIMIT 5;")
    elif "top 5 customers" in q:
        text = ("SELECT c.name, SUM(o.amount) AS total_amount FROM customers c JOIN orders o ON o.customer_id = c.customer_id "
                "GROUP BY c.name ORDER BY total_amount DESC LIMIT 5;")
    elif "email" in q:
        text = "SELECT name, email, region FROM customers;"
    else:
        text = "SELECT COUNT(*) AS customers FROM customers;"
    return SimpleNamespace(content=text, usage_metadata={"input_tokens": len(prompt) // 4, "output_tokens": max(1, len(text) // 4)})


llm_factory.get_llm = lambda role="default": llm_factory.UniversalLLM("stub", SimpleNamespace(invoke=stub), "stub-model")
# modules that imported get_llm by name before this point keep their own reference: point them at the stub too
for name in ("app.agents.intent_agent", "app.agents.sql_generator", "app.agents.analyst", "app.experimental_llm_grading"):
    module = __import__(name, fromlist=["get_llm"])
    module.get_llm = llm_factory.get_llm

import uvicorn  # noqa: E402

print(f"UI preview with a stub model on http://localhost:8030  (temporary data in {work})")
uvicorn.run("app.main:app", port=8030)
