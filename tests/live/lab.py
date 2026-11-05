"""lab.py — process, port and database helpers for the real-Kamailio journey.

Nothing here starts a process at import time.  The live test is marked
``live`` and skipped when the Kamailio binary, the edge repository or the
``sqlite3`` CLI is missing, so the default unit/API suite stays portable.

The helpers intentionally mirror ``kamailio-edge/tests/run-lab.sh``: the same
seed file, ``-A`` overrides, fake voip-stack and scripted UAC.
"""
from __future__ import annotations

import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from console.rpc import UnixDatagramRPC, flatten_htable_dump

__all__ = [
    "DEFAULT_EDGE_REPO",
    "EDGE_HOSTS",
    "EDGE_PORT",
    "KamailioLab",
    "FakeLab",
    "edge_repo",
    "missing_requirements",
    "busy_edge_ports",
    "free_udp_port",
    "seed_database",
    "set_dispatcher_destination",
    "start_kamailio",
    "stop_kamailio",
    "start_fake",
    "stop_fake",
    "register",
    "run_uac",
    "regcache",
    "wait_regcache",
    "dispatcher_sets",
    "scalar",
    "tail",
]

DEFAULT_EDGE_REPO = Path(__file__).resolve().parents[2]
LOCAL_ASSETS = Path(__file__).resolve().parent
KAMAILIO = Path(os.environ.get("KAMAILIO_BIN", "/usr/sbin/kamailio"))
EDGE_HOSTS = ("127.0.0.1", "127.0.0.9")
EDGE_PORT = 5060
_HOME1_LOG = "home1.log"


# ---------------------------------------------------------------------------
# environment / ports
# ---------------------------------------------------------------------------
def edge_repo() -> Path:
    """The repository whose ``kamailio/`` config the live gate boots.

    Defaults to this repository (so the sanitized config is what is tested);
    set ``VOIP_EDGE_REPO`` to test the deployed edge repo instead.
    """
    return Path(os.environ.get("VOIP_EDGE_REPO", str(DEFAULT_EDGE_REPO)))


def _asset(repo: Path, name: str) -> Path:
    """Prefer the target repo's test asset, else the copy shipped here."""
    candidate = repo / "tests" / name
    if candidate.is_file():
        return candidate
    local = LOCAL_ASSETS / name
    return local if local.is_file() else candidate


def missing_requirements() -> str | None:
    """Why the live journey cannot run here, or ``None`` when it can."""
    repo = edge_repo()
    if not KAMAILIO.is_file():
        return f"kamailio binary not found at {KAMAILIO}"
    if not (repo / "kamailio" / "kamailio.cfg").is_file():
        return f"kamailio config missing: {repo / 'kamailio' / 'kamailio.cfg'}"
    for name in ("seed-test.sql", "fake_voipstack.py", "uac.py"):
        if not _asset(repo, name).is_file():
            return f"live test asset missing: {_asset(repo, name)}"
    if shutil.which("sqlite3") is None:
        return "sqlite3 CLI not found"
    return None


def _port_free(host: str, port: int, kind: int) -> bool:
    sock = socket.socket(socket.AF_INET, kind)
    try:
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def busy_edge_ports() -> list[str]:
    """SIP edge ports that are not free (UDP and TCP on both listen addrs)."""
    busy: list[str] = []
    for host in EDGE_HOSTS:
        for kind, label in ((socket.SOCK_DGRAM, "udp"), (socket.SOCK_STREAM, "tcp")):
            if not _port_free(host, EDGE_PORT, kind):
                busy.append(f"{label} {host}:{EDGE_PORT}")
    return busy


def free_udp_port(preferred: int = 5190, host: str = "127.0.0.1") -> int:
    """Return *preferred* when free, else an ephemeral free UDP port."""
    if _port_free(host, preferred, socket.SOCK_DGRAM):
        return preferred
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# edge database
# ---------------------------------------------------------------------------
def seed_database(repo: Path, work: Path) -> Path:
    """Create ``work/edge.sqlite`` from the edge repo's ``seed-test.sql``.

    Like run-lab.sh this pipes the seed file through the ``sqlite3`` CLI, so
    the schema and rows are exactly the lab's.
    """
    work.mkdir(parents=True, exist_ok=True)
    seed = work / "seed-test.sql"
    shutil.copyfile(_asset(repo, "seed-test.sql"), seed)
    database = work / "edge.sqlite"
    with seed.open("rb") as stream:
        result = subprocess.run(
            [str(shutil.which("sqlite3")), str(database)],
            stdin=stream,
            capture_output=True,
            timeout=30,
        )
    if result.returncode != 0:
        raise RuntimeError(
            f"seed database failed: {result.stderr.decode(errors='replace')}"
        )
    return database


