"""derivations.py — named `derived:` rules for descriptor forms.

Kept out of ``forms_ui`` so the generic router stays free of table names (the
suite enforces that invariant).  Registry keys match ``tables.yaml``.
"""
from __future__ import annotations

from typing import Any, Mapping

from . import descriptors


def tenant_ds_onnet(
    *,
    descriptor: descriptors.Descriptor,
    field: descriptors.Field,
    values: Mapping[str, Any],
    conn,
    mode: str,
    existing: Mapping[str, Any],
) -> str:
    """A landline row inherits its home's on-net set id (never user-supplied)."""
    tenantid = str(values.get("tenantid") or "").strip()
    if not tenantid:
        return ""
    registry = descriptors.load().tables.get("tenant_registry")
    if registry is None:
        return ""
    for row in conn.execute("SELECT tprefix, tvalue FROM tenant_registry"):
        try:
            unpacked = descriptors.unpack_row(registry, dict(row))
        except (KeyError, ValueError):
            continue
        if str(unpacked.get("tenantid") or "") == tenantid:
            return unpacked.get("ds_onnet", "")
    return ""


DERIVATIONS = {"tenant_ds_onnet": tenant_ds_onnet}
