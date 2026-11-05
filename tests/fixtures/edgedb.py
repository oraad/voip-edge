"""A disposable ``edge.sqlite`` for lifecycle unit tests (T8 only).

This fixture is intentionally independent of ``tests/fixtures/seeddb.py``
(T3 owns that one).  It creates the exact five-table schema the descriptors
in ``console/tables.yaml`` expect, the three Kamailio ``version`` rows, and
two seeded homes with their landlines, dispatcher sets, trusted sources and
shared dialplan partitions 10/90.

Everything is plain sqlite; tests may mutate it directly to construct restore
collisions.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

__all__ = [
    "DOMAIN_A",
    "DOMAIN_B",
    "TENANT_A",
    "TENANT_B",
    "LANDLINE_A",
    "LANDLINE_B",
    "SRC_IP_A",
    "SRC_IP_B",
    "SETID_A_ONNET",
    "SETID_A_PSTN",
    "SETID_B_ONNET",
    "SETID_B_PSTN",
    "SCHEMA",
    "connect",
    "make_edge_db",
    "seed",
]

DOMAIN_A = "beta.home.example.net"
DOMAIN_B = "b-example.home.example.net"
TENANT_A = "T1"
TENANT_B = "T2"
LANDLINE_A = "1001"
LANDLINE_B = "1002"
SRC_IP_A = "100.96.1.10"
SRC_IP_B = "100.96.1.11"
SETID_A_ONNET = 10
SETID_A_PSTN = 30
SETID_B_ONNET = 11
SETID_B_PSTN = 31
DPID_COUNTRY = 10
DPID_BLOCK = 90

SCHEMA = """
CREATE TABLE tenant_registry (
    tprefix TEXT PRIMARY KEY,
    tvalue  TEXT NOT NULL
);
CREATE TABLE tenant_landline (
    tprefix TEXT PRIMARY KEY,
    tvalue  TEXT NOT NULL
);
CREATE TABLE dispatcher (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    setid       INTEGER NOT NULL DEFAULT 0,
    destination TEXT    NOT NULL DEFAULT '',
    flags       INTEGER NOT NULL DEFAULT 0,
    priority    INTEGER NOT NULL DEFAULT 0,
    attrs       TEXT,
    description TEXT
);
CREATE TABLE dialplan (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    dpid      INTEGER NOT NULL DEFAULT 0,
    pr        INTEGER NOT NULL DEFAULT 0,
    match_op  INTEGER NOT NULL DEFAULT 0,
    match_exp TEXT    NOT NULL DEFAULT '',
    match_len INTEGER NOT NULL DEFAULT 0,
    subst_exp TEXT,
    repl_exp  TEXT,
    attrs     TEXT
);
CREATE TABLE trusted (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    src_ip        TEXT    NOT NULL DEFAULT '',
    proto         TEXT    NOT NULL DEFAULT 'any',
    from_pattern  TEXT    NOT NULL DEFAULT '',
    ruri_pattern  TEXT    NOT NULL DEFAULT '',
    tag           TEXT,
    priority      INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE version (
    table_name    TEXT PRIMARY KEY,
    table_version INTEGER NOT NULL
);
"""

_REGISTRY = (
    # tprefix, tvalue = tenantid,ds_onnet,ds_pstn,cli,label,dpid,flags
    (DOMAIN_A, f"{TENANT_A},10,30,{LANDLINE_A},A Example,10,1+1+90"),
    (DOMAIN_B, f"{TENANT_B},11,31,{LANDLINE_B},B Example,11,1+1+90"),
)
_LANDLINES = (
    (LANDLINE_A, f"{TENANT_A},501,10,1"),
    (LANDLINE_B, f"{TENANT_B},502,11,1"),
)
_DISPATCHER = (
    (SETID_A_ONNET, "sip:100.64.0.10:5061;transport=tcp", "A on-net"),
    (SETID_B_ONNET, "sip:100.64.0.11:5061;transport=tcp", "B on-net"),
    (SETID_A_PSTN, "sip:100.64.0.30:5061;transport=tcp", "A PSTN"),
    (SETID_B_PSTN, "sip:100.64.0.31:5061;transport=tcp", "B PSTN"),
)
_DIALPLAN = (
    (DPID_COUNTRY, "^112$", "country profile"),
    (DPID_BLOCK, "^1900", "block partition"),
)


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def seed(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT INTO tenant_registry (tprefix, tvalue) VALUES (?, ?)",
        _REGISTRY,
    )
    conn.executemany(
        "INSERT INTO tenant_landline (tprefix, tvalue) VALUES (?, ?)",
        _LANDLINES,
    )
    for setid, destination, description in _DISPATCHER:
        conn.execute(
            "INSERT INTO dispatcher "
            "(setid, destination, flags, priority, attrs, description) "
            "VALUES (?, ?, 0, 0, '', ?)",
            (setid, destination, description),
        )
    for dpid, match_exp, attrs in _DIALPLAN:
        conn.execute(
            "INSERT INTO dialplan "
            "(dpid, pr, match_op, match_exp, match_len, subst_exp, repl_exp, attrs) "
            "VALUES (?, 0, 1, ?, 0, '^(.*)$', '\\1', ?)",
            (dpid, match_exp, attrs),
        )
    conn.executemany(
        "INSERT INTO trusted "
        "(src_ip, proto, from_pattern, ruri_pattern, tag, priority) "
        "VALUES (?, 'any', '', '', ?, 0)",
        ((SRC_IP_A, DOMAIN_A), (SRC_IP_B, DOMAIN_B)),
    )
    conn.executemany(
        "INSERT INTO version (table_name, table_version) VALUES (?, ?)",
        (("dispatcher", 4), ("dialplan", 2), ("trusted", 6)),
    )


def make_edge_db(
    path: str | Path, *, seed_rows: bool = True, wal: bool = True
) -> Path:
    """Create *path* with the five-table schema and (optionally) seed rows."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(target)
    try:
        conn.executescript(SCHEMA)
        if seed_rows:
            seed(conn)
        if wal:
            conn.execute("PRAGMA journal_mode = WAL")
        conn.commit()
    finally:
        conn.close()
    return target
