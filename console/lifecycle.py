"""lifecycle.py — park / restore / destroy state machines (plan §5.3, §5.7).

Durable ordering contract (park)::

    STARTED → BACKED_UP → SNAPSHOTTED → ROWS_DELETED → RELOADED → PURGED → DONE

* ``SNAPSHOTTED`` writes all four row groups into ``console_parked`` **and**
  advances the intent row (plus its audit row) in the *same* console.sqlite
  transaction.
* ``ROWS_DELETED`` is one short ``edge.sqlite`` transaction (all-or-nothing).
* ``RELOADED`` enqueues tenantdom/landline/dispatcher/trusted on the single
  :class:`console.reloads.ReloadWorker` and waits for the drain.
* ``PURGED`` runs **after** the reload: dump regcache, ``htable.delete`` every
  key ending ``@<domain>``, dump-verify zero, bounded retry.  Reload-before-
  purge closes the re-register race (plan §5.3, review ledger).
* ``DONE`` is only written after the purge gate passes.

Restore::

    STARTED → BACKED_UP → COLLISION_CHECKED → ROWS_INSERTED → RELOADED → DONE

Destroy active uses the park sequence plus ``SNAPSHOT_DISCARDED`` before
``DONE``; destroying a parked home only deletes the snapshot row.  Shared
dialplan partitions (10/90) are never touched by any lifecycle operation.

Every transition is idempotent.  A failed step only records
``console_intent.last_error`` and leaves the committed state unchanged;
:func:`resume_pending` re-runs from that state.  ``fail_after`` is a test
seam that crashes *after* a state is committed, so the resume tests can inject
a failure between every consecutive state pair.

The sidecar is opened (and thereby verified) **before** any edge mutation; an
unreadable sidecar raises :class:`SidecarUnavailable` while ``edge.sqlite``
is untouched.
"""
from __future__ import annotations

import threading

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from console import descriptors
from console import observability
from console.backups import create_backup
from console.descriptors import Descriptor, TableSet, unpack_row
from console.errors import (
    ConsoleError,
    DBBusy,
    KamailioUnavailable,
    SidecarUnavailable,
    ValidationError,
)
from console.rpc import flatten_htable_dump
from console.sidecar import Sidecar, open_sidecar

__all__ = [
    "PARK_STATES",
    "RESTORE_STATES",
    "DESTROY_ACTIVE_STATES",
    "DESTROY_PARKED_STATES",
    "RELOAD_TABLES",
    "LifecycleError",
    "CollisionError",
    "PurgeError",
    "InjectedCrash",
    "LifecycleResult",
    "park",
    "restore",
    "destroy",
    "resume_pending",
    "purge_regcache",
]

RELOAD_TABLES = ("tenant_registry", "tenant_landline", "dispatcher", "trusted")

PARK_STATES = (
    "STARTED",
    "BACKED_UP",
    "SNAPSHOTTED",
    "ROWS_DELETED",
    "RELOADED",
    "PURGED",
    "DONE",
)

RESTORE_STATES = (
    "STARTED",
    "BACKED_UP",
    "COLLISION_CHECKED",
    "ROWS_INSERTED",
    "RELOADED",
    "DONE",
)

DESTROY_ACTIVE_STATES = (
    "STARTED",
    "BACKED_UP",
    "SNAPSHOTTED",
    "ROWS_DELETED",
    "RELOADED",
    "PURGED",
    "SNAPSHOT_DISCARDED",
    "DONE",
)

DESTROY_PARKED_STATES = ("STARTED", "SNAPSHOT_DISCARDED", "DONE")

_DISPATCHER_COLUMNS = (
    "setid",
    "destination",
    "flags",
    "priority",
    "attrs",
    "description",
)
_TRUSTED_COLUMNS = (
    "src_ip",
    "proto",
    "from_pattern",
    "ruri_pattern",
    "tag",
    "priority",
)
_COLUMNS_TABLES = {
    "dispatcher": _DISPATCHER_COLUMNS,
    "trusted": _TRUSTED_COLUMNS,
}


# ---------------------------------------------------------------------------
# errors / results
# ---------------------------------------------------------------------------
class LifecycleError(ConsoleError):
    """A lifecycle step failed (state stays at the last committed value)."""


class CollisionError(LifecycleError):
    """Restore blocked by one of the five collision classes."""

    def __init__(self, collisions: list[str]) -> None:
        self.collisions = list(collisions)
        super().__init__("restore blocked: " + "; ".join(self.collisions))


class PurgeError(LifecycleError):
    """Regcache still holds keys for the domain after the bounded retry."""


class InjectedCrash(LifecycleError):
    """Test seam: raised after a state commit to simulate a crash."""


@dataclass(frozen=True)
class LifecycleResult:
    intent_id: str
    op: str
    domain: str
    state: str
    done: bool
    error: str | None = None

    def __getitem__(self, key: str) -> Any:
        """Allow dict-style access for route/sweeper callers."""
        try:
            return getattr(self, key)
        except AttributeError as exc:
            raise KeyError(key) from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "op": self.op,
            "domain": self.domain,
            "state": self.state,
            "done": self.done,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# edge.sqlite helpers
# ---------------------------------------------------------------------------
def _edge_path(edge: str | Path) -> Path:
    path = Path(edge)
    if not path.is_file():
        raise LifecycleError(f"edge database not found: {path}")
    return path


