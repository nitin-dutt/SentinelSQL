from langchain_ollama import OllamaLLM
from pydantic import BaseModel
from typing import List, Optional
from app.config import INTENT_MODEL
from pydantic import BaseModel, Field
from typing import List

DANGEROUS_INTENTS = ["delete", "drop", "update", "insert", "alter", "truncate", "create", "exec", "execute"]

class QueryIntent(BaseModel):
    tables: List[str] = Field(default_factory=list)
    metrics: List[str] = Field(default_factory=list)
    group_by: List[str] = Field(default_factory=list)
    filters: List[str] = Field(default_factory=list)
    limit: Optional[int] = 10

class IntentAnalysisAgent:
    def __init__(self):
        self.llm = OllamaLLM(model=INTENT_MODEL)

    def analyze(self, user_query: str, schema_context: str) -> QueryIntent:
        lowered = user_query.lower()
        for word in DANGEROUS_INTENTS:
            if word in lowered:
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
  "limit": number
}}

Rules:
- Use only tables and columns from the schema
- metrics should be SQL-like (e.g., SUM(orders.amount))
- group_by should be fully qualified column names
- If no limit is specified, use 10
- Do NOT include explanations

Schema:
{schema_context}

User question:
{user_query}

JSON:
"""

        response = self.llm.invoke(prompt)

        return QueryIntent.model_validate_json(response)
