"""console.rules.trusted — the Kamailio ``permissions.trusted`` family (T4.6).

Plan §5.2/§7.19.  The edge accepts a home's SIP traffic by matching the
**wire-observed source address** of its Pangolin tunnel against this table;
the tag names the home that address belongs to.

Rules
-----
``trusted_src_ip``
    A literal IPv4 address in the private/CGNAT space the overlay uses
    (RFC1918 plus ``100.64.0.0/10``).  A public address is an error unless the
    operator submits ``override_public_ip`` (``1``/``on``/``true``), which is
    the documented generic non-tunnel path.  A ``100.96.128.0/24`` address is
    Pangolin's utility-subnet alias, not the site's tunnel address, and warns.
    The same rule also enforces "one source address → one home": a duplicate
    across tags is an error naming the existing tag (there is no separate
    ``trusted_src_unique`` name in ``tables.yaml``).

``tag_options``
    The tag must be one of the existing ``tenant_registry.tprefix`` values
    (the same list ``options_query`` renders).

``proto_any_locked`` / ``priority_int``
    Small invariants from the descriptor: ``proto`` is always ``any`` and the
    priority is a non-negative whole number.
"""
from __future__ import annotations

import ipaddress
import re
import sqlite3
from typing import Any

from ..validators import ValidationContext, ipv4

__all__ = ["RULES", "PENDING"]

#: The overlay's address space.  ``100.64.0.0/10`` is the CGNAT/shared range
#: Pangolin hands out for tunnel interfaces; the three RFC1918 blocks cover
#: the home LANs.  Anything else is "public" for this rule.
_TRUSTED_NETWORKS: tuple[ipaddress.IPv4Network, ...] = tuple(
    ipaddress.IPv4Network(network)
    for network in (
        "100.64.0.0/10",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
    )
)

#: Pangolin's utility subnet (``voip.<client>.internal``); addresses here are
#: resource aliases, never the source address a native tunnel emits.
_UTILITY_ALIAS = ipaddress.IPv4Network("100.96.128.0/24")

_OVERRIDE_TRUTHY = frozenset({"1", "on", "true"})
_INTEGER = re.compile(r"^[+-]?\d+$")

PUBLIC_IP_MESSAGE = (
    "Use the site's wire-observed tunnel address (see onboarding step 2), or "
    "tick the override if this home is reached by another path."
)

#: The warning shown when the override is deliberately used.  It quotes the
#: deployment's own generic path (``pangolin/setup-trusted.sh``), which warns
#: and continues for a non-private/CGNAT address.
OVERRIDE_WARNING_PREFIX = (
    "Public address accepted because the override is ticked — this is the "
    "documented generic non-tunnel path (pangolin/setup-trusted.sh warns "
    "“not a private/CGNAT address — continuing anyway”)."
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _truthy(value: Any) -> bool:
    return _text(value).lower() in _OVERRIDE_TRUTHY


def _row_id(context: ValidationContext) -> int | None:
    """The auto ``id`` of the row being edited, when the context names it."""
    if context.mode != "edit" or context.key is None:
        return None
    try:
        return int(str(context.key))
    except (TypeError, ValueError):
        return None


def _existing_tag(
    conn: sqlite3.Connection | None,
    src_ip: str,
    tag: str,
    *,
    current_id: int | None,
) -> str | None:
    """Return a *different* tag already mapped to *src_ip*, if any."""
    if conn is None or not tag:
        return None
    try:
        rows = conn.execute(
            'SELECT id, tag FROM trusted WHERE src_ip = ? ORDER BY id',
            (src_ip,),
        ).fetchall()
    except sqlite3.Error:
        return None
    for row in rows:
        row_id, row_tag = row[0], _text(row[1])
        if current_id is not None and row_id == current_id:
            continue
        if row_tag and row_tag != tag:
            return row_tag
    return None


def _tenant_domains(conn: sqlite3.Connection | None) -> list[str]:
    if conn is None:
        return []
    try:
        rows = conn.execute(
            'SELECT DISTINCT tprefix FROM tenant_registry '
            "WHERE tprefix IS NOT NULL ORDER BY 1"
        ).fetchall()
    except sqlite3.Error:
        return []
    return [_text(row[0]) for row in rows if _text(row[0])]


# ---------------------------------------------------------------------------
# rules
# ---------------------------------------------------------------------------
def trusted_src_ip(
    value: Any, context: ValidationContext
) -> tuple[list[str], list[str]]:
    """Validate the tunnel source and prevent one address serving two homes."""
    errors, warnings = ipv4(value, context)
    if errors:
        return (errors, warnings)

    text = _text(value)
    address = ipaddress.IPv4Address(text)
    errors: list[str] = []
    warnings: list[str] = []

    if address in _UTILITY_ALIAS:
        warnings.append(
            "Use the wire-observed native IP, not the utility alias."
        )
    elif not any(address in network for network in _TRUSTED_NETWORKS):
        if _truthy(context.values.get("override_public_ip")):
            warnings.append(
                OVERRIDE_WARNING_PREFIX
                + " Use it only when this home is reached outside the "
                "Pangolin tunnel."
            )
        else:
            errors.append(PUBLIC_IP_MESSAGE)
            return (errors, warnings)

    tag = _text(context.values.get("tag"))
    existing = _existing_tag(
        context.conn, text, tag, current_id=_row_id(context)
    )
    if existing is not None:
        errors.append(
            f"This source address is already trusted for {existing}; one "
            "address cannot map to two homes."
        )
    return (errors, warnings)


def tag_options(
    value: Any, context: ValidationContext
) -> tuple[list[str], list[str]]:
    """The tag must name an existing home (``tenant_registry.tprefix``)."""
    text = _text(value)
    if context.conn is None:
        # Without a connection there is nothing to compare against; the form
        # request always supplies one, so this only affects bare unit contexts.
        return ([], [])
    domains = _tenant_domains(context.conn)

    if not domains:
        return (
            [
                "No home domains exist yet — onboard a home before trusting "
                "its source address."
            ],
            [],
        )
    if not text:
        return ([f"Choose one of the {len(domains)} existing home domains."], [])
    if text not in domains:
        return (
            [
                f"Choose an existing home domain ({len(domains)} available); "
                f"{text!r} is not one of them."
            ],
            [],
        )
    return ([], [])


def proto_any_locked(
    value: Any, context: ValidationContext
) -> tuple[list[str], list[str]]:
    """``proto`` is locked to ``any``; the tag, not the method, scopes trust."""
    if _text(value).lower() == "any":
        return ([], [])
    return (
        [
            'proto is locked to "any" — the trusted row is scoped by the '
            "source address and tag, not by SIP method."
        ],
        [],
    )


def priority_int(
    value: Any, context: ValidationContext
) -> tuple[list[str], list[str]]:
    """A non-negative whole number; lower is evaluated first."""
    text = _text(value)
    if not _INTEGER.fullmatch(text):
        return (["Enter a whole number 0 or greater."], [])
    if int(text) < 0:
        return (["Priority cannot be negative."], [])
    return ([], [])


#: Names this module implements.  They are merged into
#: :data:`console.validators.REGISTRY` and removed from the pending set.
RULES = {
    "trusted_src_ip": trusted_src_ip,
    "tag_options": tag_options,
    "proto_any_locked": proto_any_locked,
    "priority_int": priority_int,
}

PENDING = frozenset(RULES)