def _edge_connect(path: Path) -> sqlite3.Connection:
    # mode=rw: reads (e.g. the destroy confirmation page) must never create a
    # zero-byte edge.sqlite that later trips the schema consistency gate.
    uri = path.resolve().as_uri() + "?mode=rw"
    conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 2000")
    return conn


@contextmanager
def _edge_read(path: Path) -> Iterator[sqlite3.Connection]:
    conn = _edge_connect(path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _edge_transaction(path: Path) -> Iterator[sqlite3.Connection]:
    """One short ``BEGIN IMMEDIATE`` … ``COMMIT``/``ROLLBACK`` on edge.sqlite."""
    conn = _edge_connect(path)
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            conn.close()
            message = str(exc).lower()
            if "locked" in message or "busy" in message:
                raise DBBusy(f"edge database busy: {exc}") from exc
            raise LifecycleError(f"edge transaction could not start: {exc}") from exc
        try:
            yield conn
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            message = str(exc).lower()
            if "locked" in message or "busy" in message:
                raise DBBusy(f"edge database busy: {exc}") from exc
            raise LifecycleError(f"edge transaction failed: {exc}") from exc
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
    finally:
        conn.close()


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _tenant_row(path: Path, domain: str) -> dict[str, Any] | None:
    with _edge_read(path) as conn:
        row = conn.execute(
            "SELECT tprefix, tvalue FROM tenant_registry WHERE tprefix = ?",
            (domain,),
        ).fetchone()
    return dict(row) if row is not None else None


def _require_tenant(path: Path, domain: str) -> None:
    if _tenant_row(path, domain) is None:
        raise LifecycleError(f"home {domain!r} is not active")


def _snapshot_is_parked(sc: Sidecar, domain: str) -> bool:
    """True only when the newest completed intent for *domain* was a park.

    Snapshot existence alone is not proof: restore deliberately keeps the
    snapshot.  Used when the edge DB is absent/unreadable and the registry
    cannot tell active from parked.
    """
    latest = sc.latest_intent_any(domain, include_done=True)
    return bool(
        latest
        and str(latest.get("op")) == "park"
        and str(latest.get("state")) == "DONE"
    )


def _descriptor(tableset: TableSet, table: str) -> Descriptor:
    try:
        return tableset.tables[table]
    except KeyError as exc:  # pragma: no cover - tables.yaml is frozen
        raise LifecycleError(f"descriptor for {table!r} is missing") from exc


# ---------------------------------------------------------------------------
# snapshot / delete / insert
# ---------------------------------------------------------------------------
def _collect_snapshot(
    edge: Path, domain: str, tableset: TableSet
) -> dict[str, Any]:
    """Read all four row groups belonging to *domain* (no mutation)."""
    registry_desc = _descriptor(tableset, "tenant_registry")
    landline_desc = _descriptor(tableset, "tenant_landline")

    with _edge_read(edge) as conn:
        # One read snapshot for all four groups so a concurrent CLI edit
        # cannot produce a half-and-half snapshot (WAL readers never block).
        conn.execute("BEGIN")
        row = conn.execute(
            "SELECT tprefix, tvalue FROM tenant_registry WHERE tprefix = ?",
            (domain,),
        ).fetchone()
        if row is None:
            raise LifecycleError(f"home {domain!r} is not active")
        registry_row = dict(row)
        tenant = unpack_row(registry_desc, registry_row)
        tenantid = str(tenant.get("tenantid") or "")
        setids = sorted(
            {
                value
                for value in (
                    _int(tenant.get("ds_onnet")),
                    _int(tenant.get("ds_pstn")),
                )
                if value
            }
        )

        landlines: list[dict[str, Any]] = []
        for landline_row in conn.execute(
            "SELECT tprefix, tvalue FROM tenant_landline ORDER BY tprefix"
        ):
            candidate = dict(landline_row)
            try:
                unpacked = unpack_row(landline_desc, candidate)
            except (KeyError, ValueError):
                continue
            if str(unpacked.get("tenantid") or "") == tenantid:
                landlines.append(candidate)

        if setids:
            placeholders = ", ".join("?" for _ in setids)
            dispatcher_rows = conn.execute(
                f"SELECT id, {', '.join(_DISPATCHER_COLUMNS)} FROM dispatcher "
                f"WHERE setid IN ({placeholders}) ORDER BY setid, priority, id",
                tuple(setids),
            ).fetchall()
        else:
            dispatcher_rows = []

        trusted_rows = conn.execute(
            f"SELECT id, {', '.join(_TRUSTED_COLUMNS)} FROM trusted "
            "WHERE tag = ? ORDER BY src_ip, id",
            (domain,),
        ).fetchall()

    return {
        "version": 1,
        "domain": domain,
        "tenantid": tenantid,
        "ds_setids": setids,
        "tenant_registry": [registry_row],
        "tenant_landline": landlines,
        "dispatcher": [dict(item) for item in dispatcher_rows],
        "trusted": [dict(item) for item in trusted_rows],
    }


def _snapshot_detail(snapshot: dict[str, Any]) -> str:
    return (
        "snapshot: registry={} landline={} dispatcher={} trusted={}".format(
            len(snapshot.get("tenant_registry") or []),
            len(snapshot.get("tenant_landline") or []),
            len(snapshot.get("dispatcher") or []),
            len(snapshot.get("trusted") or []),
        )
    )


def _other_home_set_references(
    conn: sqlite3.Connection,
    domain: str,
    setids: list[Any],
    tableset: TableSet,
) -> list[str]:
    """Other homes whose on-net/PSTN set intersects *setids*.

    Dispatcher rows are keyed only by setid; deleting a shared set would remove
    another home's routing.  The form validators allow sharing (with a warning),
    so lifecycle must refuse the destructive scope explicitly.
    """
    wanted = {int(value) for value in setids if value}
    if not wanted:
        return []
    registry = _descriptor(tableset, "tenant_registry")
    hits: set[str] = set()
    for row in conn.execute("SELECT tprefix, tvalue FROM tenant_registry"):
        owner = str(row["tprefix"])
        if owner == domain:
            continue
        try:
            values = unpack_row(registry, dict(row))
        except (KeyError, ValueError):
            continue
        used = {
            _int(values.get("ds_onnet")),
            _int(values.get("ds_pstn")),
        }
        if used & wanted:
            hits.add(owner)
    return sorted(hits)


def _delete_edge_rows(
    conn: sqlite3.Connection, snapshot: dict[str, Any]
) -> dict[str, int]:
    """Delete every snapshotted row in the caller's transaction."""
    domain = str(snapshot["domain"])
    counts: dict[str, int] = {}

    landline_keys = [
        str(item.get("tprefix"))
        for item in snapshot.get("tenant_landline") or []
        if item.get("tprefix") is not None
    ]
    if landline_keys:
        placeholders = ", ".join("?" for _ in landline_keys)
        cursor = conn.execute(
            f"DELETE FROM tenant_landline WHERE tprefix IN ({placeholders})",
            tuple(landline_keys),
        )
        counts["tenant_landline"] = cursor.rowcount
    else:
        counts["tenant_landline"] = 0

    cursor = conn.execute(
        "DELETE FROM tenant_registry WHERE tprefix = ?", (domain,)
    )
    counts["tenant_registry"] = cursor.rowcount

    setids = [value for value in snapshot.get("ds_setids") or [] if value]
    if setids:
        placeholders = ", ".join("?" for _ in setids)
        cursor = conn.execute(
            f"DELETE FROM dispatcher WHERE setid IN ({placeholders})",
            tuple(setids),
        )
        counts["dispatcher"] = cursor.rowcount
    else:
        counts["dispatcher"] = 0

    cursor = conn.execute("DELETE FROM trusted WHERE tag = ?", (domain,))
    counts["trusted"] = cursor.rowcount
    return counts


def _guarded_insert(
    conn: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
    row: dict[str, Any],
    guard: tuple[str, ...],
) -> int:
    column_sql = ", ".join(f'"{name}"' for name in columns)
    placeholders = ", ".join("?" for _ in columns)
    guard_sql = " AND ".join(f'"{name}" IS ?' for name in guard)
    sql = (
        f'INSERT INTO "{table}" ({column_sql}) '
        f"SELECT {placeholders} "
        f'WHERE NOT EXISTS (SELECT 1 FROM "{table}" WHERE {guard_sql})'
    )
    params = [row.get(name) for name in columns]
    params.extend(row.get(name) for name in guard)
    return conn.execute(sql, tuple(params)).rowcount


def _row_present(
    conn: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
    row: Mapping[str, Any],
) -> bool:
    """True when *row* (by all *columns*) is already in *table*."""
    where = " AND ".join(f'"{name}" IS ?' for name in columns)
    params = tuple(row.get(name) for name in columns)
    found = conn.execute(
        f'SELECT 1 FROM "{table}" WHERE {where} LIMIT 1', params
    ).fetchone()
    return found is not None


def _missing_rows(
    conn: sqlite3.Connection, snapshot: dict[str, Any]
) -> dict[str, int]:
    """Rows from *snapshot* that are absent or differ from the live table."""
    groups: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("tenant_registry", ("tprefix", "tvalue")),
        ("tenant_landline", ("tprefix", "tvalue")),
        ("dispatcher", _DISPATCHER_COLUMNS),
        ("trusted", _TRUSTED_COLUMNS),
    )
    missing: dict[str, int] = {}
    for table, columns in groups:
        count = sum(
            1
            for row in snapshot.get(table) or []
            if not _row_present(conn, table, columns, row)
        )
        if count:
            missing[table] = count
    return missing


