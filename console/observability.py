"""observability.py — structured, redacted JSON log lines (plan §6/§9 T9).

Every operational event the console emits (reload outcome, lifecycle
transition, authentication failure, read-only refusal) is written as **one
JSON object per line on stdout**::

    {"ts": "2026-11-05T18:41:02.123456Z", "event": "reload.outcome",
     "fields": {"table": "tenantdom", "status": "applied"}}

The formatter is deliberately small and dependency-free.  Two rules protect
operator secrets:

* a field whose name looks sensitive (token, password, cookie, csrf,
  authorization, session, …) is replaced with ``"[redacted]"``;
* the configured console token/secret is scrubbed from any string value, and
  ``Bearer <token>`` values are redacted as well.

No function in this module raises because of an unserialisable field: unknown
objects fall back to their ``str()``.  A logging failure must never break a
reload or a lifecycle operation.
"""
from __future__ import annotations

import json
import re
import sys
import threading
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from . import settings as settings_module

__all__ = [
    "REDACTED",
    "SENSITIVE_KEY_PARTS",
    "format_event",
    "log_event",
    "log_reload_outcome",
    "log_lifecycle_transition",
    "log_auth_failure",
    "log_readonly_refusal",
    "redact_fields",
]

REDACTED = "[redacted]"

# Substrings, not exact names: ``X-CSRF-Token``, ``session_cookie`` and
# ``apiKey`` all have to be caught.
SENSITIVE_KEY_PARTS = (
    "token",
    "password",
    "passwd",
    "secret",
    "cookie",
    "csrf",
    "authorization",
    "auth",
    "api_key",
    "apikey",
    "session",
    "credential",
)

_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_KV_SECRET_RE = re.compile(
    r"(?i)\b(token|password|passwd|secret|cookie|csrf|api[_-]?key)"
    r"(\s*[=:]\s*)([^\s&,;]+)"
)
_WRITE_LOCK = threading.Lock()


def _is_sensitive_key(key: Any) -> bool:
    name = str(key).lower().replace("-", "_")
    return any(part in name for part in SENSITIVE_KEY_PARTS)


def _configured_secrets() -> tuple[str, ...]:
    """Current console secrets, longest first so overlapping values scrub fully."""
    values: list[str] = []
    for candidate in (
        getattr(settings_module.settings, "token", ""),
        getattr(settings_module.settings, "secret", ""),
    ):
        value = str(candidate or "")
        if len(value) >= 4 and value not in values:
            values.append(value)
    return tuple(sorted(values, key=len, reverse=True))


def _scrub(value: str) -> str:
    """Remove configured secrets and bearer credentials from a string."""
    scrubbed = value
    for secret in _configured_secrets():
        if secret in scrubbed:
            scrubbed = scrubbed.replace(secret, REDACTED)
    scrubbed = _BEARER_RE.sub("Bearer " + REDACTED, scrubbed)
    scrubbed = _KV_SECRET_RE.sub(lambda m: m.group(1) + m.group(2) + REDACTED, scrubbed)
    return scrubbed


def redact_fields(value: Any) -> Any:
    """Recursively redact sensitive keys and values in *value*."""
    if isinstance(value, Mapping):
        return {
            str(key): (
                REDACTED if _is_sensitive_key(key) else redact_fields(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_fields(item) for item in value]
    if isinstance(value, str):
        return _scrub(value)
    return value


def _default(value: Any) -> str:
    """JSON fallback for values json cannot serialise (never raises)."""
    try:
        return _scrub(str(value))
    except Exception:  # pragma: no cover - defensive only
        return "<unserialisable>"


def _utc_timestamp(ts: datetime | None = None) -> str:
    moment = ts or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


def format_event(
    name: str,
    fields: Mapping[str, Any] | None = None,
    *,
    ts: datetime | None = None,
) -> str:
    """Return one JSON line: ``{"ts", "event", "fields"}`` with redaction.

    ``name`` is an event identifier such as ``"reload.outcome"``; it is never
    redacted itself.  The returned string has no trailing newline.
    """
    record = {
        "ts": _utc_timestamp(ts),
        "event": str(name),
        "fields": redact_fields(dict(fields or {})),
    }
    return json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=_default)


def log_event(name: str, **fields: Any) -> None:
    """Write one structured JSON event to stdout.

    Logging is best-effort: an encoding/IO problem is swallowed so callers in
    the reload worker and lifecycle paths can never be interrupted by it.
    """
    line = format_event(name, fields)
    try:
        with _WRITE_LOCK:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
    except Exception:  # pragma: no cover - stdout closed / broken pipe
        return


# ---------------------------------------------------------------------------
# domain helpers — callers use these so event names stay stable
# ---------------------------------------------------------------------------
def log_reload_outcome(
    *,
    table: str,
    status: str,
    rpc: str | None = None,
    detail: str | None = None,
) -> None:
    """Log a reload worker outcome (``applied`` / ``scheduled`` / ``failed``)."""
    fields: dict[str, Any] = {"table": table, "status": status}
    if rpc is not None:
        fields["rpc"] = rpc
    if detail is not None:
        fields["detail"] = detail
    log_event("reload.outcome", **fields)


def log_lifecycle_transition(
    *,
    op: str,
    domain: str,
    state: str,
    intent_id: str | None = None,
    detail: str | None = None,
) -> None:
    """Log one lifecycle state transition (audit rows remain the source of truth)."""
    fields: dict[str, Any] = {"op": op, "domain": domain, "state": state}
    if intent_id is not None:
        fields["intent_id"] = intent_id
    if detail is not None:
        fields["detail"] = detail
    log_event("lifecycle.transition", **fields)


def log_auth_failure(*, reason: str, client: str | None = None, path: str | None = None) -> None:
    """Log a failed authentication/CSRF attempt without any credential value."""
    fields: dict[str, Any] = {"reason": reason}
    if client is not None:
        fields["client"] = client
    if path is not None:
        fields["path"] = path
    log_event("auth.failure", **fields)


def log_readonly_refusal(*, method: str, path: str, client: str | None = None) -> None:
    """Log a write refused by ``CONSOLE_READONLY`` (method + path only)."""
    fields: dict[str, Any] = {"method": method, "path": path}
    if client is not None:
        fields["client"] = client
    log_event("readonly.refused", **fields)
