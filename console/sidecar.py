"""sidecar.py — ``console.sqlite``: audit, snapshots, intents, pending reloads.

The sidecar is the console's own durable state.  It is a **separate** SQLite
database from the Kamailio ``edge.sqlite`` so that nothing here can ever
change the edge schema.  Plan §5.6/§5.7 make it the source of truth for:

* ``console_audit``          — one row per state transition (op, domain, state,
  detail, ts);
* ``console_parked``         — the JSON snapshot of a parked home (domain is
  unique; the whole snapshot and the intent bump live in **one** sidecar
  transaction);
* ``console_intent``         — durable lifecycle intents (uuid idempotency
  key, op, domain, mode, state, last_error);
* ``console_pending_reload`` — reload intents persisted while Kamailio is
  unreachable (``table`` + ``enqueued_at`` only; the RPC name and params are
  re-derived from the descriptor at sweep time).

Schema management is idempotent ``CREATE TABLE IF NOT EXISTS`` plus
``PRAGMA user_version`` migrations.  Any unreadable path (corrupt file,
missing parent directory, I/O error) raises
:class:`console.errors.SidecarUnavailable` — lifecycle callers must open the
sidecar **before** touching ``edge.sqlite``.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from console.errors import SidecarUnavailable

__all__ = [
    "Sidecar",
    "open_sidecar",
    "reissue_pending_reloads",
    "SCHEMA_VERSION",
]

SCHEMA_VERSION = 1

_TABLES = (
    "console_audit",
    "console_parked",
    "console_intent",
    "console_pending_reload",
)

# Each entry is the list of statements for that version.  Index + 1 is the
# resulting ``PRAGMA user_version``.  Never rewrite a released migration; add
# a new list instead.
_MIGRATIONS: list[list[str]] = [
    # -- v1 ---------------------------------------------------------------
    [
        """
        CREATE TABLE IF NOT EXISTS console_audit (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            ts       TEXT NOT NULL,
            op       TEXT NOT NULL,
            domain   TEXT NOT NULL,
            state    TEXT NOT NULL,
            detail   TEXT NOT NULL DEFAULT ''
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_console_audit_domain "
        "ON console_audit(domain, id)",
        """
        CREATE TABLE IF NOT EXISTS console_parked (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            domain     TEXT NOT NULL UNIQUE,
            tenantid   TEXT,
            snapshot   TEXT NOT NULL,
            size_bytes INTEGER NOT NULL DEFAULT 0,
            intent_id  TEXT,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS console_intent (
            id         TEXT PRIMARY KEY,
            op         TEXT NOT NULL,
            domain     TEXT NOT NULL,
            mode       TEXT NOT NULL DEFAULT '',
            state      TEXT NOT NULL,
            last_error TEXT,
            detail     TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_console_intent_state "
        "ON console_intent(state, created_at)",
        """
        CREATE TABLE IF NOT EXISTS console_pending_reload (
            "table"     TEXT PRIMARY KEY,
            enqueued_at TEXT NOT NULL,
            last_error  TEXT
        )
        """,
    ],
]


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _json_dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _safe_rollback(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


class Sidecar:
    """Open (and migrate) ``console.sqlite``.

    Construction verifies readability and applies pending migrations, so a
    caller can trust that no lifecycle step will fail later merely because
    the sidecar path is unusable.
    """

    def __init__(self, path: str | Path, timeout: float = 2.0) -> None:
        self.path = Path(path)
        self.timeout = float(timeout)
        self._prepare()

    # -- opening / schema ---------------------------------------------

    @property
    def db_path(self) -> Path:
        return self.path

    def connect(self) -> sqlite3.Connection:
        """A fresh autocommit connection with ``busy_timeout`` applied.

        Callers own the connection.  Writes that must be atomic go through
        :meth:`transaction`.
        """
        conn = sqlite3.connect(str(self.path), timeout=self.timeout)
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None  # explicit BEGIN/COMMIT only
        conn.execute("PRAGMA busy_timeout = 2000")
        return conn

    def _prepare(self) -> None:
        parent = self.path.parent
        if not parent.exists():
            raise SidecarUnavailable(
                f"sidecar directory does not exist: {parent}"
            )
        try:
            conn = self.connect()
        except (sqlite3.Error, OSError) as exc:
            raise SidecarUnavailable(
                f"sidecar unreadable at {self.path}: {exc}"
            ) from exc
        try:
            # Force SQLite to read the header now: a corrupt file otherwise
            # only fails on the first real query.
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
            _migrate(conn)
        except (sqlite3.Error, OSError) as exc:
            raise SidecarUnavailable(
                f"sidecar unreadable at {self.path}: {exc}"
            ) from exc
        finally:
            conn.close()

    def verify(self) -> None:
        """Re-check that the file is still readable (used by long-lived apps)."""
        try:
            conn = self.connect()
            try:
                conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
            finally:
                conn.close()
        except (sqlite3.Error, OSError) as exc:
            raise SidecarUnavailable(
                f"sidecar unreadable at {self.path}: {exc}"
            ) from exc

    def schema_version(self) -> int:
        with self._cursor() as conn:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])

    def table_names(self) -> set[str]:
        with self._cursor() as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
            return {str(row["name"]) for row in rows}

    # -- low-level helpers --------------------------------------------

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Connection]:
        try:
            conn = self.connect()
        except (sqlite3.Error, OSError) as exc:
            raise SidecarUnavailable(str(exc)) from exc
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """``BEGIN IMMEDIATE`` … ``COMMIT``/``ROLLBACK``.

        Any :class:`sqlite3.Error` becomes :class:`SidecarUnavailable` after
        the rollback, so callers have one sidecar failure type to handle.
        """
        try:
            conn = self.connect()
        except (sqlite3.Error, OSError) as exc:
            raise SidecarUnavailable(str(exc)) from exc
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except sqlite3.Error as exc:
                _safe_rollback(conn)
                raise SidecarUnavailable(
                    f"sidecar write failed at {self.path}: {exc}"
                ) from exc
            except BaseException:
                _safe_rollback(conn)
                raise
        finally:
            conn.close()

    def _execute(
        self,
        sql: str,
        params: tuple | list = (),
        *,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        """Run one statement; return the affected row count.

        With *conn* the caller owns the transaction; otherwise the statement
        runs on its own autocommit connection.
        """
        if conn is not None:
            return conn.execute(sql, params).rowcount
        try:
            with self._cursor() as own:
                return own.execute(sql, params).rowcount
        except sqlite3.Error as exc:
            raise SidecarUnavailable(
                f"sidecar write failed at {self.path}: {exc}"
            ) from exc

    # -- audit ---------------------------------------------------------

    def audit(
        self,
        op: str,
        domain: str,
        state: str,
        detail: str = "",
        *,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        """Append one ``console_audit`` row (same txn when *conn* is given)."""
        self._execute(
            "INSERT INTO console_audit (ts, op, domain, state, detail) "
            "VALUES (?, ?, ?, ?, ?)",
            (_utcnow(), op, domain, state, detail or ""),
            conn=conn,
        )

    def audit_rows(
        self,
        *,
        op: str | None = None,
        domain: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if op is not None:
            clauses.append("op = ?")
            params.append(op)
        if domain is not None:
            clauses.append("domain = ?")
            params.append(domain)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, int(limit)))
        with self._cursor() as conn:
            rows = conn.execute(
                f"SELECT id, ts, op, domain, state, detail FROM console_audit "
                f"{where} ORDER BY id LIMIT ?",
                tuple(params),
            ).fetchall()
        return [dict(row) for row in rows]

    # -- intents -------------------------------------------------------

    def create_intent(
        self,
        op: str,
        domain: str,
        *,
        state: str = "STARTED",
        mode: str = "",
        detail: str = "",
    ) -> str:
        """Create a durable intent and record its STARTED transition atomically."""
        intent_id = str(uuid.uuid4())
        now = _utcnow()
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO console_intent "
                "(id, op, domain, mode, state, last_error, detail, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)",
                (intent_id, op, domain, mode, state, detail or "", now, now),
            )
            self.audit(op, domain, state, detail, conn=conn)
        return intent_id

    def get_intent(self, intent_id: str) -> dict[str, Any] | None:
        with self._cursor() as conn:
            row = conn.execute(
                "SELECT id, op, domain, mode, state, last_error, detail, "
                "created_at, updated_at FROM console_intent WHERE id = ?",
                (intent_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_intents(self, *, include_done: bool = False) -> list[dict[str, Any]]:
        where = "" if include_done else "WHERE state != 'DONE'"
        with self._cursor() as conn:
            rows = conn.execute(
                "SELECT id, op, domain, mode, state, last_error, detail, "
                f"created_at, updated_at FROM console_intent {where} "
                "ORDER BY created_at, rowid"
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_intent(
        self, op: str, domain: str, *, include_done: bool = True
    ) -> dict[str, Any] | None:
        suffix = "" if include_done else " AND state != 'DONE'"
        with self._cursor() as conn:
            row = conn.execute(
                "SELECT id, op, domain, mode, state, last_error, detail, "
                "created_at, updated_at FROM console_intent "
                "WHERE op = ? AND domain = ?" + suffix + " "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (op, domain),
            ).fetchone()
        return dict(row) if row is not None else None

    def _transition(
        self,
        conn: sqlite3.Connection,
        intent_id: str,
        state: str,
        detail: str,
        last_error: str | None,
    ) -> None:
        row = conn.execute(
            "SELECT op, domain FROM console_intent WHERE id = ?", (intent_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown intent {intent_id}")
        conn.execute(
            "UPDATE console_intent SET state = ?, last_error = ?, "
            "detail = ?, updated_at = ? WHERE id = ?",
            (state, last_error, detail or "", _utcnow(), intent_id),
        )
        self.audit(str(row["op"]), str(row["domain"]), state, detail, conn=conn)

    def record_transition(
        self,
        intent_id: str,
        state: str,
        *,
        detail: str = "",
        last_error: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        """Commit a state transition + its audit row (one transaction)."""
        if conn is not None:
            self._transition(conn, intent_id, state, detail, last_error)
            return
        with self.transaction() as own:
            self._transition(own, intent_id, state, detail, last_error)

    def fail_intent(self, intent_id: str, error: str) -> None:
        """Record ``last_error`` without changing the committed state.

        Failures are *not* transitions, so no audit row is written; the intent
        row keeps the last successfully committed state.
        """
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT state FROM console_intent WHERE id = ?", (intent_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown intent {intent_id}")
            conn.execute(
                "UPDATE console_intent SET last_error = ?, updated_at = ? "
                "WHERE id = ?",
                (error, _utcnow(), intent_id),
            )

    # -- parked snapshots ---------------------------------------------

    def save_snapshot(
        self,
        domain: str,
        snapshot: dict[str, Any],
        *,
        tenantid: str | None = None,
        intent_id: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        """Store/replace the JSON snapshot for *domain*; returns its byte size."""
        payload = _json_dump(snapshot)
        size = len(payload.encode("utf-8"))
        if tenantid is None:
            tenantid = snapshot.get("tenantid")
        now = _utcnow()
        self._execute(
            "INSERT INTO console_parked "
            "(domain, tenantid, snapshot, size_bytes, intent_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(domain) DO UPDATE SET "
            "tenantid = excluded.tenantid, snapshot = excluded.snapshot, "
            "size_bytes = excluded.size_bytes, intent_id = excluded.intent_id",
            (domain, tenantid, payload, size, intent_id, now),
            conn=conn,
        )
        return size

    def get_parked(self, domain: str) -> dict[str, Any] | None:
        with self._cursor() as conn:
            row = conn.execute(
                "SELECT id, domain, tenantid, snapshot, size_bytes, intent_id, "
                "created_at FROM console_parked WHERE domain = ?",
                (domain,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        try:
            result["snapshot"] = json.loads(result.pop("snapshot"))
        except (TypeError, ValueError) as exc:
            raise SidecarUnavailable(
                f"parked snapshot for {domain} is corrupt: {exc}"
            ) from exc
        return result

    def load_snapshot(self, domain: str) -> dict[str, Any] | None:
        row = self.get_parked(domain)
        return None if row is None else row["snapshot"]

    def list_parked(self) -> list[dict[str, Any]]:
        with self._cursor() as conn:
            rows = conn.execute(
                "SELECT id, domain, tenantid, size_bytes, intent_id, created_at "
                "FROM console_parked ORDER BY created_at, domain"
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_snapshot(
        self,
        domain: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        return self._execute(
            "DELETE FROM console_parked WHERE domain = ?", (domain,), conn=conn
        ) > 0

    def parked_rows(self) -> list[dict[str, Any]]:
        """Raw parked rows (snapshot kept as decoded JSON) for backup dumps."""
        with self._cursor() as conn:
            rows = conn.execute(
                "SELECT id, domain, tenantid, snapshot, size_bytes, intent_id, "
                "created_at FROM console_parked ORDER BY created_at, domain"
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["snapshot"] = json.loads(item["snapshot"])
            except (TypeError, ValueError):
                item["snapshot"] = None
            result.append(item)
        return result

    # -- pending reloads (§5.6) ---------------------------------------

    def record_pending_reload(
        self,
        table: str,
        *,
        enqueued_at: str | None = None,
        last_error: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        """Persist (or refresh) one pending reload intent for *table*."""
        self._execute(
            'INSERT INTO console_pending_reload ("table", enqueued_at, last_error) '
            "VALUES (?, ?, ?) "
            'ON CONFLICT("table") DO UPDATE SET '
            "enqueued_at = excluded.enqueued_at, last_error = excluded.last_error",
            (table, enqueued_at or _utcnow(), last_error),
            conn=conn,
        )

    def clear_pending_reload(
        self,
        table: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        return self._execute(
            'DELETE FROM console_pending_reload WHERE "table" = ?',
            (table,),
            conn=conn,
        ) > 0

    def pending_reloads(self) -> list[dict[str, Any]]:
        with self._cursor() as conn:
            rows = conn.execute(
                'SELECT "table", enqueued_at, last_error '
                "FROM console_pending_reload ORDER BY enqueued_at, rowid"
            ).fetchall()
        return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def open_sidecar(sidecar: Sidecar | str | Path) -> Sidecar:
    """Return a verified :class:`Sidecar` (raising SidecarUnavailable)."""
    if isinstance(sidecar, Sidecar):
        sidecar.verify()
        return sidecar
    return Sidecar(sidecar)


def reissue_pending_reloads(
    sidecar: Sidecar | str | Path,
    worker: Any,
    tableset: Any,
    *,
    timeout: float = 30.0,
) -> list[str]:
    """Re-enqueue every persisted pending reload (startup sweeper helper).

    The RPC name and params are **re-derived from the descriptor** (§5.6);
    only the table key is persisted.  Pending rows are cleared for tables that
    reload successfully and left in place (with ``last_error``) otherwise.
    Returns the table names that were re-issued.
    """
    sc = open_sidecar(sidecar)
    pending = sc.pending_reloads()
    issued: list[str] = []
    for row in pending:
        table = str(row["table"])
        descriptor = tableset.tables.get(table)
        if descriptor is None:
            # §5.6 allows the persisted key to be the mtree tree name for
            # packed tables (e.g. "tenantdom"/"landline"); resolve it back to
            # the descriptor that owns that tree.
            for candidate in tableset.tables.values():
                if (
                    candidate.storage == "packed"
                    and candidate.reload.params
                    and str(candidate.reload.params[0]) == table
                ):
                    descriptor = candidate
                    break
        if descriptor is None:
            continue
        worker.enqueue(table, descriptor.reload.rpc, descriptor.reload.params)
        issued.append(table)

    if not issued:
        return []

    if not worker.flush(timeout):
        for table in issued:
            sc.record_pending_reload(table, last_error="reload did not drain")
        return issued

    errors = getattr(worker, "errors", {})
    for table in issued:
        exc = errors.get(table)
        if exc is None:
            sc.clear_pending_reload(table)
        else:
            sc.record_pending_reload(table, last_error=str(exc))
    return issued


def _migrate(conn: sqlite3.Connection) -> None:
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    for version, statements in enumerate(_MIGRATIONS, start=1):
        if current >= version:
            continue
        for statement in statements:
            conn.execute(statement)
        # PRAGMA cannot be parameterised; version is an integer we control.
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