def _insert_edge_rows(
    conn: sqlite3.Connection, snapshot: dict[str, Any]
) -> dict[str, int]:
    """Guarded inserts (``WHERE NOT EXISTS``) — never ``OR REPLACE``."""
    counts = {
        "tenant_registry": 0,
        "tenant_landline": 0,
        "dispatcher": 0,
        "trusted": 0,
    }

    for row in snapshot.get("tenant_registry") or []:
        counts["tenant_registry"] += _guarded_insert(
            conn, "tenant_registry", ("tprefix", "tvalue"), row, ("tprefix",)
        )

    for row in snapshot.get("tenant_landline") or []:
        counts["tenant_landline"] += _guarded_insert(
            conn, "tenant_landline", ("tprefix", "tvalue"), row, ("tprefix",)
        )

    for table, columns in _COLUMNS_TABLES.items():
        for row in snapshot.get(table) or []:
            counts[table] += _guarded_insert(conn, table, columns, row, columns)

    return counts


# ---------------------------------------------------------------------------
# restore collision checks (five classes)
# ---------------------------------------------------------------------------
def _check_collisions(
    edge: Path,
    snapshot: dict[str, Any],
    tableset: TableSet,
    *,
    conn: sqlite3.Connection | None = None,
) -> None:
    if conn is None:
        with _edge_read(edge) as owned:
            return _check_collisions(
                edge, snapshot, tableset, conn=owned
            )
    domain = str(snapshot["domain"])
    tenantid = str(snapshot.get("tenantid") or "")
    landline_prefixes = {
        str(item.get("tprefix"))
        for item in snapshot.get("tenant_landline") or []
        if item.get("tprefix") is not None
    }
    setids = [value for value in snapshot.get("ds_setids") or [] if value]
    src_ips = {
        str(item.get("src_ip"))
        for item in snapshot.get("trusted") or []
        if item.get("src_ip") is not None
    }

    registry_desc = _descriptor(tableset, "tenant_registry")
    landline_desc = _descriptor(tableset, "tenant_landline")
    collisions: list[str] = []

    # 1. domain
    row = conn.execute(
        "SELECT tprefix, tvalue FROM tenant_registry WHERE tprefix = ?",
        (domain,),
    ).fetchone()
    if row is not None:
        collisions.append(
            f"domain: tenant_registry.tprefix={domain!r} already exists"
        )

    # 2. tenantid
    for candidate in conn.execute(
        "SELECT tprefix, tvalue FROM tenant_registry ORDER BY tprefix"
    ):
        row_dict = dict(candidate)
        try:
            unpacked = unpack_row(registry_desc, row_dict)
        except (KeyError, ValueError):
            continue
        if str(unpacked.get("tenantid") or "") == tenantid:
            collisions.append(
                "tenantid: tenant_registry.tenantid="
                f"{tenantid!r} already used by tprefix={row_dict.get('tprefix')!r}"
            )
            break

    # 3. landline prefix digits
    if landline_prefixes:
        for candidate in conn.execute(
            "SELECT tprefix, tvalue FROM tenant_landline ORDER BY tprefix"
        ):
            row_dict = dict(candidate)
            if str(row_dict.get("tprefix")) in landline_prefixes:
                try:
                    owner = unpack_row(landline_desc, row_dict).get("tenantid")
                except (KeyError, ValueError):
                    owner = "?"
                collisions.append(
                    "landline: tenant_landline.tprefix="
                    f"{row_dict.get('tprefix')!r} already exists "
                    f"(tenantid={owner!r})"
                )
                break

        # Longest-prefix shadowing is a different collision class: an
        # exact key check is not enough (1001 vs 1001999).
        for candidate in conn.execute(
            "SELECT tprefix, tvalue FROM tenant_landline ORDER BY tprefix"
        ):
            row_dict = dict(candidate)
            other = str(row_dict.get("tprefix") or "")
            if not other or other in landline_prefixes:
                continue
            try:
                owner = str(
                    unpack_row(landline_desc, row_dict).get("tenantid") or ""
                )
            except (KeyError, ValueError):
                continue
            if owner == tenantid:
                continue
            if any(
                prefix
                and (other.startswith(prefix) or prefix.startswith(other))
                for prefix in landline_prefixes
            ):
                collisions.append(
                    "landline: snapshot prefix(es) "
                    + ", ".join(sorted(landline_prefixes))
                    + f" overlap {other!r} owned by {owner!r}"
                )
                break

    # 4. dispatcher set ids (also refuse sets shared with another home)
    if setids:
        shared = _other_home_set_references(
            conn, domain, setids, tableset
        )
        if shared:
            collisions.append(
                "dispatcher: set(s) "
                + ", ".join(str(value) for value in setids)
                + " also used by "
                + ", ".join(shared)
            )
        placeholders = ", ".join("?" for _ in setids)
        candidate = conn.execute(
            "SELECT id, setid, destination FROM dispatcher "
            f"WHERE setid IN ({placeholders}) ORDER BY setid, id LIMIT 1",
            tuple(setids),
        ).fetchone()
        if candidate is not None:
            collisions.append(
                "dispatcher: dispatcher row "
                f"id={candidate['id']} setid={candidate['setid']} "
                f"destination={candidate['destination']!r} already exists"
            )

    # 5. trusted src_ip
    if src_ips:
        placeholders = ", ".join("?" for _ in src_ips)
        candidate = conn.execute(
            "SELECT id, src_ip, tag FROM trusted "
            f"WHERE src_ip IN ({placeholders}) ORDER BY src_ip, id LIMIT 1",
            tuple(sorted(src_ips)),
        ).fetchone()
        if candidate is not None:
            collisions.append(
                "trusted: trusted row "
                f"id={candidate['id']} src_ip={candidate['src_ip']!r} "
                f"tag={candidate['tag']!r} already exists"
            )

    if collisions:
        raise CollisionError(collisions)


