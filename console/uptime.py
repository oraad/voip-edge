"""Human-readable uptime formatting for Kamailio's ``core.uptime`` replies.

Kamailio returns a structured reply with an ``uptime`` value in seconds.  The
console shows that value in the status strip and the Homes summary; format it
once here instead of in each template.  Non-numeric legacy strings are kept
verbatim so older Kamailio replies still render meaningfully.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

__all__ = ["format_uptime", "uptime_seconds"]

_UPTIME_KEYS = ("uptime", "UPTIME", "up_time", "UP_TIME", "uptime_seconds")
_NUMBER_S = re.compile(r"^(\d+)\s*s$", re.IGNORECASE)


def uptime_seconds(value: Any) -> int | None:
    """Return *value* as seconds, or ``None`` when it is not numeric."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return max(0, int(value))
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
        match = _NUMBER_S.match(text)
        if match:
            return int(match.group(1))
    return None


def format_uptime(value: Any) -> str | None:
    """Render seconds as ``2h 10m 23s``.

    Structured replies are unwrapped through the usual Kamailio keys.  A
    non-numeric string is returned unchanged (truncated defensively).
    """
    if value is None:
        return None
    if isinstance(value, Mapping):
        for key in _UPTIME_KEYS:
            if value.get(key) is not None:
                return format_uptime(value[key])
        if len(value) == 1:
            return format_uptime(next(iter(value.values())))
        return str(value)[:80]

    seconds = uptime_seconds(value)
    if seconds is None:
        try:
            text = str(value).strip()
        except Exception:  # pragma: no cover - defensive
            return None
        return text[:80] or None

    days, remainder = divmod(seconds, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, secs = divmod(remainder, 60)

    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"
