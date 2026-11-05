"""capabilities.py — startup capability probe with a short TTL cache (plan T1/T9).

The console must work when Kamailio is unreachable, so probing is *advisory*:
it returns a plain ``dict[str, bool]`` and never raises for transport or RPC
problems.  The composition root starts it on a daemon thread at startup (never
blocking the HTTP listener) and the status strip / audit page read the cached
result.

Probed capabilities
-------------------
``jsonrpcs`` / ``reachable``
    The unix-datagram JSON-RPC channel answered at all.
``core.uptime``
    The health probe used by the global status region (the uptime text is kept
    in :func:`last_uptime` too).
``htable.dump``
    The regcache dump used by the live panel and park purge gate.
``htable.delete``
    The purge primitive.  A failed probe is treated conservatively: park and
    destroy must call :func:`require_capability` and are refused rather than
    starting a purge that cannot delete keys.
``mtree.reload``, ``dispatcher.reload``, ``dialplan.reload``,
``permissions.trustedReload``
    Every reload RPC named by ``tables.yaml``.  A successful call proves the
    method is present; the call itself is Kamailio's normal idempotent reload.

An RPC fault (JSON-RPC ``error``) proves ``jsonrpcs`` is reachable but marks
the individual capability ``False``.  The probe is intentionally conservative:
"the handler answered" is not the same as "this operation is usable end to
end" — a purge that cannot delete must refuse the lifecycle op.
"""
from __future__ import annotations

import time
from typing import Any, Mapping

from .errors import KamailioUnavailable
from .rpc import RPCFault

__all__ = [
    "CAPABILITY_KEYS",
    "DEFAULT_TTL",
    "cached",
    "clear_cache",
    "last_probe_at",
    "last_uptime",
    "missing_capabilities",
    "probe",
    "require_capability",
]

DEFAULT_TTL = 60.0

# Metadata keys are not individual capabilities (they describe the result).
META_KEYS = frozenset({"ok", "aggregate", "reachable", "unknown", "jsonrpcs"})

_FALLBACK_CHECKS: tuple[tuple[str, list[Any]], ...] = (
    ("core.uptime", []),
    ("htable.dump", ["regcache"]),
    ("htable.delete", ["regcache", "__console_capability_probe__"]),
    ("mtree.reload", ["tenantdom"]),
    ("dispatcher.reload", []),
    ("dialplan.reload", []),
    ("permissions.trustedReload", []),
)

CAPABILITY_KEYS: tuple[str, ...] = (
    "core.uptime",
    "htable.dump",
    "htable.delete",
    "mtree.reload",
    "dispatcher.reload",
    "dialplan.reload",
    "permissions.trustedReload",
)

# module cache: (rpc identity, monotonic timestamp, result copy)
_cache: tuple[int | None, float, dict[str, bool]] | None = None
_last_probe_at_value: float | None = None
_last_uptime_value: str | None = None


def _checks() -> tuple[tuple[str, list[Any]], ...]:
    """Reload checks derived from ``tables.yaml`` (descriptors stay authoritative)."""
    try:
        from . import descriptors

        tableset = descriptors.load()
    except Exception:
        return _FALLBACK_CHECKS

    seen: set[str] = set()
    checks: list[tuple[str, list[Any]]] = [
        ("core.uptime", []),
        ("htable.dump", ["regcache"]),
        ("htable.delete", ["regcache", "__console_capability_probe__"]),
    ]
    seen.update(method for method, _ in checks)
    for descriptor in tableset.tables.values():
        method = descriptor.reload.rpc
        if method in seen:
            continue
        seen.add(method)
        checks.append((method, list(descriptor.reload.params)))
    return tuple(checks)


def _uptime_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        for key in ("uptime", "UPTIME", "up_time", "UP_TIME", "uptime_seconds"):
            if value.get(key) is not None:
                return str(value[key])[:80]
        return None
    try:
        text = str(value).strip()
    except Exception:  # pragma: no cover - defensive
        return None
    return text[:80] or None


def _blank_result() -> dict[str, bool]:
    result = {name: False for name, _ in _checks()}
    result.update(
        {
            "jsonrpcs": False,
            "reachable": False,
            "unknown": True,
            "ok": False,
            "aggregate": False,
        }
    )
    return result


