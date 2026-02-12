from langchain_ollama import OllamaLLM
from app.config import SQL_MODEL
from app.agents.intent_agent import QueryIntent


import re

class SQLGenerationAgent:
    def __init__(self):
        self.llm = OllamaLLM(model=SQL_MODEL)

    def generate_sql(self, intent: QueryIntent, schema_context: str) -> str:
        prompt = f"""
You are an expert SQL generator.

Generate a valid SQLite SELECT query using ONLY the intent below.
Do not invent tables or columns.
Do not use DELETE, UPDATE, INSERT, DROP.
Always include LIMIT {intent.limit}.

Intent:
Tables: {intent.tables}
Metrics: {intent.metrics}
Group By: {intent.group_by}
Filters: {intent.filters}

Schema:
{schema_context}

Rules:
- Use explicit JOINs
- Fully qualify column names
- Output ONLY SQL
"""

        raw = self.llm.invoke(prompt)
        return self._extract_sql(raw)

    def _extract_sql(self, text: str) -> str:
        """
        Extract the first SELECT statement from LLM output.
        This handles markdown, prefixes, and token corruption.
        """
        # Try to extract from markdown code block first
        code_block = re.search(r"```(?:sql)?\s*(.*?)\s*```", text, flags=re.S | re.IGNORECASE)
        if code_block:
            text = code_block.group(1).strip()

        # Find first SELECT ... ; block
        match = re.search(
            r"(select\s+.*?;)",
            text,
            flags=re.IGNORECASE | re.DOTALL
        )

        if not match:
            print(f"Raw LLM output: {text}")
            raise ValueError("No valid SELECT statement found in LLM output")

        return match.group(1).strip()


