import logging
import os
import re
from typing import Dict
from app.utils.llm_factory import get_llm
from app.utils.sql_parser import parse_and_validate_ast
from app.utils.logger import get_logger

logger = get_logger("sentinel.validator")


class SQLValidationAgent:
    def __init__(self):
        # Validation mode: "ast" (deterministic, default) or "llm" (experimental LLM grading)
        self.mode = os.getenv("VALIDATION_MODE", "ast").strip().lower()
        if self.mode == "llm":
            self.llm = get_llm(role="validation")
        else:
            self.llm = None


    def validate(self, sql: str, user_query: str) -> Dict:
        # One policy for every ingress (web, MCP, HITL edits): the AST engine in
        # app/utils/sql_parser.py. There are deliberately no substring checks here: they
        # blocked legitimate columns such as `last_updated` and `is_deleted` (GRD-02) and
        # let the web and MCP paths drift apart (GRD-04).
        # 1. Deterministic AST Security Analysis, Structural Parsing & Risk Tier Engine
        ast_res = parse_and_validate_ast(sql)

        action_label = (
            "BLOCKED" if not ast_res.is_valid
            else "REQUIRES_HUMAN_APPROVAL" if ast_res.requires_human
            else "AUTO_EXECUTE"
        )

        logger.info(f"[AST Confidence Engine] Confidence: {ast_res.confidence_score}% | Risk: {ast_res.risk_level} | Action: {action_label}")
        logger.info(f"[AST Confidence Engine] Tables: {ast_res.tables_accessed} | Joins: {ast_res.join_count} | Has Limit: {ast_res.has_limit}")
        for rule in ast_res.score_breakdown:
            logger.info(f"Rule: {rule}")

        # If AST identified hard security violation or block
        if not ast_res.is_valid:
            logger.warning(f"[AST Confidence Engine] Blocked: {ast_res.reason}")
            return {
                "safe": False,
                "reason": ast_res.reason,
                "requires_human": False,
                "risk_level": ast_res.risk_level,
                "score": ast_res.confidence_score,
                "sanitized_sql": ast_res.sanitized_sql,
                "tables": ast_res.tables_accessed,
                "breakdown": ast_res.score_breakdown
            }

        # If AST identified human review required (e.g. sensitive tables or high join count)
        if ast_res.requires_human:
            logger.info(f"[AST Confidence Engine] Human Review Required: {ast_res.reason}")
            return {
                "safe": True,
                "reason": ast_res.reason,
                "requires_human": True,
                "risk_level": ast_res.risk_level,
                "score": ast_res.confidence_score,
                "sanitized_sql": ast_res.sanitized_sql,
                "tables": ast_res.tables_accessed,
                "breakdown": ast_res.score_breakdown
            }

        # 2. Optional: Experimental LLM Confidence Scoring (Only if VALIDATION_MODE="llm" is explicitly enabled)
        if self.mode == "llm" and self.llm is not None:
            prompt = f"""
You are an expert SQL validator.
Rate how well the following SQL query fulfills the user's natural language request.
Output ONLY a single integer between 0 and 100 representing the confidence score.

User Request: {user_query}
SQL Query: {sql}
"""
            try:
                raw_score = self.llm.invoke(prompt).strip()
                match = re.search(r'\d+', raw_score)
                score = int(match.group()) if match else 0
            except Exception as e:
                logger.warning(f"[Validation LLM] Evaluation failed: {e}")
                score = 0

            logger.info(f"[Validation Agent] LLM Confidence Score: {score}% ({'Auto-executed' if score > 90 else 'Requires review' if score >= 50 else 'Rejected'})")

            if score > 90:
                return {
                    "safe": True,
                    "reason": f"High confidence ({score}%)",
                    "requires_human": False,
                    "risk_level": "LOW",
                    "score": score,
                    "sanitized_sql": ast_res.sanitized_sql,
                    "tables": ast_res.tables_accessed
                }
            elif score >= 50:
                return {
                    "safe": True,
                    "reason": f"Medium confidence ({score}%), requires review",
                    "requires_human": True,
                    "risk_level": "MEDIUM",
                    "score": score,
                    "sanitized_sql": ast_res.sanitized_sql,
                    "tables": ast_res.tables_accessed
                }
            else:
                return {
                    "safe": False,
                    "reason": f"Low confidence ({score}%)",
                    "requires_human": False,
                    "risk_level": "HIGH",
                    "score": score,
                    "sanitized_sql": ast_res.sanitized_sql,
                    "tables": ast_res.tables_accessed
                }

        # 3. Deterministic AST Confidence (Default & Recommended)
        logger.info(f"[AST Confidence Engine] Query validated successfully with {ast_res.confidence_score}% confidence")
        return {
            "safe": True,
            "reason": ast_res.reason,
            "requires_human": False,
            "risk_level": ast_res.risk_level,
            "score": ast_res.confidence_score,
            "sanitized_sql": ast_res.sanitized_sql,
            "tables": ast_res.tables_accessed,
            "breakdown": ast_res.score_breakdown
        }
