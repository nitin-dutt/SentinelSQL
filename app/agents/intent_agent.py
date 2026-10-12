import logging
import re
from typing import List, Optional
import json
from pydantic import BaseModel, Field, ValidationError, field_validator

from app.utils.llm_factory import get_llm

logger = logging.getLogger("sentinel.intent")

# Early-exit UX filter, NOT the security boundary (that is the AST engine in app/utils/sql_parser.py,
# which every generated query must pass). It only refuses requests that are plainly a command to
# change data or schema, so the user gets an immediate explanation instead of a failed generation.
# It must not trip on ordinary analytics wording such as "customers created this year",
# "last updated products" or "dropped from the catalog" (alignment_plan GRD-01).
_VERBS = r"(delete|drop|truncate|wipe|erase|destroy|purge|insert|update|alter)"

# 1. SQL-shaped destructive statements anywhere in the text
_SQL_PATTERNS = [
    re.compile(r"\b(drop|truncate|alter)\s+(?:table|database|schema|index|view|column|user|role)\b", re.I),
    re.compile(r"\b(delete)\s+from\b", re.I),
    re.compile(r"\b(insert)\s+into\b", re.I),
    re.compile(r"\b(update)\s+[\w.\"`\[\]]+\s+set\b", re.I),
    re.compile(r"\b(create)\s+(?:or\s+replace\s+)?(?:table|database|schema|index|view|user|role|trigger|function|procedure)\b", re.I),
    re.compile(r"\b(exec(?:ute)?)\s*(?:\(|xp_|sp_|immediate\b)", re.I),
    re.compile(r";\s*(drop|delete|truncate|update|insert|alter|create|exec(?:ute)?)\b", re.I),
]

# 2. A destructive verb used as a command: at the start of a sentence, or after a polite/lead-in phrase
_IMPERATIVE = re.compile(
    r"(?:^|[.;!?:]\s*|\b(?:and|then)\s+)"
    r"(?:(?:please|kindly|now|just|also|go\s+ahead\s+and)\s+)*"
    r"(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?|i\s+(?:want|need|would\s+like|'d\s+like)\s+(?:you\s+)?to\s+|help\s+me\s+|let'?s\s+)?"
    + _VERBS + r"\b",
    re.I,
)


def detect_destructive_intent(text: str) -> Optional[str]:
    """Returns the offending verb if the text is a command to modify data or schema, else None."""
    for pattern in _SQL_PATTERNS:
        m = pattern.search(text)
        if m:
            return m.group(1).lower()
    m = _IMPERATIVE.search(text.strip())
    return m.group(1).lower() if m else None


def _as_text(item) -> str:
    """Models often return structured filters ({"column":..,"operator":..,"value":..}); flatten them to text."""
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        if {"column", "operator"} <= set(item):
            value = item.get("value")
            value = f"'{value}'" if isinstance(value, str) else value
            return f"{item['column']} {item['operator']} {value}".strip()
        return json.dumps(item, ensure_ascii=False)
    return str(item)


class QueryIntent(BaseModel):
    tables: List[str] = Field(default_factory=list)
    metrics: List[str] = Field(default_factory=list)
    group_by: List[str] = Field(default_factory=list)
    filters: List[str] = Field(default_factory=list)
    limit: Optional[int] = None      # only set when the user asked for a specific number of rows

    @field_validator("tables", "metrics", "group_by", "filters", mode="before")
    @classmethod
    def _flatten(cls, v):
        if v is None:
            return []
        if not isinstance(v, list):
            v = [v]
        return [_as_text(x) for x in v]

    @field_validator("limit", mode="before")
    @classmethod
    def _limit(cls, v):
        if v in (None, "", "null", "none", "None"):
            return None
        try:
            n = int(v)
        except (TypeError, ValueError):
            return None
        return n if n > 0 else None


class IntentAnalysisAgent:
    def __init__(self):
        self.llm = get_llm(role="intent")

    def analyze(self, user_query: str, schema_context: str) -> QueryIntent:
        word = detect_destructive_intent(user_query)
        if word:
            logger.warning("Destructive intent detected: '%s' -> rejecting query", word)
            raise ValueError(f"Destructive intent detected: '{word}'")
        prompt = f"""
You are an AI system that extracts structured query intent.

Given a user question and database schema, output ONLY valid JSON
matching this schema:

{{
  "tables": [],
  "metrics": [],
  "group_by": [],
  "filters": [],
  "limit": number or null
}}

Rules:
- Use only tables and columns from the schema
- metrics should be SQL-like strings (e.g., "SUM(orders.amount)")
- group_by should be fully qualified column names
- filters must be plain strings such as "customers.region = 'North'", never objects
- limit is null unless the question asks for a specific number of rows (e.g. "top 5")
- Do NOT include explanations

Schema:
{schema_context}

User question:
{user_query}

JSON:
"""

        response = self.llm.invoke(prompt)

        # Defensive cleanup: strip markdown code blocks if returned by LLM
        clean_json = response.strip()
        clean_json = re.sub(r"^```(?:json)?\s*", "", clean_json, flags=re.IGNORECASE)
        clean_json = re.sub(r"\s*```$", "", clean_json)

        # Extract the outermost JSON block if any preamble/trailer exists
        json_match = re.search(r"(\{.*\})", clean_json, flags=re.DOTALL)
        if json_match:
            clean_json = json_match.group(1).strip()

        try:
            return QueryIntent.model_validate_json(clean_json.strip())
        except ValidationError as e:
            # The model returned something that is not the requested JSON shape. Treat it as a
            # failed request, not a server error.
            logger.warning("Intent JSON did not validate: %s", str(e)[:200])
            raise ValueError("Could not understand the model's interpretation of the question; please rephrase it") from e

