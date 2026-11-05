"""Unit tests for console.reloads — serialized, throttled reload worker."""
from __future__ import annotations

import threading
import time

import pytest

from console.reloads import ReloadWorker
from console.rpc import RPCFault
from tests.fixtures.fakerpc import FakeRPC


class VirtualClock:
    """A monotonic-clock double whose sleep advances virtual time.

    ``freeze()`` blocks the worker inside its next ``sleep`` so a test can
    enqueue several saves inside the same throttle window deterministically.
    """

    def __init__(self) -> None:
        self._now = 0.0
        self._lock = threading.Lock()
        self._gate = threading.Event()
        self._gate.set()
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        with self._lock:
            return self._now

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.sleeps.append(seconds)
        self._gate.wait()
        with self._lock:
            self._now += seconds

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._now += seconds

    def freeze(self) -> None:
        self._gate.clear()

    def release(self) -> None:
        self._gate.set()


def wait_until(predicate, timeout: float = 2.0, interval: float = 0.005) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


@pytest.fixture()
def fake(tmp_path):
    with FakeRPC(tmp_path / "kamailio_rpc.sock") as server:
        yield server


def test_three_enqueues_same_table_inside_window_issue_one_call(fake):
    fake.script("htable.reload", 1)
    clock = VirtualClock()
    clock.freeze()
    recorded: list[tuple] = []
    worker = ReloadWorker(
        fake,
        {"homes": 5},
        store=lambda *args: recorded.append(args),
        clock=clock,
        sleep=clock.sleep,
    )
    try:
        worker.enqueue("homes", "htable.reload", ["homes", 1])
        assert wait_until(lambda: len(clock.sleeps) >= 1), "worker never entered its throttle sleep"

        # Two later saves in the same window: coalesce, do not extend it.
        clock.advance(2)
        worker.enqueue("homes", "htable.reload", ["homes", 2])
        clock.advance(1)
        worker.enqueue("homes", "htable.reload", ["homes", 3])
        clock.release()

        assert worker.flush(timeout=2.0)
    finally:
        clock.release()
        worker.shutdown()

    assert fake.call_count == 1
    assert fake.calls[0].method == "htable.reload"
    # Latest enqueue wins; one RPC and one store notification.
    assert fake.calls[0].params == ["homes", 3]
    assert recorded == [("done", "homes", "htable.reload", ["homes", 3])]
    # Only the first window's throttle delay was ever slept.
    assert clock.sleeps == [5]


def test_different_tables_are_independent(fake):
    fake.script("htable.reload", 1)
    fake.script("dispatcher.reload", 1)
    clock = VirtualClock()
    clock.freeze()
    worker = ReloadWorker(
        fake, {"homes": 5, "dispatcher": 5}, clock=clock, sleep=clock.sleep
    )
    try:
        worker.enqueue("homes", "htable.reload", ["homes"])
        assert wait_until(lambda: len(clock.sleeps) >= 1)
        worker.enqueue("dispatcher", "dispatcher.reload", [])
        clock.release()
        assert worker.flush(timeout=2.0)
    finally:
        clock.release()
        worker.shutdown()

    assert sorted(fake.methods()) == ["dispatcher.reload", "htable.reload"]
    assert len(fake.calls_for("htable.reload")) == 1
    assert len(fake.calls_for("dispatcher.reload")) == 1


def test_next_window_sends_another_call(fake):
    fake.script("htable.reload", 1)
    clock = VirtualClock()
    recorded: list[tuple] = []
    worker = ReloadWorker(
        fake,
        {"homes": 5},
        store=lambda *args: recorded.append(args),
        clock=clock,
        sleep=clock.sleep,
    )
    try:
        worker.enqueue("homes", "htable.reload", ["homes"])
        assert worker.flush(timeout=2.0)
        assert fake.call_count == 1

        clock.advance(10)
        worker.enqueue("homes", "htable.reload", ["homes"])
        assert worker.flush(timeout=2.0)
    finally:
        worker.shutdown()

    assert fake.call_count == 2
    assert recorded == [
        ("done", "homes", "htable.reload", ["homes"]),
        ("done", "homes", "htable.reload", ["homes"]),
    ]


def test_calls_are_serialized_even_when_a_method_is_slow(fake):
    def slow(method, params):
        time.sleep(0.05)
        return 1

    fake.handler("slow.reload", slow)
    clock = VirtualClock()
    recorded: list[tuple] = []
    worker = ReloadWorker(
        fake,
        {"a": 0, "b": 0},
        store=lambda *args: recorded.append(args),
        clock=clock,
        sleep=clock.sleep,
    )
    try:
        worker.enqueue("a", "slow.reload", ["a"])
        worker.enqueue("b", "slow.reload", ["b"])
        assert worker.flush(timeout=3.0)
    finally:
        worker.shutdown()

    assert fake.call_count == 2
    assert fake.max_concurrency == 1
    assert sorted(recorded) == [
        ("done", "a", "slow.reload", ["a"]),
        ("done", "b", "slow.reload", ["b"]),
    ]


