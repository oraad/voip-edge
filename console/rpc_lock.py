"""Process-wide serialization for Kamailio JSON-RPC calls.

Kamailio's ``*_reload`` handlers are not safe to run concurrently for the same
table.  The console has exactly one reload worker for that reason, but the
capability probe also calls reload methods (to prove they exist) and the live
dashboard issues read calls.  This lock makes every call on the single-process
console mutually exclusive, regardless of which component owns the client.

``RLock`` keeps nested use safe: a caller may hold it around a higher-level
operation and still call :class:`console.rpc.UnixDatagramRPC`.
"""
from __future__ import annotations

import threading

RPC_LOCK = threading.RLock()
