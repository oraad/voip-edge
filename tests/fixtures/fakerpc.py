"""A real AF_UNIX ``SOCK_DGRAM`` JSON-RPC 2.0 server for tests.

This is deliberately not a mock: the client under test speaks to a bound
unix-datagram socket, so framing, the reply address, socket timeouts and
malformed replies are exercised for real.

Usage::

    with FakeRPC(tmp_path / "kamailio_rpc.sock") as fake:
        fake.script("htable.dump", [{"entry": 0, "size": 0, "slot": []}])
        rpc = UnixDatagramRPC(fake.sock_path, timeout_ms=200)
        assert rpc.call("htable.dump", ["regcache"]) == [...]

Scripting a method (`fake.replies[method]`):

* any plain value (dict/list/str/int) is returned as the JSON-RPC result;
* a callable is invoked and its return value is used as the result;
* :data:`SILENT` (or :class:`NoReply`) sends nothing back, so the client
  has to time out;
* :class:`Fault` (helpers :meth:`fault`) produces a JSON-RPC error object;
* :class:`Raw` (helper :meth:`raw`) sends the given bytes verbatim, for
  short/garbled-reply tests;
* an exception instance/class, or a callable raising one, is translated:
  ``RPCFault`` becomes an error reply, ``KamailioUnavailable``/``NoReply``
  becomes silence, anything else becomes ``-32603``.

Requests are handled on their own daemon threads so ``max_concurrency``
can prove that a client serialized its calls.  Every request is recorded as
``Call(method, params, timestamp)`` in :attr:`calls`; request ids are kept in
:attr:`request_ids`.
"""
from __future__ import annotations

import inspect
import json
import os
import socket
import threading
import time
from collections import namedtuple
from dataclasses import dataclass
from typing import Any, Callable

from console.errors import KamailioUnavailable

__all__ = [
    "Call",
    "Fault",
    "FakeRPC",
    "NoReply",
    "Raw",
    "SILENT",
]

Call = namedtuple("Call", "method params timestamp")


class NoReply(Exception):
    """Raised by a scripted callable to send no datagram at all."""


@dataclass(frozen=True)
class Fault:
    """A scripted JSON-RPC error object."""

    code: int = 500
    message: str = "scripted fault"


@dataclass(frozen=True)
class Raw:
    """Scripted raw reply bytes (for malformed/truncated replies)."""

    payload: bytes


class _Silent:
    def __repr__(self) -> str:  # pragma: no cover - repr only
        return "SILENT"


SILENT = _Silent()