# ---------------------------------------------------------------------------
# reload + regcache purge
# ---------------------------------------------------------------------------
def _reload_key(descriptor: Descriptor) -> str:
    """Worker/pending key: the mtree tree name for packed tables (§5.6)."""
    if descriptor.storage == "packed" and descriptor.reload.params:
        return str(descriptor.reload.params[0])
    return descriptor.table


def _reload_tables(
    sidecar: Sidecar,
    worker: Any,
    tableset: TableSet,
    *,
    tables: tuple[str, ...] = RELOAD_TABLES,
    timeout: float = 30.0,
) -> list[str]:
    """Enqueue the four-table batch and wait for the worker to drain.

    Packed tables are enqueued under their mtree tree name (``tenantdom`` /
    ``landline``), matching §5.6's "table key IS the tree name"; columns tables
    use their descriptor table name.
    """
    keys: list[str] = []
    for table in tables:
        descriptor = _descriptor(tableset, table)
        key = _reload_key(descriptor)
        worker.enqueue(key, descriptor.reload.rpc, descriptor.reload.params)
        keys.append(key)

    # Capture the generation persisted by the worker's on_enqueue.  Only
    # clear that exact generation later: a newer save may have refreshed the
    # row between flush() and cleanup.
    stamps = {
        key: sidecar.pending_reload_enqueued_at(key) for key in keys
    }

    if not worker.flush(timeout):
        for key in keys:
            sidecar.record_pending_reload(
                key, last_error="reload did not drain"
            )
        raise LifecycleError(
            f"reload did not drain within {timeout:g}s: {', '.join(keys)}"
        )

    errors = getattr(worker, "errors", {}) or {}
    failures: list[str] = []
    for key in keys:
        exc = errors.get(key)
        if exc is None:
            stamp = stamps.get(key)
            if stamp is not None:
                # A missing stamp means the worker's own store already
                # cleared it; never fall back to an unconditional delete.
                sidecar.clear_pending_reload(
                    key, expected_enqueued_at=stamp
                )
            continue
        failures.append(f"{key}: {exc}")
        if isinstance(exc, KamailioUnavailable):
            # §5.6: keep the intent in the sidecar; the startup sweeper
            # re-issues it once the RPC probe succeeds.
            sidecar.record_pending_reload(key, last_error=str(exc))

    if failures:
        raise LifecycleError("reload failed — " + "; ".join(failures))
    return keys