def _run_probe(rpc: Any) -> dict[str, bool]:
    global _last_probe_at_value, _last_uptime_value

    checks = _checks()
    results: dict[str, bool] = {}
    reachable = False
    _last_uptime_value = None

    if rpc is None:
        result = _blank_result()
        _last_probe_at_value = time.time()
        return result

    from .rpc_lock import RPC_LOCK

    for index, (method, params) in enumerate(checks):
        try:
            with RPC_LOCK:
                reply = rpc.call(method, params)
        except RPCFault:
            # The channel answered; the individual capability did not pass.
            reachable = True
            results[method] = False
        except KamailioUnavailable:
            # No point waiting out one timeout per method on a dead socket.
            for remaining, _ in checks[index:]:
                results[remaining] = False
            break
        except Exception:
            results[method] = False
        else:
            reachable = True
            results[method] = True
            if method == "core.uptime":
                _last_uptime_value = _uptime_text(reply)

    # Any capability not reached above is unavailable.
    for method, _ in checks:
        results.setdefault(method, False)

    ok = reachable and all(
        value for method, value in results.items() if method not in {"jsonrpcs"}
    )
    results["jsonrpcs"] = reachable
    results["reachable"] = reachable
    results["unknown"] = not reachable
    results["ok"] = bool(ok)
    results["aggregate"] = bool(ok)
    _last_probe_at_value = time.time()
    return results


def probe(
    rpc: Any,
    *,
    ttl: float = DEFAULT_TTL,
    clock: Any = None,
) -> dict[str, bool]:
    """Return capability flags for *rpc*, reusing a cached result within *ttl*.

    The function never raises for an unreachable edge: an absent socket yields
    ``reachable=False``, ``unknown=True`` and every capability ``False``.
    """
    global _cache

    read_clock = time.monotonic if clock is None else clock
    now = read_clock()
    rpc_key = id(rpc) if rpc is not None else None
    if _cache is not None:
        cached_key, cached_at, cached_result = _cache
        if (
            cached_key == rpc_key
            and max(0.0, float(ttl)) > 0
            and (now - cached_at) < float(ttl)
        ):
            return dict(cached_result)

    result = _run_probe(rpc)
    _cache = (rpc_key, now, dict(result))
    return dict(result)


def cached(*, ttl: float = DEFAULT_TTL, clock: Any = None) -> dict[str, bool] | None:
    """Return the most recent result when it is still fresh, else ``None``."""
    if _cache is None:
        return None
    read_clock = time.monotonic if clock is None else clock
    _cached_key, cached_at, result = _cache
    if max(0.0, float(ttl)) > 0 and (read_clock() - cached_at) < float(ttl):
        return dict(result)
    return None


def clear_cache() -> None:
    """Drop the cache (tests and explicit operator refresh)."""
    global _cache, _last_probe_at_value, _last_uptime_value
    _cache = None
    _last_probe_at_value = None
    _last_uptime_value = None


def last_probe_at() -> float | None:
    """Unix timestamp of the last probe attempt (``None`` when never probed)."""
    return _last_probe_at_value


def last_uptime() -> str | None:
    """Last ``core.uptime`` text seen by a successful probe, if any."""
    return _last_uptime_value


def missing_capabilities(result: Mapping[str, Any] | None) -> list[str]:
    """Names of individual capabilities that are not ``True`` in *result*."""
    if not result:
        return list(CAPABILITY_KEYS)
    return [
        name
        for name in CAPABILITY_KEYS
        if result.get(name) is not True
    ]


def require_capability(
    name: str,
    capabilities: Mapping[str, Any] | Any | None = None,
    *,
    rpc: Any = None,
) -> None:
    """Refuse an operation unless *name* is proven available.

    Callers that already hold a probe result pass it as *capabilities*; the
    startup composition root can pass an RPC client (or omit both to use the
    module cache).  Raises :class:`console.errors.KamailioUnavailable` with an
    operator-facing message when the capability is missing or unknown — which
    is exactly what park/destroy need before touching the regcache.
    """
    caps: Mapping[str, Any] | None
    if isinstance(capabilities, Mapping):
        caps = capabilities
    elif capabilities is not None:
        caps = probe(capabilities)
    elif rpc is not None:
        caps = probe(rpc)
    else:
        caps = cached()

    if caps is None:
        raise KamailioUnavailable(
            f"capability {name!r} is unknown (edge unreachable) — refusing the operation"
        )
    if caps.get("reachable") is False or caps.get("unknown") is True:
        raise KamailioUnavailable(
            f"capability {name!r} is unknown (edge unreachable) — refusing the operation"
        )
    if caps.get(name) is not True:
        raise KamailioUnavailable(
            f"capability {name!r} is unavailable — refusing the operation"
        )
