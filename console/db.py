"""db.py — sqlite access layer for the edge database (plan §3.7, §4).

Rules this module enforces:

* connection per request (or per short-lived unit of work);
* every connection re-asserts **WAL** and ``busy_timeout=2000``;
* the database file is opened ``mode=rw`` — the console never creates,
  replaces or deletes it;
* reads never take the write lock; writes (later lanes) serialise on the
  module-level :data:`write_lock` and use a short ``BEGIN IMMEDIATE``
  transaction (:func:`write_transaction`).

The two row readers are deliberately generic: the table name is checked as a
plain SQL identifier and the key/order column comes from ``PRAGMA
table_info`` (the integer ``id`` primary key on the standard tables, the
``tprefix`` primary key on the two packed mtree tables).  No table-specific
knowledge lives here.
"""
from __future__ import annotations

import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from urllib.parse import quote

from . import settings as settings_module

__all__ = [
    "BUSY_TIMEOUT_MS",
    "connect",
    "get_db",
    "get_conn",
    "get_read_db",
    "key_column",
    "read_row",
    "read_rows",
    "session",
    "write_lock",
    "write_transaction",
]

BUSY_TIMEOUT_MS = 2000

#: Serialises write transactions across the threadpool.  Reads never touch it.
write_lock = threading.Lock()

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# ---------------------------------------------------------------------------
# connections
# ---------------------------------------------------------------------------
def connect(path: str | Path) -> sqlite3.Connection:
    """Open *path* for read/write with WAL + a 2 s busy timeout.

    ``mode=rw`` is intentional: a missing file raises ``OperationalError``
    instead of silently creating a new (empty) edge database.  Autocommit
    mode keeps transaction boundaries explicit for the write lanes.
    """
    db_path = Path(path).expanduser().resolve()
    uri = f"file:{quote(db_path.as_posix(), safe='/')}?mode=rw"
    conn = sqlite3.connect(
        uri,
        uri=True,
        timeout=BUSY_TIMEOUT_MS / 1000.0,
        isolation_level=None,  # explicit BEGIN IMMEDIATE in write_transaction
        check_same_thread=False,  # one connection per request, never shared
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    # Re-asserted on every connection because create-db.sh rebuilds can revert
    # the file header to DELETE mode (plan §3.7).
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def session(path: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    """Short-lived connection context (tests and non-request callers)."""
    conn = connect(path or settings_module.settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


def get_db() -> Iterator[sqlite3.Connection]:
    """FastAPI dependency: one read/write connection for this request.

    The database must exist; write lanes use this so a missing file is a loud
    failure rather than silently empty state.
    """
    conn = connect(settings_module.settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


#: Alias used by some callers/lanes; same generator as :func:`get_db`.
get_conn = get_db


def get_read_db() -> Iterator[sqlite3.Connection | None]:
    """FastAPI dependency for read-only pages.

    Yields ``None`` when the edge database is absent or cannot be opened, so a
    freshly installed console shows an empty/first-use page instead of a 500.
    """
    path = Path(settings_module.settings.db_path)
    if not path.is_file():
        yield None
        return
    try:
        conn = connect(path)
    except (sqlite3.Error, OSError):
        yield None
        return
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# generic row access
# ---------------------------------------------------------------------------
def _checked_table(table: str) -> str:
    if not isinstance(table, str) or not _IDENTIFIER.fullmatch(table):
        raise ValueError(f"unsafe table name: {table!r}")
    return table


def _table_info(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    if not info:
        raise sqlite3.OperationalError(f"no such table: {table}")
    return info


def key_column(conn: sqlite3.Connection, table: str) -> str:
    """Return the primary-key column of *table* (first PK, else first column).

    ``dispatcher``/``dialplan``/``trusted`` expose the auto ``id`` PK; the
    packed mtree tables expose ``tprefix``.  The caller supplies the value.
    """
    table = _checked_table(table)
    info = _table_info(conn, table)
    primary = sorted((row for row in info if row[5]), key=lambda row: row[5])
    return str(primary[0][1]) if primary else str(info[0][1])


def read_rows(conn: sqlite3.Connection, table: str) -> list[dict]:
    """Return every row of *table* as a ``dict``, ordered by its key column."""
    table = _checked_table(table)
    column = key_column(conn, table)
    cursor = conn.execute(f'SELECT * FROM "{table}" ORDER BY "{column}"')
    names = [description[0] for description in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def read_row(conn: sqlite3.Connection, table: str, key: object) -> dict | None:
    """Return one row by primary key, or ``None`` when it does not exist."""
    table = _checked_table(table)
    column = key_column(conn, table)
    info = _table_info(conn, table)
    column_type = next(
        (str(row[2]) for row in info if str(row[1]) == column), ""
    ).upper()
    if "INT" in column_type:
        try:
            key = int(key)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
    cursor = conn.execute(
        f'SELECT * FROM "{table}" WHERE "{column}" = ?', (key,)
    )
    row = cursor.fetchone()
    if row is None:
        return None
    names = [description[0] for description in cursor.description]
    return dict(zip(names, row))


# ---------------------------------------------------------------------------
# writes (later lanes)
# ---------------------------------------------------------------------------
@contextmanager
def write_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Serialise a short ``BEGIN IMMEDIATE`` transaction (writes only).

    Reads must never acquire :data:`write_lock`; the lock exists to keep a
    single console writer in front of Kamailio's sqlite (plan §4).
    """
    with write_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
