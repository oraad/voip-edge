"""Unit tests for console.lifecycle — park/restore/destroy + durable resume.

The fault-injection convention here is ``fail_after=<state>``: the orchestrator
commits that state (including its audit row) and only then raises, exactly like
a process crash between two steps.  ``resume_pending`` must finish the intent
from the recorded state without repeating earlier deletions or reload RPCs.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from console import lifecycle
from console.backups import BackupError, list_backups
from console.errors import SidecarUnavailable, ValidationError
from console.lifecycle import (
    DESTROY_ACTIVE_STATES,
    DESTROY_PARKED_STATES,
    PARK_STATES,
    RELOAD_TABLES,
    RESTORE_STATES,
    CollisionError,
    InjectedCrash,
    LifecycleError,
    PurgeError,
    destroy,
    park,
    purge_regcache,
    restore,
    resume_pending,
)
from console.reloads import ReloadWorker
from console.sidecar import Sidecar
from tests.fixtures import edgedb
from tests.fixtures.edgedb import (
    DOMAIN_A,
    DOMAIN_B,
    LANDLINE_A,
    LANDLINE_B,
    TENANT_A,
    SRC_IP_A,
    SRC_IP_B,
)
from tests.fixtures.fakerpc import SILENT, FakeRPC

RELOAD_METHODS = ("mtree.reload", "dispatcher.reload", "dialplan.reload", "permissions.trustedReload")
# tenant_registry + tenant_landline both reload the mtree tree named by the
# table key; dispatcher and trusted get one call each; dialplan is untouched.
EXPECTED_RELOAD_COUNTS = {
    "mtree.reload": 2,
    "dispatcher.reload": 1,
    "dialplan.reload": 0,
    "permissions.trustedReload": 1,
}


def reload_counts(fake: FakeRPC) -> dict[str, int]:
    return {method: len(fake.calls_for(method)) for method in RELOAD_METHODS}


@dataclass
class Env:
    edge: Path
    sidecar_path: Path
    sidecar: Sidecar
    backups: Path
    fake: FakeRPC
    worker: ReloadWorker


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def script_ok(fake: FakeRPC) -> None:
    for method in (
        "mtree.reload",
        "dispatcher.reload",
        "dialplan.reload",
        "permissions.trustedReload",
    ):
        fake.script(method, 1)
    fake.script("htable.dump", [])
    fake.script("htable.delete", 1)


def reset_fake(fake: FakeRPC) -> None:
    fake.clear()
    script_ok(fake)


def buckets(keys) -> list[dict]:
    return [
        {
            "entry": index,
            "size": 1,
            "slot": {"item": {"name": key, "value": "flow", "expires": None}},
        }
        for index, key in enumerate(keys)
    ]


def count(edge: Path, sql: str, params: tuple = ()) -> int:
    with sqlite3.connect(edge) as conn:
        return int(conn.execute(sql, params).fetchone()[0])


def edge_signature(edge: Path) -> dict[str, list[tuple]]:
    signature: dict[str, list[tuple]] = {}
    with sqlite3.connect(edge) as conn:
        for table in (
            "tenant_registry",
            "tenant_landline",
            "dispatcher",
            "dialplan",
            "trusted",
        ):
            signature[table] = sorted(
                tuple(row) for row in conn.execute(f'SELECT * FROM "{table}"')
            )
    return signature


def audit_states(env: Env, op: str, domain: str = DOMAIN_A) -> list[str]:
    return [row["state"] for row in env.sidecar.audit_rows(op=op, domain=domain)]


@pytest.fixture()
def env(tmp_path):
    edge = edgedb.make_edge_db(tmp_path / "edge.sqlite")
    sidecar = Sidecar(tmp_path / "console.sqlite")
    with FakeRPC(tmp_path / "rpc.sock", timeout_ms=150) as fake:
        script_ok(fake)
        worker = ReloadWorker(fake, {})
        try:
            yield Env(
                edge=edge,
                sidecar_path=sidecar.path,
                sidecar=sidecar,
                backups=tmp_path / "backups",
                fake=fake,
                worker=worker,
            )
        finally:
            worker.shutdown(0.5)


# ---------------------------------------------------------------------------
# 1. snapshot completeness + transactional removal
# ---------------------------------------------------------------------------
def test_park_snapshots_all_four_groups_and_removes_only_that_home(env):
    result = park(
        env.sidecar, env.edge, env.worker, DOMAIN_A, backup_dir=env.backups
    )

    assert result.done is True
    assert result.state == "DONE"

    snapshot = env.sidecar.load_snapshot(DOMAIN_A)
    assert snapshot is not None
    assert snapshot["tenantid"] == "T1"
    assert [row["tprefix"] for row in snapshot["tenant_registry"]] == [DOMAIN_A]
    assert [row["tprefix"] for row in snapshot["tenant_landline"]] == [LANDLINE_A]
    assert {row["setid"] for row in snapshot["dispatcher"]} == {10, 30}
    assert {row["src_ip"] for row in snapshot["trusted"]} == {SRC_IP_A}

    # A's rows are gone; B's and the shared dialplan partitions survive.
    assert count(env.edge, "SELECT count(*) FROM tenant_registry WHERE tprefix=?", (DOMAIN_A,)) == 0
    assert count(env.edge, "SELECT count(*) FROM tenant_registry WHERE tprefix=?", (DOMAIN_B,)) == 1
    assert count(env.edge, "SELECT count(*) FROM tenant_landline WHERE tprefix=?", (LANDLINE_A,)) == 0
    assert count(env.edge, "SELECT count(*) FROM tenant_landline WHERE tprefix=?", (LANDLINE_B,)) == 1
    assert count(env.edge, "SELECT count(*) FROM dispatcher WHERE setid IN (10,30)") == 0
    assert count(env.edge, "SELECT count(*) FROM dispatcher WHERE setid IN (11,31)") == 2
    assert count(env.edge, "SELECT count(*) FROM trusted WHERE tag=?", (DOMAIN_A,)) == 0
    assert count(env.edge, "SELECT count(*) FROM trusted WHERE tag=?", (DOMAIN_B,)) == 1
    assert count(env.edge, "SELECT count(*) FROM dialplan WHERE dpid IN (10,90)") == 2

    assert env.sidecar.latest_intent("park", DOMAIN_A)["state"] == "DONE"
    assert audit_states(env, "park") == list(PARK_STATES)
    assert len(list_backups(env.backups)) == 1


def test_snapshot_failure_keeps_edge_untouched_and_rolls_back_the_snapshot(
    env, monkeypatch
):
    original = Sidecar.record_transition

    def boom(self, intent_id, state, **kwargs):
        if state == "SNAPSHOTTED":
            raise RuntimeError("snapshot transaction boom")
        return original(self, intent_id, state, **kwargs)

    monkeypatch.setattr(Sidecar, "record_transition", boom)
    before = edge_signature(env.edge)

    with pytest.raises(RuntimeError):
        park(env.sidecar, env.edge, env.worker, DOMAIN_A, backup_dir=env.backups)

    # The snapshot insert happened inside the same sidecar txn and rolled back.
    assert env.sidecar.load_snapshot(DOMAIN_A) is None
    intent = env.sidecar.latest_intent("park", DOMAIN_A)
    assert intent["state"] == "BACKED_UP"
    assert "snapshot transaction boom" in intent["last_error"]
    assert edge_signature(env.edge) == before


def test_delete_transaction_failure_rolls_back_every_edge_delete(env, monkeypatch):
    def partial_delete(conn, snapshot):
        # Prove the transaction is live before failing...
        conn.execute(
            "DELETE FROM tenant_registry WHERE tprefix = ?", (snapshot["domain"],)
        )
        raise RuntimeError("delete transaction boom")

    monkeypatch.setattr(lifecycle, "_delete_edge_rows", partial_delete)
    before = edge_signature(env.edge)

    with pytest.raises(RuntimeError):
        park(env.sidecar, env.edge, env.worker, DOMAIN_A, backup_dir=env.backups)

    intent = env.sidecar.latest_intent("park", DOMAIN_A)
    assert intent["state"] == "SNAPSHOTTED"
    assert "delete transaction boom" in intent["last_error"]
    assert edge_signature(env.edge) == before


def test_backup_failure_happens_before_any_edge_mutation(env, tmp_path):
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    before = edge_signature(env.edge)

    with pytest.raises(BackupError):
        park(env.sidecar, env.edge, env.worker, DOMAIN_A, backup_dir=blocked)

    assert edge_signature(env.edge) == before
    intent = env.sidecar.latest_intent("park", DOMAIN_A)
    assert intent["state"] == "STARTED"
    assert intent["last_error"]


# ---------------------------------------------------------------------------
# 2. ordering: reload before purge + domain-scoped purge
# ---------------------------------------------------------------------------
def test_reload_runs_before_purge_and_purge_only_deletes_own_domain(env):
    live = {f"1001@{DOMAIN_A}", f"2002@{DOMAIN_B}"}

    def dump(params=None):
        return buckets(sorted(live))

    def delete(params=None):
        live.discard(params[1])
        return 1

    env.fake.script("htable.dump", dump)
    env.fake.script("htable.delete", delete)

    park(env.sidecar, env.edge, env.worker, DOMAIN_A, backup_dir=env.backups)

    methods = env.fake.methods()
    last_reload = max(
        index for index, method in enumerate(methods) if method in RELOAD_METHODS
    )
    first_dump = min(
        index for index, method in enumerate(methods) if method == "htable.dump"
    )
    assert last_reload < first_dump, methods

    assert reload_counts(env.fake) == EXPECTED_RELOAD_COUNTS
    # mtree.reload covers both packed tables.
    assert [call.params for call in env.fake.calls_for("mtree.reload")] == [
        ["tenantdom"],
        ["landline"],
    ]

    deleted = env.fake.calls_for("htable.delete")
    assert [call.params for call in deleted] == [
        ["regcache", f"1001@{DOMAIN_A}"]
    ]
    assert live == {f"2002@{DOMAIN_B}"}
    # dump → delete → dump-verify zero.
    assert len(env.fake.calls_for("htable.dump")) == 2


def test_purge_verify_zero_gate_retries_then_errors(env):
    env.fake.script("htable.dump", buckets([f"stuck@{DOMAIN_A}"]))
    env.fake.script("htable.delete", 1)

    with pytest.raises(PurgeError) as excinfo:
        purge_regcache(env.fake, DOMAIN_A, max_attempts=3)

    assert "after 3 attempt" in str(excinfo.value)
    assert len(env.fake.calls_for("htable.delete")) == 3
    assert len(env.fake.calls_for("htable.dump")) == 4


def test_park_purge_failure_leaves_intent_at_reloaded(env):
    env.fake.script("htable.dump", buckets([f"stuck@{DOMAIN_A}"]))

    with pytest.raises(PurgeError):
        park(env.sidecar, env.edge, env.worker, DOMAIN_A, backup_dir=env.backups)

    intent = env.sidecar.latest_intent("park", DOMAIN_A)
    assert intent["state"] == "RELOADED"
    assert "regcache purge" in intent["last_error"]
    # The home is already removed; resume will retry only the purge.
    assert count(
        env.edge, "SELECT count(*) FROM tenant_registry WHERE tprefix=?", (DOMAIN_A,)
    ) == 0


# ---------------------------------------------------------------------------
# 3. resume between EVERY consecutive state pair
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("fail_after", PARK_STATES[:-1])
def test_resume_park_between_every_state_pair(env, fail_after):
    with pytest.raises(InjectedCrash):
        park(
            env.sidecar,
            env.edge,
            env.worker,
            DOMAIN_A,
            backup_dir=env.backups,
            fail_after=fail_after,
        )

    intent = env.sidecar.latest_intent("park", DOMAIN_A)
    assert intent["state"] == fail_after
    assert intent["last_error"]

    before_reloads = {
        method: len(env.fake.calls_for(method)) for method in RELOAD_METHODS
    }
    before_dumps = len(env.fake.calls_for("htable.dump"))
    before_deletes = len(env.fake.calls_for("htable.delete"))

    summary = resume_pending(
        env.sidecar, env.edge, env.worker, backup_dir=env.backups
    )
    assert summary["attempted"] == 1
    assert summary["completed"] == 1
    assert summary["failed"] == 0
    assert env.sidecar.latest_intent("park", DOMAIN_A)["state"] == "DONE"
    assert audit_states(env, "park") == list(PARK_STATES)
    # No duplicate deletion: the home is gone exactly once.
    assert count(
        env.edge, "SELECT count(*) FROM tenant_registry WHERE tprefix=?", (DOMAIN_A,)
    ) == 0
    assert count(
        env.edge, "SELECT count(*) FROM tenant_landline WHERE tprefix=?", (LANDLINE_A,)
    ) == 0
    assert count(env.edge, "SELECT count(*) FROM trusted WHERE tag=?", (DOMAIN_A,)) == 0

    # Reload happened exactly once per table, never twice.
    assert reload_counts(env.fake) == EXPECTED_RELOAD_COUNTS
    # Already-committed purge steps are never repeated.
    if fail_after == "PURGED":
        assert len(env.fake.calls_for("htable.dump")) == before_dumps
        assert len(env.fake.calls_for("htable.delete")) == before_deletes

    # A second sweep has nothing to do.
    again = resume_pending(
        env.sidecar, env.edge, env.worker, backup_dir=env.backups
    )
    assert again["attempted"] == 0
    assert again["completed"] == 0


@pytest.mark.parametrize("fail_after", RESTORE_STATES[:-1])
def test_resume_restore_between_every_state_pair(env, fail_after):
    park(env.sidecar, env.edge, env.worker, DOMAIN_A, backup_dir=env.backups)
    reset_fake(env.fake)

    with pytest.raises(InjectedCrash):
        restore(
            env.sidecar,
            env.edge,
            env.worker,
            DOMAIN_A,
            backup_dir=env.backups,
            fail_after=fail_after,
        )

    intent = env.sidecar.latest_intent("restore", DOMAIN_A)
    assert intent["state"] == fail_after
    assert intent["last_error"]

    summary = resume_pending(
        env.sidecar, env.edge, env.worker, backup_dir=env.backups
    )
    assert summary["attempted"] == 1
    assert summary["completed"] == 1
    assert env.sidecar.latest_intent("restore", DOMAIN_A)["state"] == "DONE"
    assert audit_states(env, "restore") == list(RESTORE_STATES)

    # Rows restored exactly once and the snapshot kept for the parked list.
    assert count(
        env.edge, "SELECT count(*) FROM tenant_registry WHERE tprefix=?", (DOMAIN_A,)
    ) == 1
    assert count(
        env.edge, "SELECT count(*) FROM tenant_landline WHERE tprefix=?", (LANDLINE_A,)
    ) == 1
    assert count(env.edge, "SELECT count(*) FROM dispatcher WHERE setid IN (10,30)") == 2
    assert count(env.edge, "SELECT count(*) FROM trusted WHERE tag=?", (DOMAIN_A,)) == 1
    assert env.sidecar.load_snapshot(DOMAIN_A) is not None

    assert reload_counts(env.fake) == EXPECTED_RELOAD_COUNTS

    assert resume_pending(
        env.sidecar, env.edge, env.worker, backup_dir=env.backups
    )["attempted"] == 0


@pytest.mark.parametrize("fail_after", DESTROY_ACTIVE_STATES[:-1])
def test_resume_active_destroy_between_every_state_pair(env, fail_after):
    with pytest.raises(InjectedCrash):
        destroy(
            env.sidecar,
            env.edge,
            env.worker,
            DOMAIN_A,
            confirm=DOMAIN_A,
            backup_dir=env.backups,
            fail_after=fail_after,
        )

    intent = env.sidecar.latest_intent("destroy", DOMAIN_A)
    assert intent["mode"] == "active"
    assert intent["state"] == fail_after

    summary = resume_pending(
        env.sidecar, env.edge, env.worker, backup_dir=env.backups
    )
    assert summary["attempted"] == 1
    assert summary["completed"] == 1
    assert env.sidecar.latest_intent("destroy", DOMAIN_A)["state"] == "DONE"
    assert audit_states(env, "destroy") == list(DESTROY_ACTIVE_STATES)

    assert env.sidecar.load_snapshot(DOMAIN_A) is None
    assert count(
        env.edge, "SELECT count(*) FROM tenant_registry WHERE tprefix=?", (DOMAIN_A,)
    ) == 0
    assert count(env.edge, "SELECT count(*) FROM dialplan WHERE dpid IN (10,90)") == 2
    assert reload_counts(env.fake) == EXPECTED_RELOAD_COUNTS


@pytest.mark.parametrize("fail_after", DESTROY_PARKED_STATES[:-1])
def test_resume_parked_destroy_between_every_state_pair(env, fail_after):
    park(env.sidecar, env.edge, env.worker, DOMAIN_A, backup_dir=env.backups)
    reset_fake(env.fake)
    before_edge = edge_signature(env.edge)
    backups_before = len(list_backups(env.backups))

    with pytest.raises(InjectedCrash):
        destroy(
            env.sidecar,
            env.edge,
            env.worker,
            DOMAIN_A,
            confirm="",
            fail_after=fail_after,
        )

    intent = env.sidecar.latest_intent("destroy", DOMAIN_A)
    assert intent["mode"] == "parked"
    assert intent["state"] == fail_after

    summary = resume_pending(env.sidecar, env.edge, env.worker)
    assert summary["attempted"] == 1
    assert summary["completed"] == 1
    assert env.sidecar.latest_intent("destroy", DOMAIN_A)["state"] == "DONE"
    assert audit_states(env, "destroy") == list(DESTROY_PARKED_STATES)

    assert env.sidecar.load_snapshot(DOMAIN_A) is None
    # Parked destroy touches no edge row and takes no backup.
    assert edge_signature(env.edge) == before_edge
    assert len(list_backups(env.backups)) == backups_before
    assert env.fake.methods() == []


# ---------------------------------------------------------------------------
# 4. restore collisions (five classes) + clean restore
# ---------------------------------------------------------------------------
def _inject_collision(edge: Path, kind: str) -> None:
    with sqlite3.connect(edge) as conn:
        if kind == "domain":
            conn.execute(
                "INSERT INTO tenant_registry (tprefix, tvalue) VALUES (?, ?)",
                (DOMAIN_A, "T9,12,0,9999,Other,10,0+0"),
            )
        elif kind == "tenantid":
            conn.execute(
                "INSERT INTO tenant_registry (tprefix, tvalue) VALUES (?, ?)",
                ("c-other.home.example.net", "T1,12,0,9998,Other,10,0+0"),
            )
        elif kind == "landline":
            conn.execute(
                "INSERT INTO tenant_landline (tprefix, tvalue) VALUES (?, ?)",
                (LANDLINE_A, "T2,777,11,1"),
            )
        elif kind == "dispatcher":
            conn.execute(
                "INSERT INTO dispatcher "
                "(setid, destination, flags, priority, attrs, description) "
                "VALUES (10, 'sip:100.64.0.99:5061;transport=tcp', 0, 0, '', 'collision')"
            )
        elif kind == "trusted":
            conn.execute(
                "INSERT INTO trusted "
                "(src_ip, proto, from_pattern, ruri_pattern, tag, priority) "
                "VALUES (?, 'any', '', '', ?, 0)",
                (SRC_IP_A, DOMAIN_B),
            )
        else:  # pragma: no cover
            raise AssertionError(kind)


@pytest.mark.parametrize(
    "kind,needle",
    [
        ("domain", DOMAIN_A),
        ("tenantid", "T1"),
        ("landline", LANDLINE_A),
        ("dispatcher", "setid=10"),
        ("trusted", SRC_IP_A),
    ],
)
def test_restore_collision_classes_block_and_name_the_row(env, kind, needle):
    park(env.sidecar, env.edge, env.worker, DOMAIN_A, backup_dir=env.backups)
    _inject_collision(env.edge, kind)

    with pytest.raises(CollisionError) as excinfo:
        restore(env.sidecar, env.edge, env.worker, DOMAIN_A, backup_dir=env.backups)

    message = str(excinfo.value)
    assert kind in message
    assert needle in message
    assert excinfo.value.collisions

    # The read-only collision pre-check runs before create_intent, so a
    # blocked restore leaves no non-DONE intent behind.
    assert env.sidecar.latest_intent("restore", DOMAIN_A) is None

    # Nothing was restored for A (the injected row is the only A-domain row
    # in the one class that inserts one).
    expected = 1 if kind == "domain" else 0
    assert count(
        env.edge, "SELECT count(*) FROM tenant_registry WHERE tprefix=?", (DOMAIN_A,)
    ) == expected
    assert count(
        env.edge, "SELECT count(*) FROM tenant_landline WHERE tprefix=?", (LANDLINE_A,)
    ) == (1 if kind == "landline" else 0)
    assert count(env.edge, "SELECT count(*) FROM dispatcher WHERE setid IN (10,30)") == (
        1 if kind == "dispatcher" else 0
    )


def test_clean_restore_reinserts_all_rows_and_keeps_snapshot(env):
    park(env.sidecar, env.edge, env.worker, DOMAIN_A, backup_dir=env.backups)
    snapshot = env.sidecar.load_snapshot(DOMAIN_A)
    reset_fake(env.fake)

    result = restore(
        env.sidecar, env.edge, env.worker, DOMAIN_A, backup_dir=env.backups
    )

    assert result.done is True
    assert result.state == "DONE"
    assert env.sidecar.load_snapshot(DOMAIN_A) == snapshot
    assert count(
        env.edge, "SELECT count(*) FROM tenant_registry WHERE tprefix=?", (DOMAIN_A,)
    ) == 1
    assert count(
        env.edge, "SELECT count(*) FROM tenant_landline WHERE tprefix=?", (LANDLINE_A,)
    ) == 1
    assert count(env.edge, "SELECT count(*) FROM dispatcher WHERE setid IN (10,30)") == 2
    assert count(env.edge, "SELECT count(*) FROM trusted WHERE tag=?", (DOMAIN_A,)) == 1
    # B was never involved.
    assert count(env.edge, "SELECT count(*) FROM tenant_registry") == 2
    assert audit_states(env, "restore") == list(RESTORE_STATES)
    assert reload_counts(env.fake) == EXPECTED_RELOAD_COUNTS


# ---------------------------------------------------------------------------
# 5. destroy gates + shared dialplan survival
# ---------------------------------------------------------------------------
def test_active_destroy_requires_confirm_equal_to_domain(env):
    before = edge_signature(env.edge)

    with pytest.raises(ValidationError) as excinfo:
        destroy(
            env.sidecar,
            env.edge,
            env.worker,
            DOMAIN_A,
            confirm="beta",
            backup_dir=env.backups,
        )

    assert DOMAIN_A in str(excinfo.value)
    assert env.sidecar.list_intents(include_done=True) == []
    assert edge_signature(env.edge) == before

    result = destroy(
        env.sidecar,
        env.edge,
        env.worker,
        DOMAIN_A,
        confirm=DOMAIN_A,
        backup_dir=env.backups,
    )
    assert result.done is True
    assert count(
        env.edge, "SELECT count(*) FROM tenant_registry WHERE tprefix=?", (DOMAIN_A,)
    ) == 0
    assert env.sidecar.load_snapshot(DOMAIN_A) is None
    assert count(env.edge, "SELECT count(*) FROM dialplan WHERE dpid IN (10,90)") == 2


def test_parked_destroy_only_deletes_the_snapshot(env):
    park(env.sidecar, env.edge, env.worker, DOMAIN_A, backup_dir=env.backups)
    before_edge = edge_signature(env.edge)
    backups_before = len(list_backups(env.backups))
    reset_fake(env.fake)

    result = destroy(env.sidecar, env.edge, env.worker, DOMAIN_A, confirm="")

    assert result.done is True
    assert env.sidecar.load_snapshot(DOMAIN_A) is None
    assert edge_signature(env.edge) == before_edge
    assert len(list_backups(env.backups)) == backups_before
    assert env.fake.methods() == []
    assert audit_states(env, "destroy") == list(DESTROY_PARKED_STATES)


def test_destroy_active_keeps_shared_dialplan_rows(env):
    result = destroy(
        env.sidecar,
        env.edge,
        env.worker,
        DOMAIN_A,
        confirm=DOMAIN_A,
        backup_dir=env.backups,
    )
    assert result.done is True
    assert count(env.edge, "SELECT count(*) FROM dialplan WHERE dpid=10") == 1
    assert count(env.edge, "SELECT count(*) FROM dialplan WHERE dpid=90") == 1
    # B (and its sets/trusted/landline) is untouched.
    assert count(env.edge, "SELECT count(*) FROM tenant_registry WHERE tprefix=?", (DOMAIN_B,)) == 1
    assert count(env.edge, "SELECT count(*) FROM dispatcher WHERE setid IN (11,31)") == 2
    assert count(env.edge, "SELECT count(*) FROM trusted WHERE src_ip=?", (SRC_IP_B,)) == 1


# ---------------------------------------------------------------------------
# 6. sidecar refusal + pending reload persistence
# ---------------------------------------------------------------------------
def test_unreadable_sidecar_refuses_before_any_edge_mutation(tmp_path):
    edge = edgedb.make_edge_db(tmp_path / "edge.sqlite")
    before = edge_signature(edge)

    corrupt = tmp_path / "corrupt.sqlite"
    corrupt.write_bytes(b"garbage" * 128)
    with pytest.raises(SidecarUnavailable):
        park(corrupt, edge, object(), DOMAIN_A, backup_dir=tmp_path / "backups")
    assert edge_signature(edge) == before

    absent = tmp_path / "missing-dir" / "console.sqlite"
    with pytest.raises(SidecarUnavailable):
        park(absent, edge, object(), DOMAIN_A, backup_dir=tmp_path / "backups")
    assert edge_signature(edge) == before
    assert not (tmp_path / "backups").exists()


def test_kamailio_unavailable_persists_pending_reload_then_resume_clears_it(env):
    env.fake.script("mtree.reload", SILENT)

    with pytest.raises(LifecycleError):
        park(env.sidecar, env.edge, env.worker, DOMAIN_A, backup_dir=env.backups)

    intent = env.sidecar.latest_intent("park", DOMAIN_A)
    assert intent["state"] == "ROWS_DELETED"
    pending = {row["table"] for row in env.sidecar.pending_reloads()}
    # Packed tables persist their mtree tree name as the key (§5.6).
    assert {"tenantdom", "landline"} <= pending

    script_ok(env.fake)
    summary = resume_pending(
        env.sidecar, env.edge, env.worker, backup_dir=env.backups
    )
    assert summary["completed"] == 1
    assert env.sidecar.latest_intent("park", DOMAIN_A)["state"] == "DONE"
    assert env.sidecar.pending_reloads() == []


def test_resume_failed_step_keeps_state_then_recovers(env):
    from console import descriptors
    from console.sidecar import reissue_pending_reloads

    with pytest.raises(InjectedCrash):
        park(
            env.sidecar,
            env.edge,
            env.worker,
            DOMAIN_A,
            backup_dir=env.backups,
            fail_after="ROWS_DELETED",
        )

    env.fake.script("htable.dump", buckets([f"stuck@{DOMAIN_A}"]))
    summary = resume_pending(
        env.sidecar,
        env.edge,
        env.worker,
        backup_dir=env.backups,
        purge_max_attempts=1,
    )
    assert summary["failed"] == 1
    assert summary["completed"] == 0
    intent = env.sidecar.latest_intent("park", DOMAIN_A)
    assert intent["state"] == "RELOADED"
    assert "regcache purge" in intent["last_error"]

    env.fake.script("htable.dump", [])
    summary = resume_pending(
        env.sidecar, env.edge, env.worker, backup_dir=env.backups
    )
    assert summary["completed"] == 1
    assert env.sidecar.latest_intent("park", DOMAIN_A)["state"] == "DONE"
    # The failed purge attempt did not duplicate the reload batch.
    assert reload_counts(env.fake) == EXPECTED_RELOAD_COUNTS

    # Re-issue helper derives RPC/params from the descriptor (§5.6).
    env.sidecar.record_pending_reload("tenant_registry")
    issued = reissue_pending_reloads(
        env.sidecar, env.worker, descriptors.load()
    )
    assert issued == ["tenant_registry"]
    assert env.sidecar.pending_reloads() == []
    assert env.fake.calls_for("mtree.reload")[-1].params == ["tenantdom"]

    # A pending row keyed by the mtree tree name ("tenantdom") also resolves.
    env.sidecar.record_pending_reload("tenantdom")
    issued = reissue_pending_reloads(
        env.sidecar, env.worker, descriptors.load()
    )
    assert issued == ["tenantdom"]
    assert env.sidecar.pending_reloads() == []
    assert env.fake.calls_for("mtree.reload")[-1].params == ["tenantdom"]


def test_active_destroy_is_refused_when_purge_capability_is_missing(env, monkeypatch):
    from dataclasses import replace

    from console import capabilities
    from console import settings as settings_module
    from console.errors import KamailioUnavailable

    monkeypatch.setattr(
        settings_module,
        "settings",
        replace(settings_module.settings, enforce_capabilities=True),
    )
    capabilities.clear_cache()
    env.fake.fault("htable.delete", -32601, "Method not found")
    before = edge_signature(env.edge)

    with pytest.raises(KamailioUnavailable):
        destroy(
            env.sidecar,
            env.edge,
            env.worker,
            DOMAIN_A,
            confirm=DOMAIN_A,
            backup_dir=env.backups,
        )

    assert edge_signature(env.edge) == before
    assert env.sidecar.latest_intent_any(DOMAIN_A) is None
    assert list_backups(env.backups) == []


def test_park_refused_when_capability_probe_is_unreachable(env, monkeypatch):
    from dataclasses import replace

    from console import capabilities
    from console import settings as settings_module
    from console.errors import KamailioUnavailable

    monkeypatch.setattr(
        settings_module,
        "settings",
        replace(settings_module.settings, enforce_capabilities=True),
    )
    monkeypatch.setattr(
        capabilities,
        "probe",
        lambda rpc: {"reachable": False, "unknown": True, "htable.delete": False},
    )
    before = edge_signature(env.edge)

    with pytest.raises(KamailioUnavailable):
        park(env.sidecar, env.edge, env.worker, DOMAIN_A, backup_dir=env.backups)

    assert edge_signature(env.edge) == before
    assert env.sidecar.latest_intent_any(DOMAIN_A) is None


def test_restore_resumes_after_committed_rows_lost_their_transition(env, monkeypatch):
    parked = park(
        env.sidecar, env.edge, env.worker, DOMAIN_A, backup_dir=env.backups
    )
    assert parked.done is True

    original = env.sidecar.record_transition

    def lose_rows_inserted(intent_id, state, **kwargs):
        if state == "ROWS_INSERTED":
            raise InjectedCrash("lost transition after edge commit")
        return original(intent_id, state, **kwargs)

    monkeypatch.setattr(env.sidecar, "record_transition", lose_rows_inserted)
    with pytest.raises(InjectedCrash):
        restore(
            env.sidecar, env.edge, env.worker, DOMAIN_A, backup_dir=env.backups
        )

    # The edge transaction committed; only the sidecar transition was lost.
    assert (
        count(
            env.edge,
            "SELECT count(*) FROM tenant_registry WHERE tprefix = ?",
            (DOMAIN_A,),
        )
        == 1
    )
    assert (
        count(
            env.edge,
            "SELECT count(*) FROM tenant_landline WHERE tvalue LIKE ?",
            (f"{TENANT_A},%",),
        )
        == 1
    )

    monkeypatch.setattr(env.sidecar, "record_transition", original)
    summary = resume_pending(
        env.sidecar, env.edge, env.worker, backup_dir=env.backups
    )
    assert summary["completed"] == 1
    assert env.sidecar.latest_intent("restore", DOMAIN_A)["state"] == "DONE"


def test_capability_gate_does_not_trust_a_stale_cached_success(env, monkeypatch):
    from dataclasses import replace

    from console import capabilities
    from console import settings as settings_module
    from console.errors import KamailioUnavailable

    monkeypatch.setattr(
        settings_module,
        "settings",
        replace(settings_module.settings, enforce_capabilities=True),
    )
    capabilities.clear_cache()
    env.fake.script("htable.delete", 1)
    assert capabilities.probe(env.fake)["htable.delete"] is True

    # The edge loses the capability after the cache was primed.
    env.fake.fault("htable.delete", -32601, "Method not found")
    before = edge_signature(env.edge)
    with pytest.raises(KamailioUnavailable):
        park(env.sidecar, env.edge, env.worker, DOMAIN_A, backup_dir=env.backups)
    assert edge_signature(env.edge) == before


def test_park_resume_preserves_an_edit_made_after_snapshot(env):
    with pytest.raises(InjectedCrash):
        park(
            env.sidecar,
            env.edge,
            env.worker,
            DOMAIN_A,
            backup_dir=env.backups,
            fail_after="SNAPSHOTTED",
        )
    assert env.sidecar.latest_intent("park", DOMAIN_A)["state"] == "SNAPSHOTTED"

    with sqlite3.connect(env.edge) as conn:
        tvalue = conn.execute(
            "SELECT tvalue FROM tenant_registry WHERE tprefix = ?", (DOMAIN_A,)
        ).fetchone()[0]
        fields = tvalue.split(",")
        fields[4] = "A Edited"
        conn.execute(
            "UPDATE tenant_registry SET tvalue = ? WHERE tprefix = ?",
            (",".join(fields), DOMAIN_A),
        )
        conn.commit()

    assert resume_pending(
        env.sidecar, env.edge, env.worker, backup_dir=env.backups
    )["completed"] == 1
    assert restore(
        env.sidecar, env.edge, env.worker, DOMAIN_A, backup_dir=env.backups
    ).done is True

    with sqlite3.connect(env.edge) as conn:
        restored = conn.execute(
            "SELECT tvalue FROM tenant_registry WHERE tprefix = ?", (DOMAIN_A,)
        ).fetchone()[0]
    assert "A Edited" in restored


def test_restore_collision_creates_no_blocking_intent(env):
    assert park(
        env.sidecar, env.edge, env.worker, DOMAIN_A, backup_dir=env.backups
    ).done is True

    with sqlite3.connect(env.edge) as conn:
        conn.execute(
            "INSERT INTO tenant_registry (tprefix, tvalue) VALUES (?, ?)",
            ("sip.collision.home.example.net", f"{TENANT_A},10,30,09999999,X,10,1+1"),
        )
        conn.commit()

    with pytest.raises(CollisionError):
        restore(
            env.sidecar, env.edge, env.worker, DOMAIN_A, backup_dir=env.backups
        )

    intents = [
        row for row in env.sidecar.audit_rows(domain=DOMAIN_A)
    ]
    assert all(row["op"] != "restore" for row in intents)


def test_resume_park_after_edge_delete_commit_lost_its_transition(env, monkeypatch):
    original = env.sidecar.record_transition

    def lose_rows_deleted(intent_id, state, **kwargs):
        if state == "ROWS_DELETED":
            raise InjectedCrash("lost transition after edge commit")
        return original(intent_id, state, **kwargs)

    monkeypatch.setattr(env.sidecar, "record_transition", lose_rows_deleted)
    with pytest.raises(InjectedCrash):
        park(env.sidecar, env.edge, env.worker, DOMAIN_A, backup_dir=env.backups)

    assert env.sidecar.latest_intent("park", DOMAIN_A)["state"] == "SNAPSHOTTED"
    assert (
        count(
            env.edge,
            "SELECT count(*) FROM tenant_registry WHERE tprefix = ?",
            (DOMAIN_A,),
        )
        == 0
    )

    monkeypatch.setattr(env.sidecar, "record_transition", original)
    summary = resume_pending(
        env.sidecar, env.edge, env.worker, backup_dir=env.backups
    )
    assert summary["completed"] == 1
    assert env.sidecar.latest_intent("park", DOMAIN_A)["state"] == "DONE"


def test_park_resume_refuses_an_identity_change_after_snapshot(env):
    with pytest.raises(InjectedCrash):
        park(
            env.sidecar,
            env.edge,
            env.worker,
            DOMAIN_A,
            backup_dir=env.backups,
            fail_after="SNAPSHOTTED",
        )

    # Point A at B's dispatcher sets: re-collecting would delete B's rows.
    with sqlite3.connect(env.edge) as conn:
        conn.execute(
            "UPDATE tenant_registry SET tvalue = ? WHERE tprefix = ?",
            ("T9,11,31,09999999,A,10,1+1", DOMAIN_A),
        )
        conn.commit()

    summary = resume_pending(
        env.sidecar, env.edge, env.worker, backup_dir=env.backups
    )
    assert summary["failed"] == 1
    assert env.sidecar.latest_intent("park", DOMAIN_A)["state"] == "SNAPSHOTTED"
    assert (
        count(
            env.edge,
            "SELECT count(*) FROM dispatcher WHERE setid IN (11, 31)",
        )
        == 2
    )
    assert (
        count(
            env.edge,
            "SELECT count(*) FROM tenant_registry WHERE tprefix = ?",
            (DOMAIN_A,),
        )
        == 1
    )


def test_capability_gate_requires_both_dump_and_delete(env, monkeypatch):
    from dataclasses import replace

    from console import capabilities
    from console import settings as settings_module
    from console.errors import KamailioUnavailable

    monkeypatch.setattr(
        settings_module,
        "settings",
        replace(settings_module.settings, enforce_capabilities=True),
    )
    monkeypatch.setattr(
        capabilities,
        "probe",
        lambda rpc, **kwargs: {
            "reachable": True,
            "unknown": False,
            "htable.dump": False,
            "htable.delete": True,
        },
    )
    before = edge_signature(env.edge)
    with pytest.raises(KamailioUnavailable):
        park(env.sidecar, env.edge, env.worker, DOMAIN_A, backup_dir=env.backups)
    assert edge_signature(env.edge) == before


def test_park_resume_allows_a_landline_removal_after_snapshot(env):
    with pytest.raises(InjectedCrash):
        park(
            env.sidecar,
            env.edge,
            env.worker,
            DOMAIN_A,
            backup_dir=env.backups,
            fail_after="SNAPSHOTTED",
        )

    with sqlite3.connect(env.edge) as conn:
        conn.execute(
            "DELETE FROM tenant_landline WHERE tprefix = ?", (LANDLINE_A,)
        )
        conn.commit()

    assert resume_pending(
        env.sidecar, env.edge, env.worker, backup_dir=env.backups
    )["completed"] == 1
    assert restore(
        env.sidecar, env.edge, env.worker, DOMAIN_A, backup_dir=env.backups
    ).done is True
    assert (
        count(
            env.edge,
            "SELECT count(*) FROM tenant_landline WHERE tprefix = ?",
            (LANDLINE_A,),
        )
        == 0
    )


def test_resume_park_enforces_the_purge_capability_gate(env, monkeypatch):
    from dataclasses import replace

    from console import capabilities
    from console import settings as settings_module

    with pytest.raises(InjectedCrash):
        park(
            env.sidecar,
            env.edge,
            env.worker,
            DOMAIN_A,
            backup_dir=env.backups,
            fail_after="SNAPSHOTTED",
        )

    monkeypatch.setattr(
        settings_module,
        "settings",
        replace(settings_module.settings, enforce_capabilities=True),
    )
    monkeypatch.setattr(
        capabilities,
        "probe",
        lambda rpc, **kwargs: {
            "reachable": False,
            "unknown": True,
            "htable.dump": False,
            "htable.delete": False,
        },
    )
    summary = resume_pending(
        env.sidecar, env.edge, env.worker, backup_dir=env.backups
    )
    assert summary["failed"] == 1
    assert env.sidecar.latest_intent("park", DOMAIN_A)["state"] == "SNAPSHOTTED"
    assert (
        count(
            env.edge,
            "SELECT count(*) FROM tenant_registry WHERE tprefix = ?",
            (DOMAIN_A,),
        )
        == 1
    )


def test_park_refuses_a_dispatcher_set_shared_with_another_home(env):
    with sqlite3.connect(env.edge) as conn:
        tvalue = conn.execute(
            "SELECT tvalue FROM tenant_registry WHERE tprefix = ?", (DOMAIN_B,)
        ).fetchone()[0]
        fields = tvalue.split(",")
        fields[1] = "10"  # B now points at A's on-net set
        conn.execute(
            "UPDATE tenant_registry SET tvalue = ? WHERE tprefix = ?",
            (",".join(fields), DOMAIN_B),
        )
        conn.commit()

    before = edge_signature(env.edge)
    with pytest.raises(LifecycleError):
        park(env.sidecar, env.edge, env.worker, DOMAIN_A, backup_dir=env.backups)
    assert edge_signature(env.edge) == before


def test_parked_destroy_works_when_the_edge_db_is_absent(env):
    assert park(
        env.sidecar, env.edge, env.worker, DOMAIN_A, backup_dir=env.backups
    ).done is True
    missing = env.edge.with_name("missing-edge.sqlite")
    env.edge.unlink()

    result = destroy(
        env.sidecar, missing, env.worker, DOMAIN_A, confirm=""
    )
    assert result.done is True
    assert env.sidecar.load_snapshot(DOMAIN_A) is None
    assert not missing.exists()


def test_restore_resume_does_not_resurrect_an_edited_trusted_row(env, monkeypatch):
    assert park(
        env.sidecar, env.edge, env.worker, DOMAIN_A, backup_dir=env.backups
    ).done is True

    original = env.sidecar.record_transition

    def lose_rows_inserted(intent_id, state, **kwargs):
        if state == "ROWS_INSERTED":
            raise InjectedCrash("lost transition after edge commit")
        return original(intent_id, state, **kwargs)

    monkeypatch.setattr(env.sidecar, "record_transition", lose_rows_inserted)
    with pytest.raises(InjectedCrash):
        restore(
            env.sidecar, env.edge, env.worker, DOMAIN_A, backup_dir=env.backups
        )

    with sqlite3.connect(env.edge) as conn:
        conn.execute(
            "UPDATE trusted SET src_ip = ? WHERE tag = ?",
            ("100.96.99.99", DOMAIN_A),
        )
        conn.commit()

    monkeypatch.setattr(env.sidecar, "record_transition", original)
    summary = resume_pending(
        env.sidecar, env.edge, env.worker, backup_dir=env.backups
    )
    assert summary["failed"] == 1
    with sqlite3.connect(env.edge) as conn:
        rows = [
            row[0]
            for row in conn.execute(
                "SELECT src_ip FROM trusted WHERE tag = ? ORDER BY src_ip",
                (DOMAIN_A,),
            )
        ]
    assert rows == ["100.96.99.99"]
