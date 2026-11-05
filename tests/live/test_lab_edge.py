"""Live integration journey: park/restore a home against real Kamailio.

This is the integration gate for the console lifecycle, not a FakeRPC test:
it boots the edge repository's real ``kamailio`` binary with ``WITH_CONSOLE``
JSON-RPC, registers a softphone through the edge, then drives the console's
``park`` and ``restore`` against that live edge.

Run it with::

    ./tests/live/run-live.sh

The test is marked ``live``; ``pyproject.toml`` deselects that marker for the
default ``pytest`` run (addopts ``-m 'not live'``), so unit/API suites never
start a process.  Missing binary/edge repo turns the live run into a skip.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest

from console import descriptors
from console.lifecycle import park, restore
from console.reloads import ReloadWorker
from console.rpc import UnixDatagramRPC
from console.sidecar import Sidecar

from tests.live import lab

DOMAIN = "home1.test"
TENANT_ID = "T1"
ONNET_SET = 11
PSTN_SET = 31
PSTN_DEST = "sip:127.0.0.1:5291;transport=udp"
BIND = "127.0.0.21"

_MISSING = lab.missing_requirements()
pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(_MISSING is not None, reason=_MISSING or "live lab unavailable"),
]

_PARK_RELOADS = [
    ("tenantdom", "mtree.reload", ["tenantdom"]),
    ("landline", "mtree.reload", ["landline"]),
    ("dispatcher", "dispatcher.reload", []),
    ("trusted", "permissions.trustedReload", []),
]


def _reload_tuples(events: list[Any]) -> list[tuple[str, str, list[str]]]:
    return [(event[1], event[2], event[3]) for event in events]


def _dump_failure_logs(kam: lab.KamailioLab | None, fake: lab.FakeLab | None) -> None:
    if kam is not None:
        print("---- kamailio.log (tail) ----")
        print(lab.tail(kam.log))
    if fake is not None:
        print("---- home1 fake log (tail) ----")
        print(lab.tail(fake.log))
        print("---- home1 fake stdout (tail) ----")
        print(lab.tail(fake.output))


def test_live_park_restore_journey():
    busy = lab.busy_edge_ports()
    if busy:
        pytest.skip("SIP edge port busy: " + ", ".join(busy))

    repo = lab.edge_repo()
    work = Path(tempfile.mkdtemp(prefix="voip-console-live-"))
    kam: lab.KamailioLab | None = None
    fake: lab.FakeLab | None = None
    worker: ReloadWorker | None = None
    try:
        # 1. throwaway edge.sqlite from the edge lab's own seed -----------------
        database = lab.seed_database(repo, work)
        assert lab.scalar(database, "SELECT count(*) FROM tenant_registry") == 3

        # 2. fake home1 (digest account alice/secret1) + real kamailio ---------
        port = lab.free_udp_port(5190)
        onnet_dest = f"sip:127.0.0.1:{port};transport=udp"
        if port != 5190:
            # The seed points set 11 at 5190; keep the journey runnable when
            # another lab still holds that port.
            lab.set_dispatcher_destination(database, ONNET_SET, onnet_dest)

        sock = work / "console_rpc.sock"
        kam = lab.start_kamailio(repo, work, database, sock)
        assert sock.exists(), "kamailio did not create the console RPC socket"
        fake = lab.start_fake(repo, work / "home1.log", port)
        rpc = UnixDatagramRPC(str(sock), timeout_ms=5000)

        # 3. softphone REGISTER through the live edge, cached in regcache ------
        assert lab.register(repo, domain=DOMAIN, bind=BIND, cseq=1) == 200
        lab.wait_regcache(rpc, f"alice@{DOMAIN}", present=True)
        sets = lab.dispatcher_sets(rpc)
        assert ONNET_SET in sets and onnet_dest in sets[ONNET_SET]
        assert PSTN_SET in sets and PSTN_DEST in sets[PSTN_SET]

        # 4. console lifecycle against the LIVE edge ---------------------------
        tableset = descriptors.load()
        throttles = {
            descriptor.table: descriptor.reload.throttle_seconds
            for descriptor in tableset.tables.values()
        }
        sidecar = Sidecar(work / "console.sqlite")
        events: list[Any] = []
        worker = ReloadWorker(rpc, throttles, store=events.append)
        backups = work / "backups"

        parked = park(
            sidecar,
            database,
            worker,
            DOMAIN,
            backup_dir=backups,
            tableset=tableset,
        )
        assert parked.done is True and parked.state == "DONE", parked
        assert worker.flush(30.0) is True
        assert worker.errors == {}
        assert [event[0] for event in events] == ["done"] * 4
        assert _reload_tuples(events) == _PARK_RELOADS

        # rows for that home are gone from edge.sqlite
        assert (
            lab.scalar(
                database,
                "SELECT count(*) FROM tenant_registry WHERE tprefix = ?",
                (DOMAIN,),
            )
            == 0
        )
        assert (
            lab.scalar(
                database,
                "SELECT count(*) FROM tenant_landline WHERE tvalue LIKE ?",
                (f"{TENANT_ID},%",),
            )
            == 0
        )
        assert (
            lab.scalar(
                database,
                "SELECT count(*) FROM dispatcher WHERE setid IN (?, ?)",
                (ONNET_SET, PSTN_SET),
            )
            == 0
        )
        assert (
            lab.scalar(
                database,
                "SELECT count(*) FROM trusted WHERE tag = ?",
                (DOMAIN,),
            )
            == 0
        )

        # regcache holds no AOR for the parked domain
        flat = lab.regcache(rpc)
        assert not [key for key in flat if key.endswith(f"@{DOMAIN}")], flat

        # mtree reload took effect: a new REGISTER for the domain is 403
        assert lab.register(repo, domain=DOMAIN, bind=BIND, cseq=10) == 403

        # dispatcher reload took effect: the home's sets left the live table
        # (deterministic alternative to counting OPTIONS pings for 2x20s)
        sets = lab.dispatcher_sets(rpc)
        assert ONNET_SET not in sets, sets
        assert PSTN_SET not in sets, sets
        assert onnet_dest not in [uri for uris in sets.values() for uri in uris]

        # 5. restore: rows, reloads, coherence, REGISTER path is healthy ------
        events.clear()
        restored = restore(
            sidecar,
            database,
            worker,
            DOMAIN,
            backup_dir=backups,
            tableset=tableset,
        )
        assert restored.done is True and restored.state == "DONE", restored
        assert worker.flush(30.0) is True
        assert worker.errors == {}
        assert [event[0] for event in events] == ["done"] * 4
        assert _reload_tuples(events) == _PARK_RELOADS

        assert (
            lab.scalar(
                database,
                "SELECT count(*) FROM tenant_registry WHERE tprefix = ?",
                (DOMAIN,),
            )
            == 1
        )
        assert (
            lab.scalar(
                database,
                "SELECT count(*) FROM tenant_landline WHERE tvalue LIKE ?",
                (f"{TENANT_ID},%",),
            )
            == 0
        )
        assert (
            lab.scalar(
                database,
                "SELECT count(*) FROM dispatcher WHERE setid IN (?, ?)",
                (ONNET_SET, PSTN_SET),
            )
            == 2
        )
        assert (
            lab.scalar(
                database,
                "SELECT count(*) FROM trusted WHERE tag = ?",
                (DOMAIN,),
            )
            == 1
        )

        sets = lab.dispatcher_sets(rpc)
        assert sets[ONNET_SET] == [onnet_dest], sets
        assert sets[PSTN_SET] == [PSTN_DEST], sets

        assert lab.register(repo, domain=DOMAIN, bind=BIND, cseq=20) == 200
        lab.wait_regcache(rpc, f"alice@{DOMAIN}", present=True)
        assert "event=register result=ok" in fake.log.read_text(errors="replace")

        print(
            "LIVE PASS: park/restore of "
            f"{DOMAIN} against kamailio {lab.KAMAILIO} "
            f"(rpc {sock}, home1 {onnet_dest})"
        )
    except BaseException:
        _dump_failure_logs(kam, fake)
        raise
    finally:
        if worker is not None:
            worker.shutdown(10.0)
        if fake is not None:
            lab.stop_fake(fake)
        if kam is not None:
            lab.stop_kamailio(kam)
        shutil.rmtree(work, ignore_errors=True)

    # cleanup proof: no listener survived the journey
    assert lab.busy_edge_ports() == []
