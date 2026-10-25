import os
import re

import sqlglot
from sqlglot import exp
from sqlglot.tokens import TokenType

from app.db.urls import sql_dialect, sql_dialect_label
from app.utils.sql_parser import QUERY_ROOTS
from app.agents.intent_agent import QueryIntent
from app.utils.llm_factory import get_llm
from app.utils.logger import get_logger

logger = get_logger("sentinel.sql_generator")


def _few_shot_count() -> int:
    """How many verified golden examples are retrieved into the prompt (FEWSHOT_TOP_K, default 2; 0 turns them off)."""
    try:
        return max(0, int(os.getenv("FEWSHOT_TOP_K", "2")))
    except ValueError:
        return 2


class SQLGenerationAgent:
    def __init__(self):
        self.llm = get_llm(role="sql")


    def generate_sql(self, intent: QueryIntent, schema_context: str, user_query: str = "") -> str:
        few_shot_block = ""
        dictionary_block = ""
        query_text = user_query or " ".join(intent.tables + intent.metrics + intent.filters)
        if query_text:
            try:
                from app.rag.golden_store import format_few_shot_examples
                few_shots = format_few_shot_examples(query_text, top_k=_few_shot_count())
                if few_shots:
                    few_shot_block = f"\n{few_shots}\n"
            except Exception:
                pass

            try:
                from app.rag.data_dictionary import format_dictionary_context
                dict_context = format_dictionary_context(query_text, top_k=3)
                if dict_context:
                    dictionary_block = f"\n{dict_context}\n"
            except Exception:
                pass

        limit_rule = f"Include LIMIT {intent.limit}." if intent.limit else "Do not add a LIMIT unless the question asks for a specific number of rows."
        prompt = f"""
You are an expert SQL generator.

Write one valid {sql_dialect_label()} SELECT query that answers the question below, using {sql_dialect_label()} syntax and functions. The structured intent is a hint
extracted from the question; if it disagrees with the question, follow the question.
Do not invent tables or columns.
Do not use DELETE, UPDATE, INSERT, DROP.
{limit_rule}

Rules for generation:
1. SELECT only the columns the question asks for, using explicit column names. Never use SELECT *, and do not add extra columns. Inside an EXISTS subquery write SELECT 1.
2. Match the question literally. Use DISTINCT when it asks for the different/distinct/unique values or which values exist (for example "which years"); do not add DISTINCT just because a join could repeat rows.
3. Use JOINs to bring in columns from other tables. To test that related rows exist or do not exist ("who have friends", "that do not have a cat", "with no orders", "without"), use IN / NOT IN / EXISTS / NOT EXISTS instead of a join, so rows are not duplicated or wrongly matched. Example: "students who do not have a cat" must exclude every student who owns any cat, so use NOT IN (subquery); a join with <> 'cat' keeps students who own a dog and a cat.
4. Fully qualify column names when more than one table is involved. If "Filters" is empty, omit the WHERE clause.
5. If an applicable formula or rule is provided in the Data Dictionary, use its exact SQL expression.
6. Output ONLY the raw SQL query. Do not add any conversational text or apologies.
{dictionary_block}
{few_shot_block}
Question:
{user_query}

Intent (hint):
Tables: {intent.tables}
Metrics: {intent.metrics}
Group By: {intent.group_by}
Filters: {intent.filters}

Schema:
{schema_context}
"""

        raw = self.llm.invoke(prompt)
        return self._extract_sql(raw)

    def repair_sql(self, user_query: str, schema_context: str, failed_sql: str, error: str) -> str:
        """
        One repair attempt after the database rejected a query (unknown column or table, bad syntax, ambiguous name).
        The corrected SQL is only a candidate: the caller must run it through the policy validator again.
        """
        label = sql_dialect_label()
        prompt = f"""
You are an expert SQL generator.

The {label} query below was written to answer the question, but the database rejected it. Fix it so that it answers the question.
Use ONLY tables and columns that appear in the schema; if the error says a column or table does not exist, choose the correct name from the schema.
Keep the rest of the query's logic. The same rules apply: one SELECT query, only the columns the question asks for, never SELECT *,
no DELETE, UPDATE, INSERT or DROP. Output ONLY the corrected SQL query.

Question:
{user_query}

Failed query:
{failed_sql}

Database error:
{(error or "")[:500]}

Schema:
{schema_context}
"""
        return self._extract_sql(self.llm.invoke(prompt))

    def _extract_sql(self, text: str) -> str:
        """
        Pulls the SELECT statement out of an LLM reply (alignment_plan RUN-02).

        Handles markdown fences and conversational preambles, and cuts at the first *top-level*
        semicolon using the SQL tokenizer, so a `;` inside a string literal (`WHERE note = 'a;b'`)
        no longer truncates the query. The result must parse as a SELECT/UNION/CTE query.
        """
        fenced = re.search(r"```(?:sql)?\s*(.*?)\s*```", text, flags=re.S | re.IGNORECASE)
        if fenced:
            text = fenced.group(1)

        start = re.search(r"^\s*(select|with)\b", text, flags=re.IGNORECASE | re.MULTILINE)
        if start is None:
            # A CTE introduced mid-sentence ("Sure! WITH x AS (...) SELECT ..."), then a bare SELECT.
            start = re.search(r"\b(with)\s+(?:recursive\s+)?[\w\"`]+\s*(?:\([^)]*\))?\s*as\s*\(", text, flags=re.IGNORECASE)
        if start is None:
            start = re.search(r"\b(select)\b", text, flags=re.IGNORECASE)
        if start is None:
            logger.warning("No SELECT found in LLM output: %r", text[:200])
            raise ValueError("No valid SELECT statement found in LLM output")

        candidate = text[start.start(1):]
        try:
            dialect = sql_dialect()
            for token in sqlglot.tokenize(candidate, read=dialect):
                if token.token_type == TokenType.SEMICOLON:
                    candidate = candidate[:token.start]
                    break
            parsed = sqlglot.parse_one(candidate, read=dialect)
        except Exception as e:
            logger.warning("Could not parse generated SQL (%s): %r", type(e).__name__, candidate[:200])
            raise ValueError("The model produced SQL that could not be parsed") from e

        if not isinstance(parsed, QUERY_ROOTS):
            raise ValueError("No valid SELECT statement found in LLM output")
        return candidate.strip() + ";"