class FakeRPC:
    """Serve JSON-RPC 2.0 on a real unix-datagram socket."""

    def __init__(
        self, sock_path: str | os.PathLike[str], timeout_ms: int = 2000
    ) -> None:
        self.sock_path = str(sock_path)
        self.timeout_ms = int(timeout_ms)
        self.replies: dict[str, Any] = {}
        self.calls: list[Call] = []
        self.request_ids: list[Any] = []
        self.active = 0
        self.max_concurrency = 0

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._closed = False
        self._threads: list[threading.Thread] = []

        parent = os.path.dirname(self.sock_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        if os.path.exists(self.sock_path):
            os.unlink(self.sock_path)

        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self._sock.bind(self.sock_path)
        # A short poll timeout lets stop() join promptly.
        self._sock.settimeout(0.05)

        self._thread = threading.Thread(
            target=self._serve, name=f"FakeRPC:{os.path.basename(self.sock_path)}", daemon=True
        )
        self._thread.start()

    # -- lifecycle -----------------------------------------------------

    def __enter__(self) -> "FakeRPC":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    def stop(self) -> None:
        """Signal the server thread, close the socket and unlink the path."""
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        if self._thread.is_alive():
            self._thread.join(timeout=1.0)
        for thread in list(self._threads):
            thread.join(timeout=0.5)
        if not self._closed:
            self._closed = True
            try:
                os.unlink(self.sock_path)
            except OSError:
                pass

    close = stop

    # -- scripting -----------------------------------------------------

    def script(self, method: str, result: Any) -> None:
        """Return *result* for *method*."""
        self.replies[method] = result

    def handler(self, method: str, func: Callable[..., Any]) -> None:
        """Use *func* to compute the result for *method*.

        The callable may accept ``(method, params)``, just ``(params)`` or
        no arguments.
        """
        self.replies[method] = func

    def fault(self, method: str, code: int = 500, message: str = "scripted fault") -> None:
        """Answer *method* with a JSON-RPC error object."""
        self.replies[method] = Fault(code=code, message=message)

    def silent(self, method: str) -> None:
        """Accept *method* but never reply (client must time out)."""
        self.replies[method] = SILENT

    def raw(self, method: str, payload: bytes) -> None:
        """Send *payload* verbatim for *method*."""
        self.replies[method] = Raw(payload)

    def clear(self) -> None:
        with self._lock:
            self.replies.clear()
            self.calls.clear()
            self.request_ids.clear()
            self.max_concurrency = 0

    # -- convenience client --------------------------------------------

    def call(self, method: str, params: list | None = None) -> Any:
        """Act as an RPC client against this fake (for ``ReloadWorker`` tests).

        The request still travels over the real unix-datagram socket, so the
        recorded call log and concurrency counters stay authoritative.
        """
        from console.rpc import UnixDatagramRPC

        return UnixDatagramRPC(self.sock_path, timeout_ms=self.timeout_ms).call(
            method, params
        )

    # -- recorded calls ------------------------------------------------

    @property
    def call_count(self) -> int:
        with self._lock:
            return len(self.calls)

    @property
    def last_call(self) -> Call | None:
        with self._lock:
            return self.calls[-1] if self.calls else None

    def methods(self) -> list[str]:
        with self._lock:
            return [call.method for call in self.calls]

    def calls_for(self, method: str) -> list[Call]:
        with self._lock:
            return [call for call in self.calls if call.method == method]

    # -- server --------------------------------------------------------

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(1 << 20)
            except socket.timeout:
                continue
            except OSError:
                break
            if self._stop.is_set():
                break
            thread = threading.Thread(
                target=self._handle, args=(data, addr), daemon=True
            )
            thread.start()
            with self._lock:
                self._threads = [t for t in self._threads if t.is_alive()]
                self._threads.append(thread)

    def _handle(self, data: bytes, addr: Any) -> None:
        try:
            request = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return  # malformed request: no reply, the client times out
        if not isinstance(request, dict):
            return

        method = request.get("method")
        params = request.get("params")
        request_id = request.get("id")

        with self._lock:
            self.calls.append(Call(method, params, time.monotonic()))
            self.request_ids.append(request_id)
            self.active += 1
            self.max_concurrency = max(self.max_concurrency, self.active)
        try:
            reply = self._make_reply(method, params)
        finally:
            with self._lock:
                self.active -= 1

        if reply is None:
            return
        if isinstance(reply, Raw):
            payload = reply.payload
        elif isinstance(reply, Fault):
            payload = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": reply.code, "message": reply.message},
                }
            ).encode("utf-8")
        else:
            try:
                payload = json.dumps(
                    {"jsonrpc": "2.0", "id": request_id, "result": reply}
                ).encode("utf-8")
            except (TypeError, ValueError):
                payload = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32603, "message": "unserializable scripted result"},
                    }
                ).encode("utf-8")
        try:
            self._sock.sendto(payload, addr)
        except OSError:
            pass

    def _make_reply(self, method: Any, params: Any) -> Any:
        if not isinstance(method, str) or method not in self.replies:
            return Fault(-32601, f"Method not found: {method}")
        try:
            return self._evaluate(self.replies[method], method, params)
        except BaseException as exc:  # noqa: BLE001 - translated below
            return self._exception_to_reply(exc)

    def _evaluate(self, spec: Any, method: str, params: Any) -> Any:
        if spec is SILENT:
            return None
        if isinstance(spec, (Raw, Fault)):
            return spec
        if isinstance(spec, BaseException):
            return self._exception_to_reply(spec)
        if isinstance(spec, type) and issubclass(spec, BaseException):
            return self._exception_to_reply(spec())
        if callable(spec):
            result = self._invoke(spec, method, params)
            if result is SILENT:
                return None
            if isinstance(result, (Raw, Fault)):
                return result
            if isinstance(result, BaseException):
                return self._exception_to_reply(result)
            return result
        return spec

    @staticmethod
    def _invoke(func: Callable[..., Any], method: str, params: Any) -> Any:
        try:
            signature = inspect.signature(func)
        except (TypeError, ValueError):
            return func(method, params)
        parameters = list(signature.parameters.values())
        positional = [
            p
            for p in parameters
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        ]
        has_varargs = any(p.kind == p.VAR_POSITIONAL for p in parameters)
        if has_varargs or len(positional) >= 2:
            return func(method, params)
        if len(positional) == 1:
            return func(params)
        return func()

    @staticmethod
    def _exception_to_reply(exc: BaseException) -> Any:
        if isinstance(exc, (NoReply, KamailioUnavailable)):
            return None
        # RPCFault lives in console.rpc; duck-type so this fixture does not
        # create an import cycle while the implementation is being written.
        code = getattr(exc, "code", None)
        message = getattr(exc, "message", None)
        if isinstance(code, int) and isinstance(message, str):
            return Fault(code=code, message=message)
        return Fault(-32603, str(exc))
