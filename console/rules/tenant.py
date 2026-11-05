"""tenant.py — T4.2/T4.3 validation family for ``tenant_registry``.

Plan §5.2 (tenant_registry row), §6 (imperative copy) and §9 (traceability):

``tenantid_unique``
    ``[A-Z0-9]+``, at most 16 characters, unique across the packed
    ``tenant_registry.tenantid`` values.  Editing a home keeps its own id.

``set_exists_onnet``
    The on-net dispatcher set must exist (error).  A set outside the
    ``10+N`` convention is a warning only.  ``N`` is read from the tenant row
    when possible: the paired PSTN set (``30+N``) is used first, then the
    numeric suffix of ``tenantid`` (``T2`` -> 2).  When neither exists the
    convention is not applicable and no warning is produced.

``pstn_set_exists_or_zero``
    ``0`` means "no trunk" and is always allowed; any other value must exist
    in ``dispatcher.setid``.

``cli_unique``
    The CLI is digits only.  An exact match with another home's landline (a
    ``tenant_landline`` key, or the ``cli`` field of another registry row) is
    an error; a prefix overlap in either direction is a warning.  Rows owned
    by the home being edited never match itself.

``dpid_exists``
    The emergency partition must exist in ``dialplan.dpid``.

``flags_valid``
    Parse ``ih_on+pstn_on[+block_dpid]`` with :func:`console.pack.parse_flags`;
    both switches are 0/1 and a present non-zero ``block_dpid`` must exist in
    ``dialplan.dpid``.  An empty third segment (or ``0``) means "no block".

All database reads go through the descriptors, so the packed layout stays
declared once in ``console/tables.yaml``; a corrupt row is skipped rather than
taking down a form.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any, Mapping

from .. import db, descriptors
from ..pack import parse_flags
from ..validators import ValidationContext, digits

__all__ = ["RULES", "PENDING"]

_TENANTID = re.compile(r"^[A-Z0-9]+$")
_TENANTID_LENGTH = 16
_INTEGER = re.compile(r"^-?\d+$")

#: Documented packed layouts, used only when a descriptor cannot be loaded or
#: a row is corrupt.  These mirror ``console/tables.yaml`` / plan §5.2.
_REGISTRY_KEYS = (
    "tenantid",
    "ds_onnet",
    "ds_pstn",
    "cli",
    "label",
    "dpid",
    "flags",
)
_LANDLINE_KEYS = ("tenantid", "target", "ds_onnet", "ih_on")


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = _text(value)
    if not _INTEGER.fullmatch(text):
        return None
    return int(text)


def _descriptor(table: str, context: ValidationContext):
    """The descriptor for *table* (context's own one when it matches)."""
    descriptor = getattr(context, "descriptor", None)
    if descriptor is not None and getattr(descriptor, "table", None) == table:
        return descriptor
    try:
        return descriptors.load().tables.get(table)
    except (OSError, ValueError):
        return None


def _unpack_packed(row: Mapping[str, Any], descriptor, fallback_keys) -> dict[str, Any]:
    values: dict[str, Any]
    if descriptor is not None:
        try:
            values = descriptors.unpack_row(descriptor, row)
        except (ValueError, KeyError):
            values = {}
    else:
        values = {}

    if not values:
        parts = str(row.get("tvalue") or "").split(",")
        values = dict(zip(fallback_keys, parts))
    values["tprefix"] = row.get("tprefix")
    return values


def _registry_rows(context: ValidationContext) -> list[dict[str, Any]]:
    """Every registry row as descriptor field names (best effort)."""
    if context.conn is None:
        return []
    try:
        rows = db.read_rows(context.conn, "tenant_registry")
    except sqlite3.Error:
        return []
    descriptor = _descriptor("tenant_registry", context)
    return [
        _unpack_packed(row, descriptor, _REGISTRY_KEYS)
        for row in rows
    ]


def _landline_rows(context: ValidationContext) -> list[dict[str, Any]]:
    """Every landline row as descriptor field names (best effort)."""
    if context.conn is None:
        return []
    try:
        rows = db.read_rows(context.conn, "tenant_landline")
    except sqlite3.Error:
        return []
    descriptor = _descriptor("tenant_landline", context)
    return [
        _unpack_packed(row, descriptor, _LANDLINE_KEYS)
        for row in rows
    ]


def _exists(conn: sqlite3.Connection | None, table: str, column: str, number: int) -> bool | None:
    """``True``/``False`` when the lookup ran; ``None`` when it cannot be run."""
    if conn is None:
        return None
    try:
        found = conn.execute(
            f'SELECT 1 FROM "{table}" WHERE "{column}" = ? LIMIT 1',
            (number,),
        ).fetchone()
    except sqlite3.Error:
        return None
    return found is not None


def _self_tenantids(
    context: ValidationContext, registry_rows: list[dict[str, Any]]
) -> set[str]:
    """Tenant ids that belong to the row being edited (never a collision)."""
    ids: set[str] = set()
    submitted = _text(context.values.get("tenantid"))
    if submitted:
        ids.add(submitted)

    key = _text(context.key) if context.mode == "edit" else ""
    if key:
        # The form key is the registry tprefix; a unit context may name the
        # tenant id directly.  Cover both without ever matching a stranger.
        ids.add(key)
        for row in registry_rows:
            if _text(row.get("tprefix")) == key:
                owner = _text(row.get("tenantid"))
                if owner:
                    ids.add(owner)
    return ids


def _convention_n(values: Mapping[str, Any]) -> int | None:
    """Read the convention's ``N`` from the tenant row, when it can be known.

    The paired PSTN set is the most direct source because it is defined as
    ``30+N``; the numeric suffix of ``tenantid`` (``T2`` -> 2) is the fallback.
    A home with neither (for example ``tenantid="HOME"`` and ``ds_pstn=0``)
    has no applicable convention.
    """
    pstn = _as_int(values.get("ds_pstn"))
    if pstn is not None and pstn >= 30:
        return pstn - 30

    tenantid = _text(values.get("tenantid"))
    match = re.fullmatch(r"[A-Za-z]*([0-9]+)", tenantid)
    if match is not None:
        return int(match.group(1))
    return None


# ---------------------------------------------------------------------------
# T4.2 — identity / uniqueness
# ---------------------------------------------------------------------------
def tenantid_unique(
    value: Any, context: ValidationContext
) -> tuple[list[str], list[str]]:
    """``[A-Z0-9]+``, at most 16 chars, unique across the registry."""
    text = _text(value)
    if text == "":
        # Packed fields are already reported required by forms_ui.
        return ([], [])

    errors: list[str] = []
    if not _TENANTID.fullmatch(text):
        errors.append(
            "Use uppercase letters and digits only — for example T2."
        )
    if len(text) > _TENANTID_LENGTH:
        errors.append(
            f"Keep the tenant id to {_TENANTID_LENGTH} characters or fewer "
            f"(got {len(text)})."
        )

    for row in _registry_rows(context):
        if _text(row.get("tenantid")) != text:
            continue
        if context.mode == "edit" and _text(context.key):
            if _text(row.get("tprefix")) == _text(context.key) or text == _text(
                context.key
            ):
                continue
        domain = _text(row.get("tprefix")) or "another home"
        errors.append(
            f"Tenant id {text} is already used by {domain} — choose a unique id."
        )
        break

    return (errors, [])


def cli_unique(
    value: Any, context: ValidationContext
) -> tuple[list[str], list[str]]:
    """Digits only; exact cross-home shadow blocks, prefix overlap warns."""
    errors, _ = digits(value, context)
    if errors:
        return (errors, [])

    cli = _text(value)
    if cli == "":
        return ([], [])

    registry_rows = _registry_rows(context)
    landline_rows = _landline_rows(context)
    self_ids = _self_tenantids(context, registry_rows)
    self_key = _text(context.key) if context.mode == "edit" else ""

    # owner -> the other home's landline that exactly matches this CLI.
    exact: dict[str, str] = {}
    # owner -> the other home's landline that overlaps by prefix.
    overlaps: dict[str, str] = {}

    for row in landline_rows:
        owner = _text(row.get("tenantid"))
        if owner and owner in self_ids:
            continue
        other = _text(row.get("tprefix"))
        if not other:
            continue
        if other == cli:
            exact.setdefault(owner or other, other)
        elif other.startswith(cli) or cli.startswith(other):
            overlaps.setdefault(owner or other, other)

    for row in registry_rows:
        domain = _text(row.get("tprefix"))
        if self_key and domain == self_key:
            continue
        owner = _text(row.get("tenantid"))
        if owner and owner in self_ids:
            continue
        other = _text(row.get("cli"))
        if not other:
            continue
        if other == cli:
            exact.setdefault(owner or domain, other)
        elif other.startswith(cli) or cli.startswith(other):
            overlaps.setdefault(owner or domain, other)

    errors: list[str] = []
    for owner, other in exact.items():
        errors.append(
            f"CLI {cli} is already the landline of {owner} ({other}); "
            "two homes cannot share the same CLI."
        )

    warnings: list[str] = []
    if not errors:
        for owner, other in overlaps.items():
            warnings.append(
                f"CLI {cli} prefix-overlaps {owner}'s landline {other}; "
                "check that calls still reach the intended home."
            )
    return (errors, warnings)


# ---------------------------------------------------------------------------
# T4.3 — cross references and flags
# ---------------------------------------------------------------------------
def set_exists_onnet(
    value: Any, context: ValidationContext
) -> tuple[list[str], list[str]]:
    """On-net set must exist; a non-``10+N`` set is only a warning."""
    number = _as_int(value)
    if number is None:
        if _text(value) == "":
            return ([], [])
        return (["Enter the on-net route-set id as a whole number."], [])

    errors: list[str] = []
    warnings: list[str] = []

    exists = _exists(context.conn, "dispatcher", "setid", number)
    if exists is False:
        errors.append(
            f"Choose an existing on-net route set, or create set {number} "
            "in Route sets first."
        )
        # A missing set is already the actionable problem; do not bury it
        # under a second convention notice.
        return (errors, warnings)

    n = _convention_n(context.values)
    if n is not None:
        expected = 10 + n
        if number != expected:
            warnings.append(
                f"On-net set {number} does not follow the 10+N convention: "
                f"expected set {expected}. A different set is allowed — check "
                "that it is this home's voip-stack."
            )
    return (errors, warnings)


def pstn_set_exists_or_zero(
    value: Any, context: ValidationContext
) -> tuple[list[str], list[str]]:
    """``0`` means no trunk; every other PSTN set must exist."""
    if _text(value) == "":
        return ([], [])

    number = _as_int(value)
    if number is None or number < 0:
        return (
            [
                "Enter the PSTN route-set id as a whole number, or 0 for a "
                "home with no trunk."
            ],
            [],
        )
    if number == 0:
        return ([], [])

    exists = _exists(context.conn, "dispatcher", "setid", number)
    if exists is False:
        return (
            [
                f"Choose an existing PSTN route set, or create set {number} "
                "in Route sets first. Use 0 for a home with no trunk."
            ],
            [],
        )
    return ([], [])


def dpid_exists(
    value: Any, context: ValidationContext
) -> tuple[list[str], list[str]]:
    """The emergency dialplan partition must exist (0 = no profile)."""
    if _text(value) == "":
        return ([], [])

    number = _as_int(value)
    if number is None or number < 0:
        return (["Enter the dialplan partition id as a whole number."], [])
    if number == 0:
        return ([], [])

    exists = _exists(context.conn, "dialplan", "dpid", number)
    if exists is False:
        return (
            [
                f"Choose an existing dialplan partition, or create partition "
                f"{number} in Number rules first."
            ],
            [],
        )
    return ([], [])


def flags_valid(
    value: Any, context: ValidationContext
) -> tuple[list[str], list[str]]:
    """Parse the packed flags and prove a present block partition exists."""
    text = _text(value)
    if text == "":
        # Packed fields are already reported required by forms_ui.
        return ([], [])

    try:
        _ih_on, _pstn_on, block_dpid = parse_flags(text)
    except ValueError as exc:
        return (
            [
                "Enter flags as ih_on+pstn_on[+block_dpid], for example "
                f"1+1+90 — both switches must be 0 or 1 ({exc})."
            ],
            [],
        )

    if block_dpid is not None:
        exists = _exists(context.conn, "dialplan", "dpid", block_dpid)
        if exists is False:
            return (
                [
                    f"Blocked-destination partition {block_dpid} does not "
                    "exist — choose an existing partition, or create partition "
                    f"{block_dpid} in Number rules first. Leave the third flag "
                    "segment out for no block."
                ],
                [],
            )
    return ([], [])


#: Names this module implements.  ``console.rules.__init__`` merges these into
#: :data:`console.validators.REGISTRY` and removes them from the pending set.
RULES = {
    "tenantid_unique": tenantid_unique,
    "set_exists_onnet": set_exists_onnet,
    "pstn_set_exists_or_zero": pstn_set_exists_or_zero,
    "cli_unique": cli_unique,
    "dpid_exists": dpid_exists,
    "flags_valid": flags_valid,
}

PENDING = frozenset(RULES)
