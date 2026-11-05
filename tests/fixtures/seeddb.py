"""Build disposable sqlite databases for the read-only browser tests (T3).

The schema is the *exact* Kamailio edge schema for the five tables the console
describes in ``console/tables.yaml`` plus the ``version`` bookkeeping table
(``kamailio-edge/kamailio/db/seed.sql``).  Only tests use this module; the
console itself never creates or replaces a database file.

Usage::

    path = seeddb.seed(tmp_path / "edge.sqlite")        # realistic rows
    path = seeddb.seed(tmp_path / "empty.sqlite", {})   # schema only

Each row mapping is ``{table: [{column: value, ...}, ...]}`` so the helper
stays independent of the descriptor field names.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

__all__ = ["SCHEMA", "REALISTIC_ROWS", "seed", "empty"]

# ---------------------------------------------------------------------------
# schema — copied from the deployment's seed.sql (no console-specific columns)
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS tenant_registry (
    tprefix CHAR PRIMARY KEY,
    tvalue  CHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS tenant_landline (
    tprefix CHAR PRIMARY KEY,
    tvalue  CHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS dispatcher (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    setid       INTEGER NOT NULL DEFAULT 0,
    destination CHAR NOT NULL DEFAULT '',
    flags       INTEGER NOT NULL DEFAULT 0,
    priority    INTEGER NOT NULL DEFAULT 0,
    attrs       CHAR NOT NULL DEFAULT '',
    description CHAR NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS dialplan (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    dpid      INTEGER NOT NULL DEFAULT 0,
    pr        INTEGER NOT NULL DEFAULT 1,
    match_op  INTEGER NOT NULL DEFAULT 1,
    match_exp TEXT NOT NULL DEFAULT '',
    match_len INTEGER NOT NULL DEFAULT 0,
    subst_exp TEXT NOT NULL DEFAULT '^(.*)$',
    repl_exp  TEXT NOT NULL DEFAULT '\\1',
    attrs     TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS trusted (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    src_ip       CHAR NOT NULL,
    proto        CHAR NOT NULL DEFAULT 'any',
    from_pattern CHAR,
    ruri_pattern CHAR,
    tag          CHAR,
    priority     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS version (
    table_name    TEXT PRIMARY KEY,
    table_version INTEGER NOT NULL
);
"""

TABLE_NAMES = (
    "tenant_registry",
    "tenant_landline",
    "dispatcher",
    "dialplan",
    "trusted",
)

VERSION_ROWS = (("dispatcher", 4), ("dialplan", 2), ("trusted", 6))

