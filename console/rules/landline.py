"""landline.py — T4.4 validation family for the packed ``tenant_landline`` table.

Plan §5.2 (tenant_landline row) and the V-LAND-* rows in
``docs/validation-matrix.md``:

``landline_digits``
    The dialed prefix is digits only and at least three characters long.
    Leading zeros are preserved; the form's packed-required rule already owns
    the empty-value case.

``tenant_exists``
    The owning ``tenantid`` must exist in the packed home registry.  The
    descriptor cross-ref in ``tables.yaml`` does the same structural check;
    this rule adds the imperative wording and names the submitted id.

``target_identifier``
    The rewrite target is a safe identifier: letters, digits, underscore,
    dot, dash and tilde.  Comma, plus and whitespace are rejected explicitly
    because comma/plus are packed-record separators.

Longest-prefix conflicts
------------------------
Kamailio's mtree match is longest-prefix, so a new landline that is a prefix
of an existing one (or starts with it) shadows traffic.  This is a cross-row
rule, not a field rule: :func:`check_prefix_conflict` is exported through
``FORM_CHECKS`` and the descriptor-driven form router calls it after field
validation.

* **different home** — always blocked and rendered as the dedicated
  interruption page naming both rows; the same-home checkbox cannot bypass it;
* **same home** — allowed only when ``allow_same_tenant_shadow`` is truthy
  (the interruption page's "Save anyway (same home)" acknowledgement).

The check is table-agnostic from the router's point of view: it inspects the
descriptor and returns ``None`` for every other table.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any, Mapping

from .. import db, descriptors
from ..validators import ValidationContext, Validator

__all__ = [
    "RULES",
    "PENDING",
    "FORM_CHECKS",
    "check_prefix_conflict",
    "landline_prefix_conflict",
]

_TARGET_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.~-]+$")
_TRUTHY = frozenset({"1", "on", "true", "yes"})

#: Field layout used only when a descriptor cannot be loaded / a row is corrupt.
_LANDLINE_KEYS = ("tenantid", "target", "ds_onnet", "ih_on")
_REGISTRY_KEYS = ("tenantid", "ds_onnet", "ds_pstn", "cli", "label", "dpid", "flags")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _truthy(value: Any) -> bool:
    return _text(value).lower() in _TRUTHY


def _descriptor(context: ValidationContext, table: str):
    descriptor = getattr(context, "descriptor", None)
    if descriptor is not None and getattr(descriptor, "table", None) == table:
        return descriptor
    try:
        return descriptors.load().tables.get(table)
    except (OSError, ValueError):
        return None


def _unpack_packed(
    row: Mapping[str, Any], descriptor, fallback_keys: tuple[str, ...]
) -> dict[str, Any]:
    values: dict[str, Any] = {}
    if descriptor is not None:
        try:
            values = descriptors.unpack_row(descriptor, row)
        except (ValueError, KeyError):
            values = {}
    if not values:
        parts = str(row.get("tvalue") or "").split(",")
        values = dict(zip(fallback_keys, parts))
    values["tprefix"] = row.get("tprefix")
    return values


def _registry_rows(context: ValidationContext) -> list[dict[str, Any]]:
    if context.conn is None:
        return []
    try:
        rows = db.read_rows(context.conn, "tenant_registry")
    except (sqlite3.Error, ValueError):
        return []
    descriptor = _descriptor(context, "tenant_registry")
    return [_unpack_packed(row, descriptor, _REGISTRY_KEYS) for row in rows]


def _landline_rows(
    conn: sqlite3.Connection | None, descriptor: Any
) -> list[dict[str, Any]]:
    if conn is None:
        return []
    try:
        rows = db.read_rows(conn, descriptor.table)
    except (sqlite3.Error, ValueError):
        return []
    return [_unpack_packed(row, descriptor, _LANDLINE_KEYS) for row in rows]


# ---------------------------------------------------------------------------
# field rules
# ---------------------------------------------------------------------------
def landline_digits(
    value: Any, context: ValidationContext
) -> tuple[list[str], list[str]]:
    """Digits only, at least three; leading zeros are preserved."""
    text = _text(value)
    if text == "":
        # The packed-record rule (forms_ui) already reports an empty segment.
        return ([], [])
    if not text.isdigit():
        return (
            ["Use digits only — no spaces, dots or separators in a landline."],
            [],
        )
    if len(text) < 3:
        return (
            ["Use at least 3 digits — shorter prefixes cannot be dialled."],
            [],
        )
    return ([], [])


def tenant_exists(
    value: Any, context: ValidationContext
) -> tuple[list[str], list[str]]:
    """The owning ``tenantid`` must exist in the home registry."""
    text = _text(value)
    if text == "" or context.conn is None:
        return ([], [])

    for row in _registry_rows(context):
        if _text(row.get("tenantid")) == text:
            return ([], [])
    return (
        [
            f"Choose an existing home — tenant id {text!r} is not in the home "
            "registry. Onboard the home first."
        ],
        [],
    )


def target_identifier(
    value: Any, context: ValidationContext
) -> tuple[list[str], list[str]]:
    """Safe ring-group/extension identifier, never a packed separator."""
    text = _text(value)
    if text == "":
        return ([], [])

    errors: list[str] = []
    if "," in text:
        errors.append(
            "Remove the comma — targets cannot contain the packed-record "
            "separator."
        )
    if "+" in text:
        errors.append(
            "Remove the plus sign — targets cannot contain the packed-record "
            "separator."
        )
    if text != str(value):
        errors.append("Remove the leading or trailing spaces.")
    if not _TARGET_IDENTIFIER.fullmatch(text):
        errors.append(
            "Use letters, digits, underscore, dot, dash or tilde only "
            "(for example ring-group_1 or 501)."
        )
    return (errors, [])


# ---------------------------------------------------------------------------
# longest-prefix conflict (form-level check)
# ---------------------------------------------------------------------------
def _is_landline_descriptor(descriptor: Any) -> bool:
    if getattr(descriptor, "table", None) == "tenant_landline":
        return True
    names = {getattr(field, "name", None) for field in getattr(descriptor, "fields", ())}
    return {"tprefix", "tenantid", "target", "ds_onnet", "ih_on"} <= names


def _row_owner(values: Mapping[str, Any]) -> str:
    return _text(values.get("tenantid"))


def _row_snapshot(values: Mapping[str, Any], number: str) -> dict[str, str]:
    return {
        "number": number,
        "tenantid": _row_owner(values),
        "target": _text(values.get("target")),
    }


def check_prefix_conflict(
    descriptor: Any,
    values: Mapping[str, Any],
    *,
    conn: sqlite3.Connection | None,
    mode: str,
    existing: Mapping[str, Any],
    key: Any,
) -> dict[str, Any] | None:
    """Return an interruption payload when two landlines shadow one another.

    ``None`` means no conflict, or a same-home conflict that the operator
    explicitly acknowledged with ``allow_same_tenant_shadow``.  The form router
    renders ``interruption.html`` when the returned mapping is not ``None``.
    """
    if not _is_landline_descriptor(descriptor):
        return None

    number = _text(values.get("tprefix"))
    if number == "" or not number.isdigit():
        return None

    tenant = _row_owner(values)
    acknowledged = _truthy(values.get("allow_same_tenant_shadow"))
    edit_key = _text(key) if mode == "edit" else ""

    conflicts: list[dict[str, str]] = []
    for row in _landline_rows(conn, descriptor):
        other_number = _text(row.get("tprefix"))
        if not other_number or other_number == "":
            continue
        if edit_key and other_number == edit_key:
            # Editing a row never conflicts with the row itself.
            continue
        if not (number.startswith(other_number) or other_number.startswith(number)):
            continue
        conflicts.append(_row_snapshot(row, other_number))

    if not conflicts:
        return None

    # A cross-home clash is always the most severe finding; surface it even if
    # a same-home row also overlaps.
    conflicts.sort(key=lambda item: (item["tenantid"] == tenant, len(item["number"]), item["number"]))
    other = conflicts[0]
    same_tenant = bool(tenant) and other["tenantid"] == tenant

    if same_tenant and acknowledged:
        return None

    submitted = _row_snapshot(values, number)
    if same_tenant:
        message = (
            f"Landline {number} overlaps {other['number']} in the same home. "
            "Tick allow_same_tenant_shadow to keep both — the longest matching "
            "prefix wins."
        )
    else:
        message = (
            f"Landline {number} would shadow {other['number']} owned by "
            f"{other['tenantid'] or 'another home'}; cross-tenant (cross-home) "
            "shadowing is not allowed."
        )

    return {
        "errors": {"tprefix": [message]},
        "interruption": {
            "same_tenant": same_tenant,
            "existing": other,
            "submitted": submitted,
            "message": message,
        },
    }


#: Alias matching the plan/matrix rule name; this is a form-level check, not a
#: field validator (no ``tables.yaml`` field names it).
landline_prefix_conflict = check_prefix_conflict


#: Names this module implements; the package merge removes them from
#: :data:`console.validators.PENDING_VALIDATORS`.
RULES: dict[str, Validator] = {
    "landline_digits": landline_digits,
    "tenant_exists": tenant_exists,
    "target_identifier": target_identifier,
}

PENDING = frozenset(RULES)

#: Form-level checks the descriptor-driven router invokes after field rules.
FORM_CHECKS = (check_prefix_conflict,)
