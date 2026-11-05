"""Unit tests for console.backups — online backup, parked dump, keep-5."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from console.backups import BackupError, create_backup, list_backups
from console.sidecar import Sidecar
from tests.fixtures.edgedb import DOMAIN_A, DOMAIN_B, make_edge_db

STAMP = re.compile(
    r"^edge-\d{8}T\d{12}Z-(?P<tag>.+)\.sqlite$"
)


def _backup(
    edge: Path,
    sidecar: Path,
    directory: Path,
    tag: str,
    *,
    position: int = 0,
    keep: int = 5,
) -> Path:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=position)
    return create_backup(edge, sidecar, directory, tag, keep=keep, now=now)


def _parked_snapshot(domain: str) -> dict:
    return {
        "version": 1,
        "domain": domain,
        "tenantid": "T1",
        "ds_setids": [10, 30],
        "tenant_registry": [{"tprefix": domain, "tvalue": "T1,10,30,1001,A,10,1+1+90"}],
        "tenant_landline": [{"tprefix": "1001", "tvalue": "T1,501,10,1"}],
        "dispatcher": [],
        "trusted": [],
    }


def test_online_backup_contains_committed_wal_rows(tmp_path):
    edge = make_edge_db(tmp_path / "edge.sqlite")
    sidecar_path = tmp_path / "console.sqlite"
    sidecar = Sidecar(sidecar_path)

    # A committed write in WAL mode must be visible through the backup API.
    with sqlite3.connect(edge) as conn:
        conn.execute(
            "INSERT INTO tenant_registry (tprefix, tvalue) VALUES (?, ?)",
            ("wal-probe.home.example.net", "T9,10,0,9999,WAL,10,0+0"),
        )

    backup = _backup(edge, sidecar.path, tmp_path / "backups", f"park:{DOMAIN_A}")

    match = STAMP.match(backup.name)
    assert match, backup.name
    assert match.group("tag") == f"park:{DOMAIN_A}"

    with sqlite3.connect(backup) as conn:
        prefixes = {
            row[0] for row in conn.execute("SELECT tprefix FROM tenant_registry")
        }
        assert "wal-probe.home.example.net" in prefixes
        assert DOMAIN_A in prefixes
        versions = dict(conn.execute("SELECT table_name, table_version FROM version"))
        assert versions == {"dispatcher": 4, "dialplan": 2, "trusted": 6}


def test_parked_rows_are_dumped_next_to_each_backup(tmp_path):
    edge = make_edge_db(tmp_path / "edge.sqlite")
    sidecar = Sidecar(tmp_path / "console.sqlite")
    sidecar.save_snapshot(DOMAIN_A, _parked_snapshot(DOMAIN_A), tenantid="T1")
    sidecar.save_snapshot(DOMAIN_B, _parked_snapshot(DOMAIN_B), tenantid="T2")

    backup = _backup(edge, sidecar.path, tmp_path / "backups", f"park:{DOMAIN_A}")

    parked_file = Path(str(backup) + ".parked.json")
    assert parked_file.is_file()
    rows = json.loads(parked_file.read_text(encoding="utf-8"))
    assert [row["domain"] for row in rows] == [DOMAIN_A, DOMAIN_B]
    assert rows[0]["snapshot"]["tenantid"] == "T1"
    assert rows[0]["snapshot"]["tenant_registry"][0]["tprefix"] == DOMAIN_A


def test_rotation_keeps_five_globally_across_tags(tmp_path):
    edge = make_edge_db(tmp_path / "edge.sqlite")
    sidecar = Sidecar(tmp_path / "console.sqlite")
    directory = tmp_path / "backups"

    tags = [
        f"park:{DOMAIN_A}",
        f"restore:{DOMAIN_A}",
        f"destroy:{DOMAIN_A}",
        f"wizard-apply:{DOMAIN_A}",
        f"park:{DOMAIN_B}",
        f"restore:{DOMAIN_B}",
        f"destroy:{DOMAIN_B}",
    ]
    created = [
        _backup(edge, sidecar.path, directory, tag, position=index)
        for index, tag in enumerate(tags)
    ]

    remaining = list_backups(directory)
    assert len(remaining) == 5
    # Newest five survive; the first two (and their parked dumps) are gone.
    assert {path.name for path in created[2:]} == {path.name for path in remaining}
    for path in created[:2]:
        assert not path.exists()
        assert not Path(str(path) + ".parked.json").exists()


def test_rotation_still_keeps_five_after_a_failed_backup(tmp_path):
    edge = make_edge_db(tmp_path / "edge.sqlite")
    sidecar = Sidecar(tmp_path / "console.sqlite")
    directory = tmp_path / "backups"

    for index in range(5):
        _backup(edge, sidecar.path, directory, f"park:{DOMAIN_A}", position=index)
    before = {path.name for path in list_backups(directory)}

    blocked = tmp_path / "not-a-directory"
    blocked.write_text("x", encoding="utf-8")
    with pytest.raises(BackupError):
        create_backup(edge, sidecar.path, blocked, f"park:{DOMAIN_A}")

    # The failed attempt must not disturb the existing rotation set.
    assert {path.name for path in list_backups(directory)} == before

    _backup(edge, sidecar.path, directory, f"destroy:{DOMAIN_B}", position=10)
    remaining = list_backups(directory)
    assert len(remaining) == 5
    assert remaining[0].name.endswith(f"destroy:{DOMAIN_B}.sqlite")


def test_backup_failure_raises_and_leaves_no_partial_file(tmp_path):
    directory = tmp_path / "backups"
    sidecar = Sidecar(tmp_path / "console.sqlite")

    with pytest.raises(BackupError):
        create_backup(
            tmp_path / "missing-edge.sqlite",
            sidecar.path,
            directory,
            f"park:{DOMAIN_A}",
        )

    assert list_backups(directory) == []
    assert list(directory.glob("*.tmp")) == []


def test_corrupt_sidecar_fails_the_backup(tmp_path):
    edge = make_edge_db(tmp_path / "edge.sqlite")
    sidecar_path = tmp_path / "console.sqlite"
    sidecar_path.write_bytes(b"not a database" * 32)

    with pytest.raises(BackupError):
        create_backup(edge, sidecar_path, tmp_path / "backups", "park:x")

    assert list_backups(tmp_path / "backups") == []
