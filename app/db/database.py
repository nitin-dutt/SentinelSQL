import sqlite3
from typing import List, Dict
import re


DB_PATH = "sentinel.db"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    with open("app/db/schema.sql", "r") as f:
        conn.executescript(f.read())
    conn.commit()
    conn.close()


def execute_readonly_query(query: str) -> List[Dict]:
    if not re.match(r"^\s*select\b", query, re.IGNORECASE):
        raise ValueError("Only SELECT queries are allowed")


    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute(query)
    rows = cursor.fetchall()

    conn.close()

    return [dict(row) for row in rows]