def _residual_keys(rpc: Any, domain: str) -> list[str]:
    dump = rpc.call("htable.dump", ["regcache"])
    flat = flatten_htable_dump(dump)
    suffix = f"@{domain}"
    return sorted(key for key in flat if key.endswith(suffix))


def purge_regcache(
    rpc: Any, domain: str, *, max_attempts: int = 3
) -> list[str]:
    """Dump → delete every ``@<domain>`` key → dump-verify zero, bounded retry.

    Returns the deleted keys.  Raises :class:`PurgeError` when residual keys
    survive ``max_attempts`` delete rounds (the dump-verify gate).
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    deleted: list[str] = []
    for _ in range(max_attempts):
        residual = _residual_keys(rpc, domain)
        if not residual:
            return deleted
        for key in residual:
            rpc.call("htable.delete", ["regcache", key])
            deleted.append(key)

    residual = _residual_keys(rpc, domain)
    if residual:
        preview = ", ".join(residual[:3])
        raise PurgeError(
            f"regcache purge for {domain} left {len(residual)} key(s) "
            f"after {max_attempts} attempt(s): {preview}"
        )
    return deleted


# ---------------------------------------------------------------------------
# orchestrator
# ---------------------------------------------------------------------------
class _Run:
    """One intent's progress through its state sequence."""

    def __init__(
        self,
        *,
        sidecar: Sidecar,
        intent_id: str,
        op: str,
        domain: str,
        sequence: tuple[str, ...],
        edge: Path,
        worker: Any,
        backup_dir: str | Path | None,
        tableset: TableSet,
        purge_max_attempts: int = 3,
        reload_timeout: float = 30.0,
    ) -> None:
        self.sidecar = sidecar
        self.intent_id = intent_id
        self.op = op
        self.domain = domain
        self.sequence = sequence
        self.edge = edge
        self.worker = worker
        self.backup_dir = None if backup_dir is None else Path(backup_dir)
        self.tableset = tableset
        self.purge_max_attempts = purge_max_attempts
        self.reload_timeout = reload_timeout
        self.snapshot: dict[str, Any] | None = None

    # -- helpers -------------------------------------------------------

    def _load_snapshot(self) -> dict[str, Any]:
        if self.snapshot is None:
            snapshot = self.sidecar.load_snapshot(self.domain)
            if snapshot is None:
                raise LifecycleError(
                    f"no parked snapshot for {self.domain!r}"
                )
            self.snapshot = snapshot
        return self.snapshot

    def _transition(self, state: str, detail: str = "") -> None:
        self.sidecar.record_transition(
            self.intent_id, state, detail=detail
        )
        # T9 observability: the sidecar audit row remains the source of truth;
        # this JSON line is the operational log mirror (no secrets in fields).
        observability.log_lifecycle_transition(
            op=self.op,
            domain=self.domain,
            state=state,
            intent_id=self.intent_id,
            detail=detail or None,
        )

    def _backup_tag(self) -> str:
        return f"{self.op}:{self.domain}"

    def _backup(self) -> Path:
        if self.backup_dir is None:
            raise LifecycleError("backup directory is not configured")
        return create_backup(
            self.edge,
            self.sidecar.path,
            self.backup_dir,
            self._backup_tag(),
        )

    # -- steps (each one is idempotent) --------------------------------

    def _step_backed_up(self) -> None:
        path = self._backup()
        self._transition("BACKED_UP", f"backup {path.name}")

    def _step_snapshotted(self) -> None:
        snapshot = _collect_snapshot(self.edge, self.domain, self.tableset)
        self.snapshot = snapshot
        detail = _snapshot_detail(snapshot)
        # Snapshot row + intent transition + audit row: ONE sidecar txn.
        with self.sidecar.transaction() as conn:
            self.sidecar.save_snapshot(
                self.domain,
                snapshot,
                tenantid=str(snapshot.get("tenantid") or ""),
                intent_id=self.intent_id,
                conn=conn,
            )
            self.sidecar.record_transition(
                self.intent_id, "SNAPSHOTTED", detail=detail, conn=conn
            )

    def _step_rows_deleted(self) -> None:
        from . import db as db_module

        snapshot = self._load_snapshot()
        # A crash after SNAPSHOTTED leaves the home active and editable.  Do
        # not delete the old snapshot's idea of the rows: re-collect under the
        # console write lock so an edit made after the crash is preserved in a
        # refreshed snapshot (restore must not silently lose it).
        with db_module.write_lock:
            if _tenant_row(self.edge, self.domain) is None:
                # A prior attempt committed the edge delete but lost the
                # sidecar transition.  Do not re-derive a scope from a
                # registry row that a new home may already have reused.
                counts = {
                    "tenant_registry": 0,
                    "tenant_landline": 0,
                    "dispatcher": 0,
                    "trusted": 0,
                }
            else:
                current = _collect_snapshot(self.edge, self.domain, self.tableset)
                # Identity changes would widen the destructive scope (a new
                # tenantid/set id can point at another home's rows).  Refuse;
                # benign edits (label/target/new landline) refresh instead.
                if (
                    current.get("tenantid") != snapshot.get("tenantid")
                    or current.get("ds_setids") != snapshot.get("ds_setids")
                ):
                    raise LifecycleError(
                        "home identity (tenantid or dispatcher set ids) "
                        "changed after the snapshot; refusing to widen the "
                        "park/destroy scope"
                    )
                if current != snapshot:
                    with self.sidecar.transaction() as conn:
                        self.sidecar.save_snapshot(
                            self.domain,
                            current,
                            tenantid=str(current.get("tenantid") or ""),
                            intent_id=self.intent_id,
                            conn=conn,
                        )
                    snapshot = current
                    self.snapshot = current
                with _edge_transaction(self.edge) as conn:
                    shared = _other_home_set_references(
                        conn,
                        self.domain,
                        snapshot.get("ds_setids") or [],
                        self.tableset,
                    )
                    if shared:
                        raise LifecycleError(
                            "dispatcher set(s) also used by "
                            + ", ".join(shared)
                            + "; refusing to delete shared routing"
                        )
                    counts = _delete_edge_rows(conn, snapshot)
        self._transition(
            "ROWS_DELETED",
            "deleted: "
            + ", ".join(f"{table}={count}" for table, count in counts.items()),
        )

    def _step_reloaded(self) -> None:
        tables = _reload_tables(
            self.sidecar,
            self.worker,
            self.tableset,
            timeout=self.reload_timeout,
        )
        self._transition("RELOADED", "reloaded: " + ", ".join(tables))

    def _step_purged(self) -> None:
        deleted = purge_regcache(
            self.worker.rpc,
            self.domain,
            max_attempts=self.purge_max_attempts,
        )
        self._transition(
            "PURGED", f"regcache purge: {len(deleted)} key(s) deleted"
        )

    def _step_collision_checked(self) -> None:
        snapshot = self._load_snapshot()
        _check_collisions(self.edge, snapshot, self.tableset)
        self._transition("COLLISION_CHECKED", "no restore collisions")

    def _step_rows_inserted(self) -> None:
        from . import db as db_module

        snapshot = self._load_snapshot()
        with db_module.write_lock:
            with _edge_transaction(self.edge) as conn:
                already = conn.execute(
                    "SELECT 1 FROM tenant_registry WHERE tprefix = ?",
                    (str(snapshot.get("domain") or ""),),
                ).fetchone()
                if already is None:
                    # Re-check collisions in the write transaction: a crash
                    # after COLLISION_CHECKED can resume into this step after
                    # another home took the tenantid/set/prefix.
                    _check_collisions(
                        self.edge, snapshot, self.tableset, conn=conn
                    )
                counts = _insert_edge_rows(conn, snapshot)
                # Verify presence, not "newly inserted": a crash after the
                # edge COMMIT but before the sidecar ROWS_INSERTED transition
                # resumes here with every row already present and must succeed.
                missing = _missing_rows(conn, snapshot)
                if missing:
                    raise LifecycleError(
                        "restore rows are missing or differ from the snapshot "
                        "(concurrent write?): "
                        + ", ".join(
                            f"{table}={count}" for table, count in missing.items()
                        )
                    )
        self._transition(
            "ROWS_INSERTED",
            "verified: "
            + ", ".join(f"{table}+{count}" for table, count in counts.items()),
        )

    def _step_snapshot_discarded(self) -> None:
        removed = self.sidecar.delete_snapshot(self.domain)
        self._transition(
            "SNAPSHOT_DISCARDED",
            "snapshot discarded" if removed else "snapshot already absent",
        )

    def _step_done(self) -> None:
        self._transition("DONE", f"{self.op} complete")

    # -- driver --------------------------------------------------------

    def advance(self, fail_after: str | None = None) -> None:
        intent = self.sidecar.get_intent(self.intent_id)
        if intent is None:
            raise LifecycleError(f"unknown intent {self.intent_id!r}")
        current = str(intent["state"])
        try:
            start = self.sequence.index(current)
        except ValueError as exc:
            raise LifecycleError(
                f"intent {self.intent_id} has state {current!r} "
                f"outside the {self.op} sequence"
            ) from exc

        # Crash exactly *between* the committed state and its successor,
        # including between STARTED and BACKED_UP.
        if fail_after is not None and current == fail_after and current != "DONE":
            raise InjectedCrash(f"injected failure after {current}")

        for state in self.sequence[start + 1 :]:
            step = getattr(self, f"_step_{state.lower()}")
            step()
            if fail_after is not None and state == fail_after and state != "DONE":
                raise InjectedCrash(f"injected failure after {state}")