def test_fault_is_recorded_store_says_pending_and_worker_survives(fake):
    fake.fault("dispatcher.reload", code=503, message="cannot reload dispatcher")
    clock = VirtualClock()
    recorded: list[tuple] = []
    worker = ReloadWorker(
        fake,
        {"dispatcher": 0},
        store=lambda *args: recorded.append(args),
        clock=clock,
        sleep=clock.sleep,
    )
    try:
        worker.enqueue("dispatcher", "dispatcher.reload", ["dispatcher"])
        assert worker.flush(timeout=2.0)

        assert fake.call_count == 1
        assert recorded == [
            ("pending", "dispatcher", "dispatcher.reload", ["dispatcher"])
        ]
        assert isinstance(worker.errors["dispatcher"], RPCFault)
        assert worker.errors["dispatcher"].code == 503
        assert worker.errors["dispatcher"].message == "cannot reload dispatcher"

        # The worker must survive the fault and serve the next call.
        fake.script("dispatcher.reload", 1)
        worker.enqueue("dispatcher", "dispatcher.reload", ["dispatcher"])
        assert worker.flush(timeout=2.0)
    finally:
        worker.shutdown()

    assert fake.call_count == 2
    assert recorded[-1] == ("done", "dispatcher", "dispatcher.reload", ["dispatcher"])
    assert "dispatcher" not in worker.errors


def test_kamailio_unavailable_is_pending_and_worker_survives(fake):
    fake.silent("htable.reload")
    clock = VirtualClock()
    recorded: list[tuple] = []
    # Short client timeout so the silent method returns quickly.
    from console.rpc import UnixDatagramRPC

    rpc = UnixDatagramRPC(fake.sock_path, timeout_ms=50)
    worker = ReloadWorker(
        rpc,
        {"homes": 0},
        store=lambda *args: recorded.append(args),
        clock=clock,
        sleep=clock.sleep,
    )
    try:
        worker.enqueue("homes", "htable.reload", ["homes"])
        assert worker.flush(timeout=2.0)
        assert recorded == [("pending", "homes", "htable.reload", ["homes"])]
        assert "homes" in worker.errors

        fake.script("htable.reload", 1)
        worker.enqueue("homes", "htable.reload", ["homes"])
        assert worker.flush(timeout=2.0)
    finally:
        worker.shutdown()

    assert recorded[-1] == ("done", "homes", "htable.reload", ["homes"])
    assert "homes" not in worker.errors


def test_pending_persistence_tuples_for_mixed_outcomes(fake):
    fake.script("htable.reload", 1)
    fake.fault("dispatcher.reload", code=500, message="old data stays active")
    clock = VirtualClock()
    recorded: list[tuple] = []
    worker = ReloadWorker(
        fake,
        {"homes": 0, "dispatcher": 0},
        store=lambda *args: recorded.append(args),
        clock=clock,
        sleep=clock.sleep,
    )
    try:
        worker.enqueue("homes", "htable.reload", ["homes"])
        worker.enqueue("dispatcher", "dispatcher.reload", ["dispatcher"])
        assert worker.flush(timeout=2.0)
    finally:
        worker.shutdown()

    assert ("done", "homes", "htable.reload", ["homes"]) in recorded
    assert (
        "pending",
        "dispatcher",
        "dispatcher.reload",
        ["dispatcher"],
    ) in recorded
    assert set(worker.errors) == {"dispatcher"}


def test_on_enqueue_persists_before_the_worker_runs(fake):
    fake.script("mtree.reload", 1)
    events: list[tuple] = []
    worker = ReloadWorker(
        fake,
        {"homes": 0},
        on_enqueue=lambda *args: events.append(args),
    )
    try:
        worker.enqueue("homes", "mtree.reload", ["tenantdom"])
        assert events == [("homes", "mtree.reload", ["tenantdom"])]
        assert worker.flush(2.0) is True
    finally:
        worker.shutdown(0.2)


def test_store_baseexception_does_not_wedge_the_worker(fake):
    class Boom(BaseException):
        pass

    fake.script("mtree.reload", 1)

    def store(status, *args):
        if status == "done":
            raise Boom("store exploded")

    worker = ReloadWorker(fake, {"homes": 0}, store=store)
    try:
        worker.enqueue("homes", "mtree.reload", ["tenantdom"])
        assert worker.flush(2.0) is True
        worker.enqueue("homes", "mtree.reload", ["tenantdom"])
        assert worker.flush(2.0) is True
    finally:
        worker.shutdown(0.2)


def test_probe_and_worker_do_not_run_reloads_concurrently():
    from console import capabilities

    class SlowRPC:
        def __init__(self) -> None:
            self._lock = threading.Lock()
            self._active = 0
            self.max_concurrency = 0
            self.started = threading.Event()

        def call(self, method, params=None):
            with self._lock:
                self._active += 1
                self.max_concurrency = max(self.max_concurrency, self._active)
            try:
                if method == "mtree.reload":
                    self.started.set()
                    time.sleep(0.2)
                if method == "htable.dump":
                    return []
                return 1
            finally:
                with self._lock:
                    self._active -= 1

    rpc = SlowRPC()
    capabilities.clear_cache()
    worker = ReloadWorker(rpc, {"homes": 0})
    try:
        worker.enqueue("homes", "mtree.reload", ["tenantdom"])
        assert rpc.started.wait(2.0)
        capabilities.probe(rpc, ttl=0)
        assert worker.flush(5.0) is True
        assert rpc.max_concurrency == 1
    finally:
        worker.shutdown(0.2)
