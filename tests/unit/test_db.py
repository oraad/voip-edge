"""Unit tests for console.db — sqlite layer of the read-only browser (T3)."""
from __future__ import annotations

import os
import sqlite3

import pytest

from console import db
from console.db import write_transaction
from console.errors import DBBusy
from tests.fixtures import seeddb

DOMAIN = "sip.alpha.home.example.net"


def _connect(path):
    return db.connect(path)


def test_connect_sets_wal_and_busy_timeout(tmp_path):
    path = seeddb.seed(tmp_path / "edge.sqlite")
    conn = _connect(path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 2000
    finally:
        conn.close()


def test_connect_never_creates_a_missing_database(tmp_path):
    missing = tmp_path / "not-there.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        db.connect(missing)
    assert not missing.exists()


def test_read_rows_returns_dicts_with_database_types(tmp_path):
    path = seeddb.seed(tmp_path / "edge.sqlite")
    conn = _connect(path)
    try:
        registry = db.read_rows(conn, "tenant_registry")
        assert isinstance(registry, list) and registry
        assert all(isinstance(row, dict) for row in registry)
        assert isinstance(registry[0]["tprefix"], str)
        assert isinstance(registry[0]["tvalue"], str)

        dispatcher = db.read_rows(conn, "dispatcher")
        assert isinstance(dispatcher[0]["id"], int)
        assert isinstance(dispatcher[0]["setid"], int)
        assert dispatcher[0]["destination"] == "sip:10.0.0.10:5060;transport=tcp"
    finally:
        conn.close()


def test_read_row_by_text_and_integer_key(tmp_path):
    path = seeddb.seed(tmp_path / "edge.sqlite")
    conn = _connect(path)
    try:
        registry = db.read_row(conn, "tenant_registry", DOMAIN)
        assert registry is not None
        assert registry["tvalue"].startswith("T1,")

        # The path parameter arrives as a string; integer PKs must still match.
        dispatcher = db.read_row(conn, "dispatcher", "1")
        assert dispatcher is not None
        assert dispatcher["setid"] == 11

        assert db.read_row(conn, "dispatcher", "99999") is None
        assert db.read_row(conn, "tenant_registry", "sip.nope.example") is None
    finally:
        conn.close()


def test_read_only_operations_leave_the_file_untouched(tmp_path):
    path = seeddb.seed(tmp_path / "edge.sqlite")
    before = os.stat(path)

    conn = _connect(path)
    try:
        db.read_rows(conn, "tenant_registry")
        db.read_rows(conn, "dispatcher")
        db.read_row(conn, "tenant_registry", DOMAIN)
        db.read_row(conn, "trusted", "1")
    finally:
        conn.close()

    after = os.stat(path)
    assert after.st_ino == before.st_ino
    assert after.st_mtime_ns == before.st_mtime_ns


def test_unsafe_table_names_are_rejected(tmp_path):
    path = seeddb.seed(tmp_path / "edge.sqlite")
    conn = _connect(path)
    try:
        for bad in ("tenant_registry; DROP TABLE trusted", "../tenant_registry", ""):
            with pytest.raises(ValueError):
                db.read_rows(conn, bad)
        with pytest.raises(sqlite3.OperationalError):
            db.read_rows(conn, "no_such_table")
    finally:
        conn.close()


def test_key_column_is_generic_pk_lookup(tmp_path):
    path = seeddb.seed(tmp_path / "edge.sqlite")
    conn = _connect(path)
    try:
        assert db.key_column(conn, "tenant_registry") == "tprefix"
        assert db.key_column(conn, "tenant_landline") == "tprefix"
        assert db.key_column(conn, "dispatcher") == "id"
        assert db.key_column(conn, "dialplan") == "id"
        assert db.key_column(conn, "trusted") == "id"
    finally:
        conn.close()


def test_write_lock_helper_is_available():
    with db.write_lock:
        assert db.write_lock.locked()
    assert not db.write_lock.locked()


def test_begin_lock_is_mapped_to_dbbusy(tmp_path):
    """A busy/locked BEGIN IMMEDIATE surfaces as DBBusy, not raw sqlite."""
    import sqlite3

    path = tmp_path / "edge.sqlite"
    conn1 = sqlite3.connect(str(path), timeout=0.0)
    conn1.execute("CREATE TABLE t (x)")
    conn1.commit()
    conn1.execute("BEGIN IMMEDIATE")

    conn2 = sqlite3.connect(str(path), timeout=0.0)
    try:
        with pytest.raises(DBBusy):
            with write_transaction(conn2):
                conn2.execute("INSERT INTO t VALUES (1)")
    finally:
        conn1.close()
        conn2.close()