def _record_failure(run: _Run, exc: BaseException) -> None:
    try:
        run.sidecar.fail_intent(run.intent_id, str(exc))
    except (SidecarUnavailable, KeyError):
        pass
    except Exception:  # never mask the original lifecycle error
        pass


def _finish(run: _Run, fail_after: str | None) -> LifecycleResult:
    try:
        run.advance(fail_after)
    except BaseException as exc:
        _record_failure(run, exc)
        raise
    intent = run.sidecar.get_intent(run.intent_id) or {}
    return LifecycleResult(
        intent_id=run.intent_id,
        op=run.op,
        domain=run.domain,
        state=str(intent.get("state", "DONE")),
        done=True,
    )


def _ensure_no_pending(sc: Sidecar, op: str, domain: str) -> None:
    """Refuse a second intent for the domain while any state machine runs."""
    pending = sc.latest_intent_any(domain, include_done=False)
    if pending is not None:
        raise LifecycleError(
            f"{op} for {domain!r} already has intent {pending['id']} "
            f"at state {pending['state']}; resume it instead"
        )


def _require_purge_capability(worker: Any) -> None:
    """Park/destroy purge regcache; refuse unless htable.delete is proven.

    ``CONSOLE_ENFORCE_CAPABILITIES=1`` is fail-closed: an unreachable edge, a
    failed probe, or a missing RPC client all refuse the operation *before*
    any backup, snapshot or row deletion.
    """
    from . import settings as settings_module

    if not settings_module.settings.enforce_capabilities:
        return
    from . import capabilities
    from .errors import KamailioUnavailable

    rpc = getattr(worker, "rpc", None)
    if rpc is None or not hasattr(rpc, "call"):
        raise KamailioUnavailable(
            "cannot prove htable.delete (no RPC client) — park/destroy refused"
        )
    try:
        # Destructive operation: never trust a cached success from up to a
        # minute ago.  A fresh probe is mandatory when enforcement is on.
        caps = capabilities.probe(rpc, ttl=0)
    except BaseException as exc:
        raise KamailioUnavailable(
            f"cannot prove htable.delete (probe failed: {exc}) — park/destroy refused"
        ) from exc
    # The purge is dump -> delete -> dump-verify; both primitives must be
    # proven.  Raises when either is False, unknown, unreachable or absent.
    for capability in ("htable.dump", "htable.delete"):
        capabilities.require_capability(capability, caps)


