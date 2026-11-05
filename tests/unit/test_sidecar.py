"""Unit tests for console.sidecar — schema v1, intents, snapshots, audit."""
from __future__ import annotations

import sqlite3

import pytest

from console.errors import SidecarUnavailable
from console.sidecar import SCHEMA_VERSION, Sidecar
from tests.fixtures.edgedb import DOMAIN_A


def test_schema_is_created_idempotently_with_user_version(tmp_path):
    path = tmp_path / "console.sqlite"

    first = Sidecar(path)
    assert first.schema_version() == SCHEMA_VERSION
    assert {
        "console_audit",
        "console_parked",
        "console_intent",
        "console_pending_reload",
    } <= first.table_names()

    # Opening (and therefore migrating) twice is a no-op, not an error.
    second = Sidecar(path)
    assert second.schema_version() == SCHEMA_VERSION

    with sqlite3.connect(path) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == 1


def test_intent_transitions_append_audit_rows(tmp_path):
    sc = Sidecar(tmp_path / "console.sqlite")
    intent_id = sc.create_intent("park", DOMAIN_A, detail="park started")

    sc.record_transition(intent_id, "BACKED_UP", detail="backup x.sqlite")
    sc.record_transition(intent_id, "SNAPSHOTTED", detail="snapshot: 1 row")

    intent = sc.get_intent(intent_id)
    assert intent is not None
    assert intent["state"] == "SNAPSHOTTED"
    assert intent["op"] == "park"
    assert intent["domain"] == DOMAIN_A
    assert intent["last_error"] is None

    states = [row["state"] for row in sc.audit_rows(domain=DOMAIN_A)]
    assert states == ["STARTED", "BACKED_UP", "SNAPSHOTTED"]
    rows = sc.audit_rows(domain=DOMAIN_A)
    assert all(row["ts"] for row in rows)
    assert rows[1]["detail"] == "backup x.sqlite"


def test_failed_step_records_last_error_without_changing_state(tmp_path):
    sc = Sidecar(tmp_path / "console.sqlite")
    intent_id = sc.create_intent("park", DOMAIN_A)
    sc.record_transition(intent_id, "BACKED_UP")

    sc.fail_intent(intent_id, "kamailio down")

    intent = sc.get_intent(intent_id)
    assert intent["state"] == "BACKED_UP"
    assert intent["last_error"] == "kamailio down"
    # Failures are not transitions: the audit stays one row per state.
    assert [row["state"] for row in sc.audit_rows(domain=DOMAIN_A)] == [
        "STARTED",
        "BACKED_UP",
    ]

    # A later successful transition clears the error.
    sc.record_transition(intent_id, "SNAPSHOTTED")
    assert sc.get_intent(intent_id)["last_error"] is None


def test_snapshot_json_round_trips_through_sidecar(tmp_path):
    sc = Sidecar(tmp_path / "console.sqlite")
    intent_id = sc.create_intent("park", DOMAIN_A)
    snapshot = {
        "version": 1,
        "domain": DOMAIN_A,
        "tenantid": "T1",
        "ds_setids": [10, 30],
        "tenant_registry": [{"tprefix": DOMAIN_A, "tvalue": "T1,10,30,..."}],
        "tenant_landline": [{"tprefix": "1001", "tvalue": "T1,501,10,1"}],
        "dispatcher": [
            {
                "id": 1,
                "setid": 10,
                "destination": "sip:100.64.0.10:5061;transport=tcp",
                "flags": 0,
                "priority": 0,
                "attrs": "",
                "description": "A on-net",
            }
        ],
        "trusted": [
            {
                "id": 1,
                "src_ip": "100.96.1.10",
                "proto": "any",
                "from_pattern": "",
                "ruri_pattern": "",
                "tag": DOMAIN_A,
                "priority": 0,
            }
        ],
    }

    # Snapshot + intent bump share the caller's transaction.
    with sc.transaction() as conn:
        size = sc.save_snapshot(
            DOMAIN_A,
            snapshot,
            tenantid="T1",
            intent_id=intent_id,
            conn=conn,
        )
        sc.record_transition(intent_id, "SNAPSHOTTED", conn=conn)

    loaded = sc.load_snapshot(DOMAIN_A)
    assert loaded == snapshot
    assert loaded["tenant_registry"][0]["tprefix"] == DOMAIN_A

    parked = sc.get_parked(DOMAIN_A)
    assert parked is not None
    assert parked["tenantid"] == "T1"
    assert parked["intent_id"] == intent_id
    assert parked["size_bytes"] == size > 0
    assert sc.get_intent(intent_id)["state"] == "SNAPSHOTTED"

    listed = sc.list_parked()
    assert [row["domain"] for row in listed] == [DOMAIN_A]

    assert sc.delete_snapshot(DOMAIN_A) is True
    assert sc.load_snapshot(DOMAIN_A) is None
    assert sc.delete_snapshot(DOMAIN_A) is False


def test_snapshot_and_intent_roll_back_together(tmp_path):
    sc = Sidecar(tmp_path / "console.sqlite")
    intent_id = sc.create_intent("park", DOMAIN_A)

    class Unserializable:
        pass

    with pytest.raises(TypeError):
        with sc.transaction() as conn:
            sc.save_snapshot(
                DOMAIN_A,
                {"bad": Unserializable()},
                intent_id=intent_id,
                conn=conn,
            )
            sc.record_transition(intent_id, "SNAPSHOTTED", conn=conn)

    assert sc.load_snapshot(DOMAIN_A) is None
    assert sc.get_intent(intent_id)["state"] == "STARTED"


def test_pending_reload_intents_are_persisted_and_cleared(tmp_path):
    sc = Sidecar(tmp_path / "console.sqlite")

    sc.record_pending_reload("tenant_registry")
    sc.record_pending_reload("dispatcher", last_error="edge down")

    pending = sc.pending_reloads()
    assert [row["table"] for row in pending] == ["tenant_registry", "dispatcher"]
    assert all(row["enqueued_at"] for row in pending)
    assert pending[1]["last_error"] == "edge down"

    # Upsert, not duplicate.
    sc.record_pending_reload("tenant_registry", last_error="still down")
    pending = {row["table"]: row for row in sc.pending_reloads()}
    assert len(pending) == 2
    assert pending["tenant_registry"]["last_error"] == "still down"

    assert sc.clear_pending_reload("tenant_registry") is True
    assert [row["table"] for row in sc.pending_reloads()] == ["dispatcher"]
    assert sc.clear_pending_reload("tenant_registry") is False


def test_corrupt_sidecar_raises_sidecar_unavailable(tmp_path):
    path = tmp_path / "console.sqlite"
    path.write_bytes(b"this is definitely not a sqlite database\n" * 64)

    with pytest.raises(SidecarUnavailable):
        Sidecar(path)


def test_absent_sidecar_directory_raises_sidecar_unavailable(tmp_path):
    missing = tmp_path / "never-created" / "console.sqlite"

    with pytest.raises(SidecarUnavailable):
        Sidecar(missing)


def test_parked_snapshot_corruption_is_reported(tmp_path):
    sc = Sidecar(tmp_path / "console.sqlite")
    sc.save_snapshot(DOMAIN_A, {"version": 1, "domain": DOMAIN_A})
    with sqlite3.connect(sc.path) as conn:
        conn.execute(
            "UPDATE console_parked SET snapshot = ? WHERE domain = ?",
            ("{not json", DOMAIN_A),
        )

    with pytest.raises(SidecarUnavailable):
        sc.load_snapshot(DOMAIN_A)
