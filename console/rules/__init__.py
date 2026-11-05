"""console.rules — per-family validator modules (T4.2–T4.7).

Each family lives in its own module to keep the fan-out parallel-safe:

    RULES: dict[str, Validator]      # name -> fn(value, context) -> (errors, warnings)
    PENDING: frozenset[str]          # names this module now implements

Importing this package merges every present module into
``console.validators.REGISTRY`` and removes its names from
``console.validators.PENDING_VALIDATORS``. Missing family modules are simply
skipped (they land later without touching validators.py).
"""
from __future__ import annotations

import importlib

from .. import validators as _v

FAMILIES = ("tenant", "landline", "dispatcher", "trusted", "dialplan")


def _merge() -> None:
    pending = set(_v.PENDING_VALIDATORS)
    for name in FAMILIES:
        try:
            mod = importlib.import_module(f"console.rules.{name}")
        except ImportError:
            continue
        for rule_name, fn in getattr(mod, "RULES", {}).items():
            _v.REGISTRY[rule_name] = fn
        pending -= set(getattr(mod, "PENDING", frozenset()))
        _v.PENDING_VALIDATORS = frozenset(pending)  # type: ignore[misc]


_merge()