def _sequence_for(op: str, mode: str) -> tuple[str, ...]:
    if op == "park":
        return PARK_STATES
    if op == "restore":
        return RESTORE_STATES
    if op == "destroy":
        if mode == "parked":
            return DESTROY_PARKED_STATES
        return DESTROY_ACTIVE_STATES
    raise LifecycleError(f"unknown lifecycle op {op!r}")


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------
_OP_LOCK = threading.Lock()


def park(
    *args, **kwargs
) -> LifecycleResult:
    with _OP_LOCK:
        return _park(*args, **kwargs)


def _park(
    sidecar: Sidecar | str | Path,
    edge: str | Path,
    worker: Any,
    domain: str,
    *,
    backup_dir: str | Path,
    tableset: TableSet | None = None,
    purge_max_attempts: int = 3,
    reload_timeout: float = 30.0,
    fail_after: str | None = None,
) -> LifecycleResult:
    """Park an active home (snapshot, delete, reload, purge)."""
    sc = open_sidecar(sidecar)  # SidecarUnavailable before any edge mutation
    _require_purge_capability(worker)
    edge_path = _edge_path(edge)
    tables = tableset or descriptors.load()

    _ensure_no_pending(sc, "park", domain)
    # An active home may still carry the snapshot from an earlier park; the
    # SNAPSHOTTED step upserts it.  "Parked" is defined by the absence of the
    # registry row, not by snapshot existence (restore deliberately keeps the
    # snapshot as a recovery copy).
    _require_tenant(edge_path, domain)

    intent_id = sc.create_intent("park", domain, detail=f"park {domain}")
    run = _Run(
        sidecar=sc,
        intent_id=intent_id,
        op="park",
        domain=domain,
        sequence=PARK_STATES,
        edge=edge_path,
        worker=worker,
        backup_dir=backup_dir,
        tableset=tables,
        purge_max_attempts=purge_max_attempts,
        reload_timeout=reload_timeout,
    )
    return _finish(run, fail_after)


def restore(
    *args, **kwargs
) -> LifecycleResult:
    with _OP_LOCK:
        return _restore(*args, **kwargs)


def _restore(
    sidecar: Sidecar | str | Path,
    edge: str | Path,
    worker: Any,
    domain: str,
    *,
    backup_dir: str | Path,
    tableset: TableSet | None = None,
    reload_timeout: float = 30.0,
    fail_after: str | None = None,
) -> LifecycleResult:
    """Restore a parked home from its sidecar snapshot."""
    sc = open_sidecar(sidecar)
    edge_path = _edge_path(edge)
    tables = tableset or descriptors.load()

    _ensure_no_pending(sc, "restore", domain)
    snapshot = sc.load_snapshot(domain)
    if snapshot is None:
        raise LifecycleError(f"no parked snapshot for {domain!r}")

    # Pre-flight the read-only collision classes before creating an intent:
    # a blocked restore must not leave a non-DONE intent that refuses later
    # lifecycle operations.  The state machine re-checks before writing.
    _check_collisions(edge_path, snapshot, tables)

    intent_id = sc.create_intent("restore", domain, detail=f"restore {domain}")
    run = _Run(
        sidecar=sc,
        intent_id=intent_id,
        op="restore",
        domain=domain,
        sequence=RESTORE_STATES,
        edge=edge_path,
        worker=worker,
        backup_dir=backup_dir,
        tableset=tables,
        reload_timeout=reload_timeout,
    )
    return _finish(run, fail_after)


