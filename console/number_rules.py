"""Pure pattern builders for the simplified number-rules editor."""
from __future__ import annotations

import re

__all__ = [
    "exact_pattern",
    "parse_entries",
    "prefix_pattern",
    "range_pattern",
]

_TOKEN_SPLIT = re.compile(r"[\s,]+")
_DIGITS = re.compile(r"^\d+$")
_MAX_DIGITS = 15


def parse_entries(text: str) -> list[str]:
    """Split an operator list into unique digit strings."""
    entries: list[str] = []
    for token in _TOKEN_SPLIT.split((text or "").strip()):
        if not token:
            continue
        if not _DIGITS.fullmatch(token):
            raise ValueError(f"{token!r} is not a number")
        if len(token) > _MAX_DIGITS:
            raise ValueError(f"{token!r} is longer than {_MAX_DIGITS} digits")
        if token not in entries:
            entries.append(token)
    return entries


def _alternation(values: list[str], *, end: bool) -> str:
    if not values:
        raise ValueError("at least one number is required")
    for value in values:
        if not _DIGITS.fullmatch(value):
            raise ValueError(f"{value!r} is not a number")
    if len(values) == 1:
        return f"^{re.escape(values[0])}{'$' if end else ''}"
    body = "|".join(re.escape(value) for value in values)
    return f"^({body}){'$' if end else ''}"


def exact_pattern(numbers: list[str]) -> str:
    """Match one of the numbers exactly."""
    return _alternation(list(dict.fromkeys(numbers)), end=True)


def prefix_pattern(prefixes: list[str]) -> str:
    """Match numbers that start with one of the prefixes."""
    return _alternation(list(dict.fromkeys(prefixes)), end=False)


def _range_core(low: str, high: str) -> str:
    if low == high:
        return re.escape(low)
    if len(low) == 1:
        return f"[{low}-{high}]"
    if low[0] == high[0]:
        return re.escape(low[0]) + _range_core(low[1:], high[1:])

    width = len(low) - 1
    parts: list[str] = []
    if low[1:] == "0" * width:
        parts.append(f"{low[0]}[0-9]{{{width}}}")
    else:
        parts.append(
            re.escape(low[0]) + _range_core(low[1:], "9" * width)
        )
    first_middle = int(low[0]) + 1
    last_middle = int(high[0]) - 1
    if first_middle <= last_middle:
        if first_middle == last_middle:
            parts.append(f"[{first_middle}][0-9]{{{width}}}")
        else:
            parts.append(f"[{first_middle}-{last_middle}][0-9]{{{width}}}")
    if high[1:] == "9" * width:
        parts.append(f"{high[0]}[0-9]{{{width}}}")
    else:
        parts.append(
            re.escape(high[0]) + _range_core("0" * width, high[1:])
        )
    unique = list(dict.fromkeys(parts))
    if len(unique) == 1:
        return unique[0]
    return "(?:" + "|".join(unique) + ")"


def range_pattern(start: str, end: str) -> str:
    """Match every fixed-width decimal number from *start* through *end*."""
    start = str(start or "")
    end = str(end or "")
    if (
        not start
        or not end
        or not _DIGITS.fullmatch(start)
        or not _DIGITS.fullmatch(end)
    ):
        raise ValueError("range bounds must be digits")
    if len(start) != len(end):
        raise ValueError("range bounds must have the same length")
    if len(start) > _MAX_DIGITS:
        raise ValueError(f"range is longer than {_MAX_DIGITS} digits")
    if int(start) > int(end):
        raise ValueError("range start must not be after the end")
    return f"^{_range_core(start, end)}$"
