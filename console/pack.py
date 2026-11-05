"""packed-column codec for the two mtree tables (plan §5.1/§5.2).

Kamailio loads ``tenant_registry`` and ``tenant_landline`` in the default
two-column mtree form, so the record is packed into ``tvalue``:

* ``tenant_registry.tvalue``  = ``tenantid,ds_onnet,ds_pstn,cli,label,dpid,flags``
* ``tenant_landline.tvalue``  = ``tenantid,target,ds_onnet,ih_on``

The comma is the field separator and ``+`` is the flag separator.  Free-text
field values may contain neither: a stray separator would silently shift every
later field, so packing refuses instead.  ``flags`` itself is a structured
value (see :func:`pack_flags`/:func:`parse_flags`) and is the one field allowed
to contain ``+``; :func:`console.descriptors.pack_row` handles it separately.
"""
from __future__ import annotations

from collections.abc import Iterable

FIELD_SEPARATOR = ","
FLAG_SEPARATOR = "+"


def _text(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def _reject_separators(value: object, label: str) -> str:
    text = _text(value)
    if FIELD_SEPARATOR in text:
        raise ValueError(
            f"{label} may not contain {FIELD_SEPARATOR!r} "
            f"(comma is the packed-field separator): {text!r}"
        )
    if FLAG_SEPARATOR in text:
        raise ValueError(
            f"{label} may not contain {FLAG_SEPARATOR!r} "
            f"(plus is the flags separator): {text!r}"
        )
    return text


def pack_fields(fields: Iterable[object]) -> str:
    """Comma-join packed fields, rejecting the packing separators inside them."""
    parts: list[str] = []
    for index, value in enumerate(fields):
        parts.append(_reject_separators(value, f"packed field {index}"))
    return FIELD_SEPARATOR.join(parts)


def unpack_fields(raw: str, expected: int | None = None) -> list[str]:
    """Split a packed ``tvalue``; ``expected`` guards against row corruption."""
    if raw is None:  # tolerate a NULL column from a hand-edited DB
        raw = ""
    raw = str(raw)
    fields = raw.split(FIELD_SEPARATOR) if raw != "" else []
    if expected is not None and len(fields) != expected:
        raise ValueError(
            f"packed value has {len(fields)} field(s), expected {expected}: {raw!r}"
        )
    return fields


def _bit(value: object) -> str:
    return "1" if value else "0"


def pack_flags(
    ih_on: bool, pstn_on: bool, block_dpid: int | None = None
) -> str:
    """Pack the registry flags ``ih_on+pstn_on[+block_dpid]``.

    ``None`` means "no block partition" and is omitted; an explicit ``0`` is
    also legal on the wire and is kept as the third segment.
    """
    if block_dpid is None:
        return f"{_bit(ih_on)}{FLAG_SEPARATOR}{_bit(pstn_on)}"
    dpid = int(block_dpid)
    if dpid < 0:
        raise ValueError(f"block_dpid must be >= 0, got {dpid}")
    return f"{_bit(ih_on)}{FLAG_SEPARATOR}{_bit(pstn_on)}{FLAG_SEPARATOR}{dpid}"


def _flag_bit(part: str, label: str) -> bool:
    if part == "1":
        return True
    if part == "0":
        return False
    raise ValueError(f"{label} must be 0 or 1, got {part!r}")


def parse_flags(raw: str) -> tuple[bool, bool, int | None]:
    """Parse flags, tolerating the legal absent ``block_dpid`` segment.

    ``""`` (never configured) is treated as both switches off, no block.
    ``block_dpid`` of ``0`` is normalised to ``None`` because it means the same.
    """
    text = (raw or "").strip()
    if text == "":
        return (False, False, None)

    parts = [part.strip() for part in text.split(FLAG_SEPARATOR)]
    if len(parts) > 3:
        raise ValueError(f"flags have at most three segments: {raw!r}")

    ih_on = _flag_bit(parts[0], "ih_on")
    pstn_on = _flag_bit(parts[1], "pstn_on") if len(parts) > 1 else False

    block_dpid: int | None = None
    if len(parts) == 3 and parts[2] != "":
        try:
            block_dpid = int(parts[2])
        except ValueError as exc:
            raise ValueError(f"block_dpid must be an integer, got {parts[2]!r}") from exc
        if block_dpid < 0:
            raise ValueError(f"block_dpid must be >= 0, got {block_dpid}")
        if block_dpid == 0:
            block_dpid = None

    return (ih_on, pstn_on, block_dpid)
