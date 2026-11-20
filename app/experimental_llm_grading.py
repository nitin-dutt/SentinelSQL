"""
EXPERIMENTAL, off by default: VALIDATION_MODE=llm.

After the AST policy has allowed a query, a model is asked how well the SQL answers the question (0-100) and that number
replaces the policy engine's verdict: above 90 the query runs, 50 to 90 it is held for review, below 50 it is blocked.
A failed model call counts as 0, so it blocks the query. It never loosens a policy block or a policy hold, because
`judge` (app/governance.py) only calls it for queries the policy already allowed.

This file is deliberately separate so it can be deleted in one step after the post-redesign benchmark run
(phase4_redesign_plan.md section 1). The removal steps are listed next to its single call site in `judge`.
"""

import re
from typing import Any, Dict

from app.utils.llm_factory import get_llm
from app.utils.logger import get_logger
from app.settings import settings

logger = get_logger("sentinel.governance.llm_grading")

PROMPT = """
You are an expert SQL validator.
Rate how well the following SQL query fulfills the user's natural language request.
Output ONLY a single integer between 0 and 100 representing the confidence score.

User Request: {question}
SQL Query: {sql}
"""


def llm_grading_enabled() -> bool:
    return settings.pipeline.validation_mode == "llm"


def llm_grade(sql: str, question: str) -> Dict[str, Any]:
    """The Verdict fields the model's grade overrides: outcome, score, risk_level, reason, breakdown."""
    try:
        raw = get_llm(role="validation").invoke(PROMPT.format(question=question, sql=sql)).strip()
        match = re.search(r"\d+", raw)
        score = int(match.group()) if match else 0
    except Exception as e:
        logger.warning("[LLM grading] evaluation failed: %s", e)
        score = 0

    if score > 90:
        outcome, risk, reason = "allowed", "LOW", f"High confidence ({score}%)"
    elif score >= 50:
        outcome, risk, reason = "held", "MEDIUM", f"Medium confidence ({score}%), requires review"
    else:
        outcome, risk, reason = "blocked", "HIGH", f"Low confidence ({score}%)"
    logger.info("[LLM grading] %s%% -> %s", score, outcome)
    return {"outcome": outcome, "score": score, "risk_level": risk, "reason": reason,
            "breakdown": [f"LLM grading (VALIDATION_MODE=llm): {score}%"]}
