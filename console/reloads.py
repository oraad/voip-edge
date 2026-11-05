"""reloads.py — serialized background reload worker with coalescing throttle.

Kamailio's ``*_reload`` RPCs are not safe to run concurrently for the same
table (documented crash hazard) and several tables enforce their own reload
delta.  Plan §5.8 therefore puts **one** worker thread in front of all
reloads:

* a global FIFO-ish queue (dict of table → pending intent);
* per-table windows: the timer starts at the **first** enqueue, later saves
  for the same table coalesce into the same window and do **not** extend it;
* at most one ``rpc.call`` in flight at any time (global serialization);
* successes notify the persistence callback with ``("done", ...)``; RPC
  faults / an unreachable edge notify it with ``("pending", ...)`` and keep
  the error in :attr:`ReloadWorker.errors` for the global banner.

No asyncio: endpoints only call :meth:`ReloadWorker.enqueue`.

Store callback contract
-----------------------
``store`` is called as ``store(status, table, rpc_name, params)`` where
``status`` is ``"done"`` or ``"pending"``.  A one-argument callable (for
example ``list.append``) receives the four values as a single tuple, which
keeps the worker usable with a simple recording list.
"""
from __future__ import annotations

import inspect
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

__all__ = ["ReloadWorker"]


@dataclass
class _Pending:
    rpc_name: str
    params: list[Any] = field(default_factory=list)
    first_at: float = 0.0


class ReloadWorker:
    """Consume reload intents on one daemon thread, serialized and throttled."""

    def __init__(
        self,
        rpc: Any,
        throttle_seconds: dict[str, int],
        store: Callable[..., Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.rpc = rpc
        self.throttle_seconds = dict(throttle_seconds or {})
        self.store = store
        self.clock = clock
        self.sleep = sleep

        self.errors: dict[str, BaseException] = {}

        self._cond = threading.Condition()
        self._pending: dict[str, _Pending] = {}
        self._in_flight = False
        self._stop = False
        self._thread: threading.Thread | None = None
        self._store_one_arg = self._detect_single_arg_store(store)

    # -- public API ----------------------------------------------------

    def enqueue(self, table: str, rpc_name: str, params: list) -> None:
        """Queue one reload intent for *table* (non-blocking).

        Repeated saves for the same table inside the current throttle window
        coalesce; the last ``rpc_name``/``params`` win.
        """
        params = list(params) if params is not None else []
        with self._cond:
            if self._stop:
                raise RuntimeError("ReloadWorker has been shut down")
            pending = self._pending.get(table)
            if pending is None:
                self._pending[table] = _Pending(
                    rpc_name=rpc_name, params=params, first_at=self.clock()
                )
            else:
                pending.rpc_name = rpc_name
                pending.params = params
            self._ensure_thread_locked()
            self._cond.notify_all()

    @property
    def pending_tables(self) -> list[str]:
        """Tables currently waiting for their throttle window."""
        with self._cond:
            return list(self._pending)

    def flush(self, timeout: float = 2.0) -> bool:
        """Wait until no reload is pending or in flight.

        Returns ``True`` on a clean drain, ``False`` when *timeout* elapsed.
        With an injected clock/sleep (tests) this drains synchronously.
        """
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._pending or self._in_flight:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(min(remaining, 0.01))
            return True

    def shutdown(self, timeout: float = 2.0) -> bool:
        """Drain (bounded), stop the thread and join it.

        Returns ``True`` when the worker thread has exited.  Pending intents
        are already persisted by the caller (plan §5.6) and are dropped if
        the drain times out.
        """
        self.flush(timeout)
        with self._cond:
            # Persist anything still queued before dropping it (plan §5.6).
            for table, entry in list(self._pending.items()):
                self._notify_store("pending", table, entry.rpc_name, entry.params)
            self._pending.clear()
            self._stop = True
            self._cond.notify_all()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        return thread is None or not thread.is_alive()

    # Friendly aliases for callers/tests using either vocabulary.
    wait = flush
    stop = shutdown

    @property
    def is_alive(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    # -- worker --------------------------------------------------------

    def _ensure_thread_locked(self) -> None:
        if self._thread is not None:
            if self._thread.is_alive():
                return
            # A crashed worker (e.g. a BaseException in a store callback) is
            # replaced on the next enqueue rather than leaving intents stuck.
            self._thread = None
        self._thread = threading.Thread(
            target=self._run, name="console-reload-worker", daemon=True
        )
        self._thread.start()

    def _throttle(self, table: str) -> float:
        try:
            return max(0.0, float(self.throttle_seconds.get(table, 0)))
        except (TypeError, ValueError):
            return 0.0

    def _run(self) -> None:
        while True:
            wait_for: float | None = None
            entry: _Pending | None = None
            table = ""
            with self._cond:
                if self._stop:
                    return
                if not self._pending:
                    self._cond.wait()
                    continue
                now = self.clock()
                table = min(
                    self._pending,
                    key=lambda name: self._pending[name].first_at
                    + self._throttle(name),
                )
                candidate = self._pending[table]
                due_at = candidate.first_at + self._throttle(table)
                if now < due_at:
                    wait_for = due_at - now
                else:
                    del self._pending[table]
                    self._in_flight = True
                    entry = candidate

            if wait_for is not None:
                self.sleep(wait_for)
                continue

            assert entry is not None
            try:
                self._execute(table, entry)
            except BaseException as exc:  # never kill the worker thread
                self.errors[table] = exc
                self._notify_store("pending", table, entry.rpc_name, entry.params)
                with self._cond:
                    self._in_flight = False
                    self._cond.notify_all()

    def _execute(self, table: str, entry: _Pending) -> None:
        status = "done"
        try:
            self.rpc.call(entry.rpc_name, entry.params)
        except BaseException as exc:  # RPCFault, KamailioUnavailable, or a stub failure
            self.errors[table] = exc
            status = "pending"
        else:
            self.errors.pop(table, None)

        self._notify_store(status, table, entry.rpc_name, entry.params)

        with self._cond:
            self._in_flight = False
            self._cond.notify_all()

    def _notify_store(
        self, status: str, table: str, rpc_name: str, params: list[Any]
    ) -> None:
        if self.store is None:
            return
        try:
            if self._store_one_arg:
                self.store((status, table, rpc_name, params))
            else:
                self.store(status, table, rpc_name, params)
        except Exception:
            # Persistence failures must never kill the reload worker; the
            # caller surfaces sidecar problems through its own path.
            pass

    @staticmethod
    def _detect_single_arg_store(store: Callable[..., Any] | None) -> bool:
        if store is None:
            return False
        try:
            signature = inspect.signature(store)
        except (TypeError, ValueError):
            return False
        parameters = list(signature.parameters.values())
        if any(
            parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD)
            for parameter in parameters
        ):
            return False
        positional = [
            parameter
            for parameter in parameters
            if parameter.kind
            in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
        ]
        return len(positional) == 1
