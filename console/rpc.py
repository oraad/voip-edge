"""rpc.py — JSON-RPC 2.0 client for Kamailio's ``jsonrpcs`` datagram transport.

The edge exposes its RPC channel as a UNIX **datagram** socket
(``modparam("jsonrpcs", "dgram_socket", "/run/kamailio/kamailio_rpc.sock")``).
Each call is a self-contained request/reply exchange; this client uses one
socket per call (bound to an abstract address so the server can reply) and a
strict timeout.

Typed errors (plan §4 taxonomy):

* transport problems (missing socket, timeout, short/garbled reply) raise
  :class:`console.errors.KamailioUnavailable`;
* a JSON-RPC ``error`` object raises :class:`RPCFault` (a ``ConsoleError``)
  carrying the method, code and Kamailio's verbatim message.

``flatten_htable_dump`` normalises ``htable.dump``'s bucket-shaped output.
"""
from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import uuid
from typing import Any

from console.errors import ConsoleError, KamailioUnavailable
from console.rpc_lock import RPC_LOCK

__all__ = ["RPCFault", "UnixDatagramRPC", "flatten_htable_dump"]

# Large enough for the configured 256 KiB dgram_max_size plus envelope.
_MAX_REPLY = 4 * 1024 * 1024


class RPCFault(ConsoleError):
    """A JSON-RPC ``error`` reply from Kamailio.

    ``message`` is Kamailio's text verbatim (surface it unchanged in toasts,
    per plan §5.8/§6).  ``code`` is the JSON-RPC error code.
    """

    def __init__(self, method: str, code: int, message: str) -> None:
        self.method = method
        self.code = code
        self.message = message
        super().__init__(f"{method} failed ({code}): {message}")


def _bind_client_socket(sock: socket.socket) -> str | None:
    """Bind the client so Kamailio can send the reply back.

    Linux autobinds an abstract address for ``bind("")`` (and for an explicit
    ``\\0``-prefixed name), which needs no filesystem cleanup.  Older/safer
    platforms get a temporary path, returned so the caller can unlink it.
    """
    try:
        sock.bind(b"\x00voip-console-rpc-" + uuid.uuid4().hex.encode("ascii"))
        return None
    except OSError:
        pass
    path = tempfile.mktemp(prefix="voip-console-rpc-", suffix=".sock")
    sock.bind(path)
    return path


class UnixDatagramRPC:
    """Synchronous JSON-RPC 2.0 client over an AF_UNIX SOCK_DGRAM socket."""

    def __init__(self, sock_path: str, timeout_ms: int = 2000) -> None:
        self.sock_path = str(sock_path)
        self.timeout_ms = int(timeout_ms)
        self._id_lock = threading.Lock()
        self._next_id = 0

    def _request_id(self) -> int:
        with self._id_lock:
            self._next_id += 1
            return self._next_id

    def call(self, method: str, params: list | None = None) -> Any:
        """Invoke *method* and return the decoded JSON-RPC ``result``.

        Raises :class:`KamailioUnavailable` for any transport failure and
        :class:`RPCFault` when Kamailio answers with an ``error`` object.
        """
        request: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
            "id": self._request_id(),
        }
        if params is not None:
            request["params"] = params
        payload = json.dumps(request).encode("utf-8")

        reply_path: str | None = None
        sock: socket.socket | None = None
        try:
            # Serialize with the reload worker and capability probe.
            with RPC_LOCK:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                sock.settimeout(self.timeout_ms / 1000.0)
                reply_path = _bind_client_socket(sock)
                sock.connect(self.sock_path)
                sock.send(payload)
                raw = sock.recv(_MAX_REPLY)
        except OSError as exc:
            raise KamailioUnavailable(
                f"kamailio RPC unavailable at {self.sock_path}: {exc}"
            ) from exc
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            if reply_path is not None:
                try:
                    os.unlink(reply_path)
                except OSError:
                    pass

        try:
            reply = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise KamailioUnavailable(
                f"kamailio RPC returned an invalid reply ({len(raw)} bytes) "
                f"from {self.sock_path}"
            ) from exc

        if not isinstance(reply, dict):
            raise KamailioUnavailable(
                f"kamailio RPC returned {type(reply).__name__}, expected an object"
            )

        error = reply.get("error")
        if error is not None:
            if not isinstance(error, dict):
                raise KamailioUnavailable(
                    f"kamailio RPC returned a malformed error object: {error!r}"
                )
            try:
                code = int(error.get("code", -32603))
            except (TypeError, ValueError):
                code = -32603
            message = error.get("message", "unspecified JSON-RPC error")
            raise RPCFault(method, code, str(message))

        if "result" not in reply:
            raise KamailioUnavailable(
                "kamailio RPC reply has neither result nor error"
            )
        return reply["result"]


def flatten_htable_dump(dump: Any) -> dict[str, dict[str, Any]]:
    """Flatten ``htable.dump`` into ``{key: {"value": str, "expires": int|None}}``.

    Kamailio returns a list of buckets::

        [{"entry": 0, "size": 2,
          "slot": [{"item": {"name": "aor", "value": "flow", "expires": 1740}},
                   {"item": {"name": "aor2", "value": 42}}]},
         {"entry": 1, "size": 1,
          "slot": {"item": {"name": "aor3", "value": "x"}}}]

    ``slot`` is a plain dict rather than a list when ``size == 1``; both forms
    are handled.  ``value`` may arrive as a string or a number and is always
    stringified.  ``expires`` is normalised to an int, or ``None`` when absent
    (regcache has no autoexpire — plan §3.8).
    """
    if dump is None:
        return {}
    if isinstance(dump, dict):
        if "entries" in dump:
            dump = dump["entries"]
        elif "slot" in dump or "entry" in dump:
            dump = [dump]
        else:
            return {}
    if not isinstance(dump, list):
        return {}

    flat: dict[str, dict[str, Any]] = {}
    for bucket in dump:
        if not isinstance(bucket, dict):
            continue
        slot = bucket.get("slot")
        if isinstance(slot, list):
            wrappers = slot
        elif isinstance(slot, dict):
            wrappers = [slot]
        else:
            continue

        for wrapper in wrappers:
            if not isinstance(wrapper, dict):
                continue
            item = wrapper.get("item", wrapper)
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if name is None:
                continue
            value = item.get("value")
            expires = item.get("expires")
            if expires is None or expires == "":
                expires = None
            else:
                try:
                    expires = int(expires)
                except (TypeError, ValueError):
                    expires = None
            flat[str(name)] = {
                "value": "" if value is None else str(value),
                "expires": expires,
            }
    return flat
