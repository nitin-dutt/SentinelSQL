"""
The connect-and-create routine shared by SentinelSQL's own small SQLite files: the audit log (audit.db), the review
queue (hitl_runs.db), traces (traces.db) and chat conversations (conversations.db).

Each store stays its own file with its own tables and its own rules. The audit log in particular is kept apart from
data that is purged as a matter of routine. What they share is only this: where the file is (asked on every connect, so
the location can be redirected), how a connection is opened, creating the tables the first time a file is used, and
deleting rows older than a time limit.
"""

import sqlite3
import threading
import time
from typing import Callable, Optional, Set


class SqliteStore:
    """
    One internal SQLite file.

    `path` is called on every connect and returns the file to use. `create` is called once per file, with an open
    connection, to create or migrate its tables. `wal` switches the file to write-ahead logging. `isolation_level` is
    passed to sqlite3 (None means the caller writes BEGIN and COMMIT itself, as the hash-chained audit log does).
    `rows` makes connections return `sqlite3.Row`.
    """

    def __init__(self, path: Callable[[], str], create: Callable[[sqlite3.Connection], None], *, wal: bool = True,
                 isolation_level: Optional[str] = "", rows: bool = True, timeout: float = 10):
        self._path = path
        self._create = create
        self._wal = wal
        self._isolation_level = isolation_level
        self._rows = rows
        self._timeout = timeout
        self._lock = threading.Lock()
        self.initialized: Set[str] = set()        # files whose tables have been created in this process

    def path(self) -> str:
        return self._path()

    def connect(self) -> sqlite3.Connection:
        path = self._path()
        conn = sqlite3.connect(path, timeout=self._timeout, isolation_level=self._isolation_level)
        if self._rows:
            conn.row_factory = sqlite3.Row
        if path not in self.initialized:
            with self._lock:
                if self._wal:
                    conn.execute("PRAGMA journal_mode=WAL")
                self._create(conn)
                if self._isolation_level is not None:
                    conn.commit()
                self.initialized.add(path)
        return conn

    def forget(self) -> None:
        """Forget which files were set up, so the next connect creates the tables again (after a file was replaced)."""
        self.initialized.clear()


def delete_older_than(conn: sqlite3.Connection, table: str, column: str, seconds: float, where: str = "") -> None:
    """
    Deletes the rows of `table` whose `column` (a time.time() value) is more than `seconds` old. `where` narrows it
    with one more condition. `table`, `column` and `where` are written by the calling module, never by a user.
    """
    sql = f"DELETE FROM {table} WHERE {column} < ?" + (f" AND {where}" if where else "")
    conn.execute(sql, (time.time() - seconds,))
