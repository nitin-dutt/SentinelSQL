import re
from typing import Dict


class SQLValidationAgent:
    FORBIDDEN_KEYWORDS = [
        "drop", "delete", "update", "insert",
        "alter", "truncate"
    ]

    def validate(self, sql: str) -> Dict:
        sql_lower = sql.lower()

        for keyword in self.FORBIDDEN_KEYWORDS:
            if keyword in sql_lower:
                return {
                    "safe": False,
                    "reason": f"Forbidden keyword detected: {keyword}",
                    "requires_human": True
                }

        if "select *" in sql_lower:
            return {
                "safe": False,
                "reason": "SELECT * is not allowed",
                "requires_human": True
            }

        if "limit" not in sql_lower:
            return {
                "safe": False,
                "reason": "Missing LIMIT clause",
                "requires_human": True
            }

        join_count = len(re.findall(r"\bjoin\b", sql_lower))
        if join_count > 3:
            return {
                "safe": False,
                "reason": "Too many JOINs",
                "requires_human": True
            }

        return {
            "safe": True,
            "reason": "Query passed all validation checks",
            "requires_human": False
        }