# ---------------------------------------------------------------------------
# realistic fixture data: the three homes from the deployment seed
# ---------------------------------------------------------------------------
REALISTIC_ROWS: dict[str, list[dict[str, object]]] = {
    "tenant_registry": [
        {
            "tprefix": "sip.alpha.home.example.net",
            "tvalue": "T1,11,31,01111111,01111111,10,1+1+90",
        },
        {
            "tprefix": "sip.beta.home.example.net",
            "tvalue": "T2,12,32,02222222,02222222,10,1+1+90",
        },
        {
            "tprefix": "sip.gamma.home.example.net",
            "tvalue": "T3,13,33,03333333,03333333,10,1+1+90",
        },
    ],
    "tenant_landline": [
        {"tprefix": "01111111", "tvalue": "T1,home,11,1"},
        {"tprefix": "02222222", "tvalue": "T2,home,12,1"},
        {"tprefix": "03333333", "tvalue": "T3,home,13,1"},
    ],
    "dispatcher": [
        {
            "setid": 11,
            "destination": "sip:10.0.0.10:5060;transport=tcp",
            "flags": 0,
            "priority": 0,
            "attrs": "",
            "description": "alpha voip-stack (HA host LAN IP, native routing)",
        },
        {
            "setid": 31,
            "destination": "sip:10.0.0.10:5060;transport=tcp",
            "flags": 0,
            "priority": 0,
            "attrs": "",
            "description": "alpha PSTN via voip-stack (HT813 trunk, LAN-only)",
        },
        {
            "setid": 12,
            "destination": "sip:10.0.99.12:5060;transport=tcp",
            "flags": 0,
            "priority": 0,
            "attrs": "",
            "description": "beta voip-stack (PLACEHOLDER - set its HA LAN IP)",
        },
        {
            "setid": 32,
            "destination": "sip:10.0.99.12:5060;transport=tcp",
            "flags": 0,
            "priority": 0,
            "attrs": "",
            "description": "beta PSTN via voip-stack (PLACEHOLDER)",
        },
        {
            "setid": 13,
            "destination": "sip:10.0.99.13:5060;transport=tcp",
            "flags": 0,
            "priority": 0,
            "attrs": "",
            "description": "gamma voip-stack (PLACEHOLDER - set its HA LAN IP)",
        },
        {
            "setid": 33,
            "destination": "sip:10.0.99.13:5060;transport=tcp",
            "flags": 0,
            "priority": 0,
            "attrs": "",
            "description": "gamma PSTN via voip-stack (PLACEHOLDER)",
        },
    ],
    "dialplan": [
        {
            "dpid": 10,
            "pr": 1,
            "match_op": 1,
            "match_exp": "^(112|911|999)$",
            "match_len": 0,
            "subst_exp": "^(.*)$",
            "repl_exp": "\\1",
            "attrs": "emergency Example (ISF/CivDef/Fire/RedCross)",
        },
        {
            "dpid": 90,
            "pr": 1,
            "match_op": 1,
            "match_exp": "^0900[0-9]{6}$",
            "match_len": 0,
            "subst_exp": "^(.*)$",
            "repl_exp": "\\1",
            "attrs": "premium Example 0900 audiotex - blocked",
        },
    ],
    "trusted": [
        {
            "src_ip": "100.64.10.10",
            "proto": "any",
            "from_pattern": None,
            "ruri_pattern": None,
            "tag": "sip.alpha.home.example.net",
            "priority": 0,
        },
        {
            "src_ip": "100.64.10.12",
            "proto": "any",
            "from_pattern": None,
            "ruri_pattern": None,
            "tag": "sip.beta.home.example.net",
            "priority": 0,
        },
        {
            "src_ip": "100.64.10.14",
            "proto": "any",
            "from_pattern": None,
            "ruri_pattern": None,
            "tag": "sip.gamma.home.example.net",
            "priority": 0,
        },
    ],
}


def _insert_rows(conn: sqlite3.Connection, rows: dict[str, list[dict]]) -> None:
    for table, table_rows in rows.items():
        if table not in TABLE_NAMES:
            raise KeyError(f"unknown fixture table {table!r}")
        for row in table_rows:
            columns = list(row)
            if not columns:
                continue
            column_sql = ", ".join(f'"{column}"' for column in columns)
            mark_sql = ", ".join("?" for _ in columns)
            conn.execute(
                f'INSERT INTO "{table}" ({column_sql}) VALUES ({mark_sql})',
                [row[column] for column in columns],
            )


def seed(path: str | Path, rows: dict[str, list[dict]] | None = None) -> Path:
    """Create a fresh WAL database at *path* and insert *rows*.

    ``rows=None`` uses :data:`REALISTIC_ROWS`; pass ``{}`` for a schema-only
    (first-use) database.  The file is checkpointed and closed so callers get
    a self-contained database with no ``-wal`` sidecar.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    for candidate in (target, Path(f"{target}-wal"), Path(f"{target}-shm")):
        candidate.unlink(missing_ok=True)

    conn = sqlite3.connect(target)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT INTO version (table_name, table_version) VALUES (?, ?)",
            VERSION_ROWS,
        )
        _insert_rows(conn, REALISTIC_ROWS if rows is None else rows)
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    return target


def empty(path: str | Path) -> Path:
    """Convenience: schema + version rows, no tenant data."""
    return seed(path, {})