def set_dispatcher_destination(database: Path, setid: int, destination: str) -> None:
    with sqlite3.connect(str(database)) as conn:
        conn.execute(
            "UPDATE dispatcher SET destination = ? WHERE setid = ?",
            (destination, setid),
        )


def scalar(database: Path, sql: str, params: tuple[Any, ...] = ()) -> Any:
    with sqlite3.connect(str(database)) as conn:
        row = conn.execute(sql, params).fetchone()
    return None if row is None else row[0]


# ---------------------------------------------------------------------------
# processes
# ---------------------------------------------------------------------------
def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().split(")", 1)[1].split()
        if fields and fields[0] == "Z":
            return False
    except (OSError, IndexError):
        pass
    return True


def _child_pids(pid: int) -> list[int]:
    children: list[int] = []
    path = Path(f"/proc/{pid}/task/{pid}/children")
    try:
        children = [int(value) for value in path.read_text().split()]
    except (OSError, ValueError):
        return []
    descendants = list(children)
    for child in children:
        descendants.extend(_child_pids(child))
    return descendants


def _terminate_pid(pid: int) -> None:
    try:
        if os.getpgid(pid) == pid:
            os.killpg(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


def _kill_pid(pid: int) -> None:
    try:
        if os.getpgid(pid) == pid:
            os.killpg(pid, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


@dataclass
class KamailioLab:
    pid: int
    pidfile: Path
    log: Path
    sock: Path
    work: Path


def start_kamailio(
    repo: Path,
    work: Path,
    database: Path,
    sock: Path,
    *,
    startup_timeout: float = 30.0,
) -> KamailioLab:
    """Start the real binary with the edge config and lab ``-A`` overrides."""
    log = work / "kamailio.log"
    pidfile = work / "kamailio.pid"
    command = [
        str(KAMAILIO),
        "-f",
        str(repo / "kamailio" / "kamailio.cfg"),
        "-M",
        "64",
        "-m",
        "128",
        "-E",
        "-P",
        str(pidfile),
        "-A",
        'PUBIP="127.0.0.1"',
        "-A",
        'WGIP="127.0.0.9"',
        "-A",
        'WGSOCK="udp:127.0.0.9:5060"',
        "-A",
        f'DBURL="sqlite:///{database}"',
        "-A",
        "WITH_CONSOLE",
        "-A",
        f'CONSOLESOCK="{sock}"',
    ]
    with log.open("wb") as output:
        process = subprocess.Popen(
            command,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            returncode = process.wait(timeout=startup_timeout)
        except subprocess.TimeoutExpired:
            _kill_pid(process.pid)
            raise RuntimeError(
                "kamailio did not daemonize in time:\n" + tail(log)
            ) from None
    if returncode != 0:
        raise RuntimeError(
            f"kamailio refused to start (exit {returncode}):\n" + tail(log)
        )

    deadline = time.monotonic() + 5.0
    pid: int | None = None
    while time.monotonic() < deadline:
        try:
            pid = int(pidfile.read_text().strip())
            break
        except (OSError, ValueError):
            time.sleep(0.1)
    if pid is None:
        raise RuntimeError("kamailio started but wrote no pidfile:\n" + tail(log))

    lab = KamailioLab(pid=pid, pidfile=pidfile, log=log, sock=sock, work=work)
    try:
        _wait_ready(pid, sock, log, timeout=startup_timeout)
    except BaseException:
        stop_kamailio(lab)
        raise
    return lab


def _wait_ready(pid: int, sock: Path, log: Path, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            raise RuntimeError(
                f"kamailio pid {pid} exited during startup:\n" + tail(log)
            )
        if sock.exists():
            try:
                UnixDatagramRPC(str(sock), timeout_ms=500).call("core.uptime")
                return
            except Exception:
                pass
        time.sleep(0.2)
    raise RuntimeError(
        f"kamailio RPC socket {sock} was not ready within {timeout:g}s:\n"
        + tail(log)
    )


def stop_kamailio(kam: KamailioLab) -> None:
    """Stop the daemon by pidfile PID, then reap any surviving children."""
    pid = kam.pid
    descendants = _child_pids(pid)
    _terminate_pid(pid)

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if not pid_alive(pid) and not any(pid_alive(child) for child in descendants):
            return
        time.sleep(0.1)

    for survivor in [pid, *descendants]:
        if pid_alive(survivor):
            _kill_pid(survivor)
    time.sleep(0.2)


@dataclass
class FakeLab:
    process: subprocess.Popen
    log: Path
    output: Path
    stream: Any


def start_fake(
    repo: Path,
    log: Path,
    port: int,
    *,
    user: str = "alice",
    password: str = "secret1",
    startup_timeout: float = 10.0,
) -> FakeLab:
    """Start the edge repo's fake voip-stack (home1 digest account)."""
    output = log.with_suffix(".out")
    stream = output.open("wb")
    command = [
        sys.executable,
        str(_asset(repo, "fake_voipstack.py")),
        "--port",
        str(port),
        "--log",
        str(log),
        "--user",
        user,
        "--password",
        password,
    ]
    process = subprocess.Popen(
        command,
        stdout=stream,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    # No readiness socket to poll; give the asyncio endpoint a moment to bind
    # and fail fast if the interpreter exited.
    time.sleep(min(0.5, startup_timeout))
    if process.poll() is not None:
        stream.close()
        raise RuntimeError(
            "fake voip-stack exited during startup:\n" + tail(output)
        )
    return FakeLab(process=process, log=log, output=output, stream=stream)


def stop_fake(fake: FakeLab) -> None:
    process = fake.process
    if process.poll() is None:
        _terminate_pid(process.pid)
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            _kill_pid(process.pid)
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass
    try:
        fake.stream.close()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# scripted UAC / RPC views
# ---------------------------------------------------------------------------
def run_uac(repo: Path, *arguments: str, timeout: float = 30.0) -> int:
    """Run the edge repo's ``uac.py`` and return its ``RESULT=`` SIP status."""
    command = [sys.executable, str(_asset(repo, "uac.py")), *arguments]
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=timeout
    )
    output = (result.stdout or "") + (result.stderr or "")
    for line in reversed(output.splitlines()):
        if line.startswith("RESULT="):
            return int(line.split("=", 1)[1])
    raise RuntimeError(
        f"uac.py produced no RESULT (exit {result.returncode}): {output.strip()}"
    )


def register(
    repo: Path,
    *,
    domain: str,
    bind: str,
    user: str = "alice",
    password: str = "secret1",
    expires: int = 600,
    cseq: int = 1,
) -> int:
    return run_uac(
        repo,
        "--cmd",
        "register",
        "--user",
        user,
        "--password",
        password,
        "--domain",
        domain,
        "--bind",
        bind,
        "--expires",
        str(expires),
        "--cseq",
        str(cseq),
    )


def regcache(rpc: Any) -> dict[str, dict[str, Any]]:
    return flatten_htable_dump(rpc.call("htable.dump", ["regcache"]))


def wait_regcache(
    rpc: Any,
    key: str,
    *,
    present: bool = True,
    timeout: float = 8.0,
) -> dict[str, dict[str, Any]]:
    """Poll ``regcache`` until *key* appears/disappears (or raise)."""
    deadline = time.monotonic() + timeout
    flat: dict[str, dict[str, Any]] = {}
    while time.monotonic() < deadline:
        flat = regcache(rpc)
        if (key in flat) is present:
            return flat
        time.sleep(0.2)
    state = "present" if present else "absent"
    raise AssertionError(
        f"regcache key {key!r} not {state} after {timeout:g}s; keys={sorted(flat)}"
    )


def dispatcher_sets(rpc: Any) -> dict[int, list[str]]:
    """Map dispatcher set id -> destination URIs from ``dispatcher.list``."""
    result = rpc.call("dispatcher.list")
    records = result.get("RECORDS", []) if isinstance(result, dict) else result
    sets: dict[int, list[str]] = {}
    for record in records or []:
        if not isinstance(record, dict):
            continue
        entry = record.get("SET", record)
        if not isinstance(entry, dict):
            continue
        try:
            setid = int(entry.get("ID"))
        except (TypeError, ValueError):
            continue
        uris: list[str] = []
        for target in entry.get("TARGETS", []) or []:
            dest = target.get("DEST", target) if isinstance(target, dict) else {}
            uri = dest.get("URI") if isinstance(dest, dict) else None
            if uri:
                uris.append(str(uri))
        sets[setid] = uris
    return sets


# ---------------------------------------------------------------------------
# logs
# ---------------------------------------------------------------------------
def tail(path: Path, lines: int = 40) -> str:
    try:
        content = path.read_text(errors="replace").splitlines()
    except OSError:
        return f"(no log at {path})"
    return "\n".join(content[-lines:])
