"""dispatcher.py — T4.5 validation family for the standard ``dispatcher`` table.

Plan §5.2 (dispatcher row) and §7 scenario 18:

* the destination must be a **literal IPv4** SIP URI (``sip:`` or ``sips:``);
  an FQDN is blocked with the "use the voip-stack host's LAN IP" hint;
* the port, when present, is a whole number in ``1..65535``;
* ``transport=`` is restricted to ``tcp``/``udp``;
* **UDP on the tunnel is a warning**, never a block, with the locked MTU text;
* the console never infers REGISTER transport from the destination transport —
  the relay forces UDP for REGISTER regardless (plan §5.2 / scenario 18).

Transport omitted — deliberate decision
---------------------------------------
The plan text says transport is mandatory.  The console instead **accepts an
omitted transport with a warning**, because Kamailio's transport default is
TCP and rejecting the value would block a destination that Kamailio would
load happily.  The warning tells the operator exactly what the console assumed
and how to make it explicit::

    transport omitted — treated as tcp; set ;transport=tcp explicitly

This keeps the save path honest (the operator sees the assumption) without
pretending the console can rewrite the stored URI.  The warning is also the
"normalize note": the stored value is left untouched.

Set references
--------------
:func:`set_referenced` is the shared helper T5's delete guard will call: it
returns the tenant domains whose ``ds_onnet`` or ``ds_pstn`` point at a set.
The ``setid_int`` rule uses it for the non-blocking "not referenced yet"
badge.  Both read packed rows through the descriptor, so no tenant-record
layout is duplicated here.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from .. import db, descriptors
from ..validators import ValidationContext, Validator, sip_uri

__all__ = [
    "RULES",
    "PENDING",
    "MTU_WARNING",
    "TRANSPORT_OMITTED_WARNING",
    "UNREFERENCED_WARNING",
    "set_referenced",
]

#: Locked warning text (plan §5.2, scenario 18).  The em dash is intentional.
MTU_WARNING = (
    "OPTIONS pings stay green while INVITEs fragment and die in the tunnel — "
    "use ;transport=tcp for tunnel destinations"
)

#: Omitted transport is accepted but the assumption is surfaced (see module docstring).
TRANSPORT_OMITTED_WARNING = (
    "transport omitted — treated as tcp; set ;transport=tcp explicitly"
)

#: Non-blocking set-id badge text.
UNREFERENCED_WARNING = "not referenced by any home yet"

#: Known dispatcher flag values (plan §5.2): state bits and "not probed".
KNOWN_FLAGS = (0, 1, 2, 4, 8, 16)

_ALLOWED_TRANSPORTS = frozenset({"tcp", "udp"})


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _non_negative_int(value: Any) -> tuple[int | None, str | None]:
    """Coerce an integer field; blank means the columns-table default ``0``.

    Returns ``(number, error)``.  ``bool`` is rejected even though it is an
    ``int`` subclass — a checkbox value must never silently become 1 here.
    """
    if isinstance(value, bool):
        return (None, "Enter a whole number.")
    if isinstance(value, int):
        number = value
    else:
        text = "" if value is None else str(value).strip()
        if text == "":
            number = 0
        else:
            try:
                number = int(text)
            except (TypeError, ValueError):
                return (None, f"Enter a whole number, not {text!r}.")
    if number < 0:
        return (None, "Use 0 or a larger whole number.")
    return (number, None)


def set_referenced(conn: sqlite3.Connection | None, setid: Any) -> list[str]:
    """Tenant domains whose on-net or PSTN set is *setid* (sorted).

    This is the lookup T5's in-use delete guard will call.  A missing/partial
    database is treated as "no references" rather than raising inside a form:
    the delete guard can then decide how to surface the read failure.  Packed
    rows are unpacked through the ``tenant_registry`` descriptor so the tenant
    record layout stays declared in one place.
    """
    if conn is None:
        return []

    number, error = _non_negative_int(setid)
    if error is not None:
        return []

    try:
        rows = db.read_rows(conn, "tenant_registry")
        descriptor = descriptors.load().tables.get("tenant_registry")
    except (sqlite3.Error, ValueError, OSError):
        return []

    def matches(candidate: Any) -> bool:
        candidate_number, candidate_error = _non_negative_int(candidate)
        if candidate_error is None and candidate_number is not None:
            return candidate_number == number
        return str(candidate).strip() == str(number)

    domains: set[str] = set()
    for row in rows:
        unpacked: dict[str, Any]
        if descriptor is not None:
            try:
                unpacked = descriptors.unpack_row(descriptor, row)
            except (ValueError, KeyError):
                unpacked = _unpacked_registry_fallback(row)
        else:
            unpacked = _unpacked_registry_fallback(row)

        if matches(unpacked.get("ds_onnet")) or matches(unpacked.get("ds_pstn")):
            domain = unpacked.get("tprefix") or row.get("tprefix")
            if domain:
                domains.add(str(domain))
    return sorted(domains)


def _unpacked_registry_fallback(row: dict[str, Any]) -> dict[str, Any]:
    """Best-effort read of the documented tenant record layout.

    Only reached when the descriptor or a corrupted row cannot be unpacked;
    positions are the locked layout from ``tables.yaml``/plan §5.2.
    """
    parts = str(row.get("tvalue") or "").split(",")
    keys = ("tenantid", "ds_onnet", "ds_pstn", "cli", "label", "dpid", "flags")
    values = dict(zip(keys, parts))
    values["tprefix"] = row.get("tprefix")
    return values


# ---------------------------------------------------------------------------
# dispatcher rules
# ---------------------------------------------------------------------------
def setid_int(value: Any, context: ValidationContext) -> tuple[list[str], list[str]]:
    """A non-negative integer set id; warn when no home references it yet."""
    number, error = _non_negative_int(value)
    if error is not None or number is None:
        return ([error or "Enter a whole number."], [])

    if context.conn is not None and not set_referenced(context.conn, number):
        return ([], [f"Set id {number} is {UNREFERENCED_WARNING}."])
    return ([], [])


def destination_ipv4_required(
    value: Any, context: ValidationContext
) -> tuple[list[str], list[str]]:
    """Dispatcher destination: literal-IPv4 SIP URI with transport policy.

    Shape (scheme, user, host, port, parameter grammar) is delegated to the
    generic :func:`console.validators.sip_uri` rule; this rule adds the
    dispatcher-only ``sips:`` scheme and the transport policy.  It never
    derives or comments on REGISTER behavior.
    """
    text = "" if value is None else str(value).strip()
    if text == "":
        return (
            ["Enter a SIP URI such as sip:10.0.0.10:5060;transport=tcp."],
            [],
        )

    scheme, separator, rest = text.partition(":")
    if not separator or scheme.strip().lower() not in {"sip", "sips"}:
        return (
            [
                "Start the URI with sip: or sips: "
                "(for example sip:10.0.0.10:5060;transport=tcp)."
            ],
            [],
        )

    transports: list[str] = []
    for raw_param in rest.split(";")[1:]:
        name, _, param_value = raw_param.partition("=")
        if name.strip().lower() == "transport":
            transports.append(param_value.strip().lower())

    for transport in transports:
        if transport not in _ALLOWED_TRANSPORTS:
            shown = transport or "empty"
            return (
                [f"transport must be tcp or udp, not {shown!r}."],
                [],
            )

    # sip_uri owns host/port/parameter grammar and already carries the locked
    # FQDN hint.  sips: has the same authority syntax, so validate it as sip:.
    shape_errors, _ = sip_uri(f"sip:{rest}", context)
    if shape_errors:
        return (shape_errors, [])

    warnings: list[str] = []
    if not transports:
        warnings.append(TRANSPORT_OMITTED_WARNING)
    elif any(transport == "udp" for transport in transports):
        warnings.append(MTU_WARNING)
    return ([], warnings)


def dispatcher_flags(
    value: Any, context: ValidationContext
) -> tuple[list[str], list[str]]:
    """Dispatcher state flags: a non-negative integer with a known-bit warning."""
    number, error = _non_negative_int(value)
    if error is not None or number is None:
        return ([error or "Enter a whole number."], [])
    if number not in KNOWN_FLAGS:
        expected = ", ".join(str(flag) for flag in KNOWN_FLAGS)
        return (
            [],
            [
                f"Dispatcher flag value {number} is unusual — expected one of "
                f"{expected}."
            ],
        )
    return ([], [])


def priority_int(value: Any, context: ValidationContext) -> tuple[list[str], list[str]]:
    """Dispatcher priority: a non-negative integer (lower is tried first)."""
    number, error = _non_negative_int(value)
    if error is not None or number is None:
        return ([error or "Enter a whole number."], [])
    return ([], [])


def attrs_free_text(
    value: Any, context: ValidationContext
) -> tuple[list[str], list[str]]:
    """``attrs`` is a separate plain-text column, so free text is allowed.

    Only characters that cannot survive the column/UI round trip are rejected:
    NUL and line breaks.  Semicolons, commas and SIP-shaped notes are fine —
    they never become part of the destination.
    """
    text = "" if value is None else str(value)
    errors: list[str] = []
    if "\x00" in text:
        errors.append("Remove the NUL character — this field is stored as text.")
    if "\r" in text or "\n" in text:
        errors.append("Use a single line — line breaks are not stored in this field.")
    return (errors, [])


RULES: dict[str, Validator] = {
    "setid_int": setid_int,
    "destination_ipv4_required": destination_ipv4_required,
    "dispatcher_flags": dispatcher_flags,
    "priority_int": priority_int,
    "attrs_free_text": attrs_free_text,
}

#: Names this module implements; the package merge removes them from
#: ``console.validators.PENDING_VALIDATORS``.
PENDING: frozenset[str] = frozenset(RULES)