def destroy(
    *args, **kwargs
) -> LifecycleResult:
    with _OP_LOCK:
        return _destroy(*args, **kwargs)


def _destroy(
    sidecar: Sidecar | str | Path,
    edge: str | Path,
    worker: Any,
    domain: str,
    *,
    confirm: str = "",
    backup_dir: str | Path | None = None,
    tableset: TableSet | None = None,
    purge_max_attempts: int = 3,
    reload_timeout: float = 30.0,
    fail_after: str | None = None,
) -> LifecycleResult:
    """Destroy an active home (typed confirm) or a parked snapshot."""
    sc = open_sidecar(sidecar)
    edge_path = Path(edge)
    tables = tableset or descriptors.load()

    _ensure_no_pending(sc, "destroy", domain)
    active = False
    if edge_path.is_file():
        edge_path = _edge_path(edge)
        active = _tenant_row(edge_path, domain) is not None
    parked = sc.get_parked(domain) is not None
    if not active and not parked:
        raise LifecycleError(f"home {domain!r} has no sidecar snapshot")
    if not active and parked and not _snapshot_is_parked(sc, domain):
        raise LifecycleError(
            "no active registry row and the newest completed intent was not "
            "a park; refusing to delete the retained snapshot"
        )
    if not active and not parked:
        raise LifecycleError(f"home {domain!r} is neither active nor parked")

    mode = "active" if active else "parked"
    if mode == "active":
        _require_purge_capability(worker)
    if mode == "active" and confirm != domain:
        raise ValidationError(
            {"confirm": f"type {domain} exactly"},
            summary=f"Type the full domain {domain} to destroy this active home",
        )

    intent_id = sc.create_intent(
        "destroy",
        domain,
        mode=mode,
        detail=f"destroy {mode} {domain}",
    )
    run = _Run(
        sidecar=sc,
        intent_id=intent_id,
        op="destroy",
        domain=domain,
        sequence=_sequence_for("destroy", mode),
        edge=edge_path,
        worker=worker,
        backup_dir=backup_dir,
        tableset=tables,
        purge_max_attempts=purge_max_attempts,
        reload_timeout=reload_timeout,
    )
    return _finish(run, fail_after)


def resume_pending(*args, **kwargs):
    with _OP_LOCK:
        return _resume_pending(*args, **kwargs)


def _resume_pending(
    sidecar: Sidecar | str | Path,
    edge: str | Path,
    worker: Any,
    *,
    backup_dir: str | Path | None = None,
    tableset: TableSet | None = None,
    purge_max_attempts: int = 3,
    reload_timeout: float = 30.0,
    fail_after: str | None = None,
) -> dict[str, Any]:
    """Resume every non-DONE intent from its last committed state.

    Returns ``{"attempted", "completed", "failed", "results"}``.  A failed
    step records ``last_error`` and leaves the state unchanged; other intents
    are still attempted.
    """
    sc = open_sidecar(sidecar)
    edge_input = Path(edge)
    tables = tableset or descriptors.load()

    intents = sc.list_intents(include_done=False)
    results: list[LifecycleResult] = []
    for intent in intents:
        intent_id = str(intent["id"])
        op = str(intent["op"])
        domain = str(intent["domain"])
        mode = str(intent.get("mode") or "")
        run: _Run | None = None
        try:
            sequence = _sequence_for(op, mode)
            needs_edge = op in {"park", "restore"} or (
                op == "destroy" and mode != "parked"
            )
            edge_path = _edge_path(edge_input) if needs_edge else edge_input
            run = _Run(
                sidecar=sc,
                intent_id=intent_id,
                op=op,
                domain=domain,
                sequence=sequence,
                edge=edge_path,
                worker=worker,
                backup_dir=backup_dir,
                tableset=tables,
                purge_max_attempts=purge_max_attempts,
                reload_timeout=reload_timeout,
            )
            # A resumed park/active-destroy still deletes rows and purges;
            # enforce the fail-closed gate before advancing.  States past the
            # destructive work only have DONE/SNAPSHOT_DISCARDED left.
            state = str(intent.get("state") or "")
            if (
                (op == "park" or (op == "destroy" and mode != "parked"))
                and state not in {"PURGED", "SNAPSHOT_DISCARDED"}
            ):
                _require_purge_capability(worker)
            run.advance(fail_after)
        except BaseException as exc:  # one broken intent never blocks the rest
            if run is not None:
                _record_failure(run, exc)
            else:
                try:
                    sc.fail_intent(intent_id, str(exc))
                except (SidecarUnavailable, KeyError):
                    pass
                except Exception:
                    pass
            current = sc.get_intent(intent_id) or {}
            results.append(
                LifecycleResult(
                    intent_id=intent_id,
                    op=op,
                    domain=domain,
                    state=str(current.get("state", intent.get("state", ""))),
                    done=False,
                    error=str(exc),
                )
            )
            continue

        current = sc.get_intent(intent_id) or {}
        results.append(
            LifecycleResult(
                intent_id=intent_id,
                op=op,
                domain=domain,
                state=str(current.get("state", "DONE")),
                done=True,
            )
        )

    completed = sum(1 for result in results if result.done)
    return {
        "attempted": len(results),
        "completed": completed,
        "failed": len(results) - completed,
        "results": results,
    }
