"""backups.py — online ``edge.sqlite`` backups with global keep-5 rotation.

Plan §0.4: before **every** destructive lifecycle operation an online,
WAL-safe backup is taken via the SQLite backup API (never a file copy), named
``edge-<utc>-<tag>.sqlite`` and rotation keeps the newest **5 globally**,
regardless of purpose tag.  Tags look like ``park:<domain>``,
``restore:<domain>``, ``destroy:<domain>`` and ``wizard-apply:<domain>``.

Next to every ``.sqlite`` backup a ``.parked.json`` sidecar is written with
the current ``console_parked`` rows, so a parked snapshot survives even if
``console.sqlite`` is lost (plan §12 architecture lens: "backup rotation spec
(+console_parked inside backups)").

A failure raises :class:`BackupError`; lifecycle callers take the backup
**before** any edge mutation, so a raised backup leaves the edge untouched.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from console.errors import ConsoleError

__all__ = [
    "BackupError",
    "BACKUP_GLOB",
    "KEEP_BACKUPS",
    "create_backup",
    "list_backups",
    "rotate_backups",
]

BACKUP_GLOB = "edge-*.sqlite"
KEEP_BACKUPS = 5
_SAFE_TAG = re.compile(r"[\\/\x00]")


class BackupError(ConsoleError):
    """The online backup could not be taken (or its parked dump failed)."""


def _utc_stamp(now: datetime | None) -> str:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    # Microseconds make same-second backups distinct while keeping lexical
    # order equal to chronological order.
    return moment.strftime("%Y%m%dT%H%M%S%fZ")


def _safe_tag(tag: str) -> str:
    if not tag:
        raise BackupError("backup tag must not be empty")
    cleaned = _SAFE_TAG.sub("_", str(tag)).strip()
    if not cleaned or cleaned in (".", ".."):
        raise BackupError(f"unsafe backup tag {tag!r}")
    return cleaned


def _new_backup_path(backup_dir: Path, tag: str, now: datetime | None) -> Path:
    stamp = _utc_stamp(now)
    base = f"edge-{stamp}-{tag}"
    candidate = backup_dir / f"{base}.sqlite"
    counter = 1
    while candidate.exists():
        candidate = backup_dir / f"{base}-{counter}.sqlite"
        counter += 1
    return candidate


def _connect_ro(path: Path) -> sqlite3.Connection:
    """Open *path* without ever creating it."""
    if not path.is_file():
        raise BackupError(f"database not found: {path}")
    uri = f"file:{path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 2000")
        return conn
    except sqlite3.Error as exc:
        raise BackupError(f"cannot open {path} for backup: {exc}") from exc


def _parked_rows(sidecar_db: Path) -> list[dict[str, Any]]:
    if not sidecar_db.exists():
        return []
    try:
        conn = _connect_ro(sidecar_db)
    except BackupError as exc:
        raise BackupError(f"cannot read sidecar for parked dump: {exc}") from exc
    try:
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name = 'console_parked'"
        ).fetchone()
        if table is None:
            return []
        rows = conn.execute(
            "SELECT id, domain, tenantid, snapshot, size_bytes, intent_id, "
            "created_at FROM console_parked ORDER BY created_at, domain"
        ).fetchall()
    except sqlite3.Error as exc:
        raise BackupError(
            f"cannot dump console_parked from {sidecar_db}: {exc}"
        ) from exc
    finally:
        conn.close()

    dump: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        raw = item.get("snapshot")
        try:
            item["snapshot"] = json.loads(raw) if raw is not None else None
        except (TypeError, ValueError):
            item["snapshot"] = None
        dump.append(item)
    return dump


def _write_json_atomic(target: Path, payload: Any) -> None:
    tmp = target.with_name(target.name + ".tmp")
    try:
        tmp.write_text(
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp, target)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise BackupError(f"cannot write parked dump {target}: {exc}") from exc


def create_backup(
    edge_db: str | Path,
    sidecar_db: str | Path,
    backup_dir: str | Path,
    tag: str,
    *,
    keep: int = KEEP_BACKUPS,
    now: datetime | None = None,
) -> Path:
    """Take an online backup of *edge_db* and rotate the global keep-*keep*.

    Returns the new ``.sqlite`` path.  Raises :class:`BackupError` on any
    failure; partial files are removed and existing backups are not touched.
    """
    edge_path = Path(edge_db)
    sidecar_path = Path(sidecar_db)
    directory = Path(backup_dir)
    safe_tag = _safe_tag(tag)
    if keep < 1:
        raise BackupError("keep must be at least 1")

    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BackupError(f"cannot create backup directory {directory}: {exc}") from exc

    target = _new_backup_path(directory, safe_tag, now)
    tmp = target.with_name(target.name + ".tmp")
    parked_target = Path(str(target) + ".parked.json")

    source: sqlite3.Connection | None = None
    dest: sqlite3.Connection | None = None
    try:
        source = _connect_ro(edge_path)
        dest = sqlite3.connect(str(tmp), timeout=2.0)
        source.backup(dest)  # online, WAL-safe
        dest.commit()
        dest.close()
        dest = None
        source.close()
        source = None

        parked = _parked_rows(sidecar_path)
        _write_json_atomic(parked_target, parked)

        os.replace(tmp, target)
    except BackupError:
        _cleanup(tmp, parked_target)
        raise
    except (sqlite3.Error, OSError) as exc:
        _cleanup(tmp, parked_target)
        raise BackupError(f"backup of {edge_path} failed: {exc}") from exc
    finally:
        if dest is not None:
            try:
                dest.close()
            except sqlite3.Error:
                pass
        if source is not None:
            try:
                source.close()
            except sqlite3.Error:
                pass

    rotate_backups(directory, keep=keep)
    return target


def _cleanup(*paths: Path) -> None:
    for path in paths:
        try:
            path.unlink()
        except OSError:
            pass


def list_backups(backup_dir: str | Path) -> list[Path]:
    """All backup files, newest first (mtime, then name)."""
    directory = Path(backup_dir)
    if not directory.is_dir():
        return []
    files = [path for path in directory.glob(BACKUP_GLOB) if path.is_file()]
    files.sort(key=lambda path: (path.stat().st_mtime_ns, path.name), reverse=True)
    return files


def rotate_backups(
    backup_dir: str | Path, *, keep: int = KEEP_BACKUPS
) -> list[Path]:
    """Delete backups beyond the newest *keep* (global, all tags).

    Returns the removed paths.  Each removed ``.sqlite`` takes its
    ``.parked.json`` sidecar with it.
    """
    if keep < 1:
        raise BackupError("keep must be at least 1")
    files = list_backups(backup_dir)
    removed: list[Path] = []
    for old in files[keep:]:
        _cleanup(old, Path(str(old) + ".parked.json"), Path(str(old) + ".tmp"))
        removed.append(old)
    return removed
