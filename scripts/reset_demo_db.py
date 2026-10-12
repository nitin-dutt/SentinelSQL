"""
Repair the demo database (alignment_plan DAT-01) and clear derived caches.

What it does
  1. Rebuilds `orders` and `salaries` with the UNIQUE constraints that schema.sql
     declares. `CREATE TABLE IF NOT EXISTS` never upgraded the old tables, so every
     server reload re-inserted the seed rows. Duplicates are collapsed, keeping the
     lowest primary key per unique key.
  2. Leaves every other table (data_dictionary, golden_queries, ...) untouched.
  3. Deletes the on-disk semantic cache, which may hold results computed from the
     duplicated data.

Usage:  python scripts/reset_demo_db.py [--db sentinel.db] [--keep-cache]
"""

import argparse
import os
import sqlite3
import sys

# table -> (CREATE statement with constraints, column list, unique key columns)
REBUILDS = {
    "orders": (
        """CREATE TABLE orders_new (
            order_id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER,
            amount NUMERIC(10, 2),
            order_date DATE,
            FOREIGN KEY (customer_id) REFERENCES customers(customer_id),
            UNIQUE(customer_id, amount, order_date)
        )""",
        ["order_id", "customer_id", "amount", "order_date"],
        ["customer_id", "amount", "order_date"],
    ),
    "salaries": (
        """CREATE TABLE salaries_new (
            employee_id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_name VARCHAR(255) NOT NULL,
            salary NUMERIC(12, 2) NOT NULL,
            department VARCHAR(100),
            UNIQUE(employee_name, department)
        )""",
        ["employee_id", "employee_name", "salary", "department"],
        ["employee_name", "department"],
    ),
}

CACHE_FILES = [".semantic_cache.json"]


def rebuild_table(conn: sqlite3.Connection, table: str) -> tuple:
    create_sql, cols, key = REBUILDS[table]
    before = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    col_list = ", ".join(cols)
    key_list = ", ".join(key)
    conn.execute(f"DROP TABLE IF EXISTS {table}_new")
    conn.execute(create_sql)
    conn.execute(
        f"INSERT OR IGNORE INTO {table}_new ({col_list}) "
        f"SELECT {col_list} FROM {table} "
        f"WHERE {cols[0]} IN (SELECT MIN({cols[0]}) FROM {table} GROUP BY {key_list}) "
        f"ORDER BY {cols[0]}"
    )
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
    after = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return before, after


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--db", default="sentinel.db")
    parser.add_argument("--keep-cache", action="store_true")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"Database not found: {args.db}")
        return 1

    conn = sqlite3.connect(args.db)
    try:
        existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        with conn:
            for table in REBUILDS:
                if table not in existing:
                    print(f"  {table}: not present, skipped")
                    continue
                before, after = rebuild_table(conn, table)
                print(f"  {table}: {before} -> {after} rows")
    finally:
        conn.close()

    if not args.keep_cache:
        for path in CACHE_FILES:
            if os.path.exists(path):
                os.remove(path)
                print(f"  removed {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
