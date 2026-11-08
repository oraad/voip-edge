"""Shared per-home verification checks.

The onboarding verify page and the Homes detail page use the same rules:
SQLite coherence never calls Kamailio; live checks are explicit.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any, Iterable, Mapping

from . import db, descriptors, validators

_INTEGER = re.compile(r"^-?\d+$")


def int_or_none(value: Any) -> int | None:
    text = "" if value is None else str(value).strip()
    if text == "":
        return None
    if not _INTEGER.fullmatch(text):
        return None
    return int(text)


def dispatcher_target_ok(destination: str) -> bool:
    """True for a dispatcher URI the dispatcher rule would save.

    ``sips:`` and a user part are accepted, matching
    :func:`console.rules.dispatcher.destination_ipv4_required`; the shared
    ``sip_uri`` rule enforces the literal-IPv4 host.
    """
    text = "" if destination is None else str(destination).strip()
    scheme, separator, rest = text.partition(":")
    if not separator or scheme.strip().lower() not in {"sip", "sips"}:
        return False
    errors, _ = validators.sip_uri(
        f"sip:{rest}", validators.ValidationContext()
    )
    return not errors


def trusted_entries(result: Any) -> list[dict[str, str]]:
    """Normalise ``permissions.trustedDump`` text or structured output.

    Kamailio repeats ``item`` keys in one object, so :meth:`call_pairs`
    returns nested ``(key, value)`` lists.  Treat those as mappings and keep
    every row instead of the last one.
    """
    entries: list[dict[str, str]] = []

    def add(ip: Any, tag: Any) -> None:
        ip_text = str(ip or "").strip().strip('"')
        tag_text = str(tag or "").strip().strip('"')
        if ip_text and tag_text:
            entries.append({"ip": ip_text, "tag": tag_text})

    def collect(value: Any) -> None:
        if value is None:
            return
        if isinstance(value, str):
            # One block per ``item:`` keeps an untagged row from pairing with
            # the next row's tag.
            for block in re.split(r"item\s*:\s*\{", value)[1:]:
                block = block.split("}", 1)[0]
                ip = re.search(r"\bip\s*:\s*(\S+)", block)
                tag = re.search(r"\btag\s*:\s*(\S+)", block)
                if ip is not None and tag is not None:
                    add(ip.group(1), tag.group(1))
            return
        if isinstance(value, Mapping):
            item = value.get("item")
            if isinstance(item, Mapping):
                collect(item)
            add(value.get("ip"), value.get("tag"))
            return
        if isinstance(value, (list, tuple)):
            pairs = [
                item
                for item in value
                if isinstance(item, tuple)
                and len(item) == 2
                and isinstance(item[0], str)
            ]
            if pairs:
                for key, item in pairs:
                    if key == "item":
                        collect(item)
                add(
                    next((item for key, item in pairs if key == "ip"), None),
                    next((item for key, item in pairs if key == "tag"), None),
                )
                return
            for item in value:
                collect(item)

    collect(result)
    return entries


def _live_targets(rows: list[dict[str, Any]], set_id: int | None) -> list[dict[str, Any]]:
    if set_id in (None, 0):
        return []
    return [
        row for row in rows if str(row.get("setid")) == str(set_id)
    ]


def _live_detail(rows: list[dict[str, Any]], set_id: int) -> str:
    parts: list[str] = []
    for row in rows:
        state = row.get("label") or row.get("state") or "unknown"
        target = row.get("target") or row.get("target_display") or ""
        loaded = (
            "loaded" if row.get("state") == "active" else "probe not answered"
        )
        parts.append(f"{target} · {loaded} ({state})")
    return f"set {set_id} · " + " | ".join(parts)


def build_live_checks(
    get_rpc: Any,
    domain: str,
    source_ip: str,
    onnet_set: int | None,
    pstn_set: int | None,
) -> dict[str, Any]:
    """Probe Kamailio for the loaded dispatcher and trusted state."""
    from .live import build_dispatcher_view, edge_health

    items: list[dict[str, Any]] = []
    errors: list[str] = []

    try:
        rpc = get_rpc()
    except Exception as exc:  # noqa: BLE001 - report, never raise from the panel
        rpc = None
        errors.append(f"RPC client unavailable: {exc}")

    health = edge_health(rpc)
    reachable = bool(health["reachable"])
    if not reachable:
        errors.append("Edge unreachable; live checks skipped.")
        return {
            "reachable": False,
            "uptime": None,
            "items": [],
            "errors": errors,
        }

    rows: list[dict[str, Any]] = []
    dispatcher_unavailable = False
    try:
        rows = build_dispatcher_view(rpc.call("dispatcher.list"))
    except Exception as exc:  # noqa: BLE001
        if "No Destination Sets" not in str(exc):
            dispatcher_unavailable = True
            errors.append(f"dispatcher.list failed: {exc}")

    onnet_rows = _live_targets(rows, onnet_set)
    onnet_state = "ok"
    if dispatcher_unavailable:
        onnet_ok = False
        onnet_state = "unavailable"
        onnet_detail = "dispatcher list unavailable"
    elif onnet_set in (None, 0):
        onnet_ok = False
        onnet_state = "missing"
        onnet_detail = "no on-net route set"
    elif not onnet_rows:
        onnet_ok = False
        onnet_state = "missing"
        onnet_detail = f"set {onnet_set} is not loaded"
    elif not all(
        dispatcher_target_ok(row.get("target", "")) for row in onnet_rows
    ):
        onnet_ok = False
        onnet_state = "missing"
        onnet_detail = f"set {onnet_set} has a non-literal-IPv4 destination"
    elif not all(row.get("state") == "active" for row in onnet_rows):
        onnet_ok = False
        onnet_state = "missing"
        onnet_detail = _live_detail(onnet_rows, onnet_set)
    else:
        onnet_ok = True
        onnet_detail = _live_detail(onnet_rows, onnet_set)
    items.append(
        {
            "key": "edge_trunk_out",
            "label": "Edge → PBX: on-net route set",
            "ok": onnet_ok,
            "state": onnet_state,
            "detail": onnet_detail,
        }
    )

    pstn_rows = _live_targets(rows, pstn_set)
    pstn_state = "ok"
    if dispatcher_unavailable:
        pstn_ok = False
        pstn_state = "unavailable"
        pstn_detail = "dispatcher list unavailable"
    elif pstn_set in (None, 0):
        pstn_ok = True
        pstn_state = "neutral"
        pstn_detail = "No PSTN trunk configured"
    elif not pstn_rows:
        pstn_ok = False
        pstn_state = "missing"
        pstn_detail = f"set {pstn_set} is not loaded"
    elif not all(
        dispatcher_target_ok(row.get("target", "")) for row in pstn_rows
    ) or not all(row.get("state") == "active" for row in pstn_rows):
        pstn_ok = False
        pstn_state = "missing"
        pstn_detail = _live_detail(pstn_rows, pstn_set)
    else:
        pstn_ok = True
        pstn_detail = _live_detail(pstn_rows, pstn_set)
    items.append(
        {
            "key": "pstn",
            "label": "PSTN trunk route set",
            "ok": pstn_ok,
            "state": pstn_state,
            "detail": pstn_detail,
        }
    )

    trusted_ok = False
    trusted_state = "missing"
    trusted_detail = "no trusted entries loaded"
    try:
        dump = getattr(rpc, "call_pairs", rpc.call)
        result = dump("permissions.trustedDump")
        entries = trusted_entries(result)
        if not entries and result not in (None, [], (), {}):
            trusted_state = "unavailable"
            trusted_detail = "trusted dump is not readable"
        else:
            match = next(
                (
                    entry
                    for entry in entries
                    if entry["ip"] == source_ip and entry["tag"] == domain
                ),
                None,
            )
            if match is not None:
                trusted_ok = True
                trusted_state = "ok"
                trusted_detail = f"{match['ip']} → {match['tag']} · loaded"
            elif not source_ip:
                trusted_detail = "no trusted source in the database"
            else:
                trusted_detail = f"{source_ip} → {domain} is not loaded"
    except Exception as exc:  # noqa: BLE001 - older Kamailio may lack the RPC
        trusted_state = "unavailable"
        trusted_detail = f"trusted dump unavailable: {exc}"
    items.append(
        {
            "key": "edge_trunk_in",
            "label": "PBX → edge: trusted tunnel source",
            "ok": trusted_ok,
            "state": trusted_state,
            "detail": trusted_detail,
        }
    )

    return {
        "reachable": True,
        "uptime": health.get("uptime"),
        "items": items,
        "errors": errors,
    }


def build_home_live(
    get_rpc: Any,
    domain: str,
    source_ip: str,
    onnet_set: int | None,
    pstn_set: int | None,
) -> dict[str, Any]:
    """Live checks plus the phones registered at this home."""
    live = build_live_checks(
        get_rpc, domain, source_ip, onnet_set, pstn_set
    )
    live["phones"] = []
    if not live["reachable"]:
        return live
    try:
        rpc = get_rpc()
        from .live import build_regcache_view

        rows = build_regcache_view(rpc.call("htable.dump", ["regcache"]))
        live["phones"] = [
            row for row in rows if row.get("domain") == domain
        ]
    except Exception as exc:  # noqa: BLE001 - panel must still render
        live["errors"].append(f"registered phones unavailable: {exc}")
    return live


def coherence_summaries(
    conn: sqlite3.Connection | None, domains: Iterable[str]
) -> dict[str, dict[str, Any]]:
    """DB-only coherence per home, with one bulk read for the Homes list."""
    ordered = [str(domain) for domain in domains if str(domain)]
    unavailable = {
        domain: {
            "tone": "warn",
            "text": "database unavailable",
            "missing": ["database"],
        }
        for domain in ordered
    }
    if conn is None or not ordered:
        return unavailable

    wanted = set(ordered)
    registry_values: dict[str, dict[str, Any]] = {}
    dispatcher_sets: dict[int, list[str]] = {}
    trusted_tags: set[str] = set()
    landline_keys: set[str] = set()
    dpids: set[int] = set()
    try:
        descriptor = descriptors.load().tables["tenant_registry"]
        for raw in db.read_rows(conn, "tenant_registry"):
            try:
                values = descriptors.unpack_row(descriptor, raw)
            except (KeyError, ValueError):
                continue
            domain = str(values.get("tprefix") or "")
            if domain in wanted:
                registry_values[domain] = values

        for setid, destination in conn.execute(
            "SELECT setid, destination FROM dispatcher"
        ):
            number = int_or_none(setid)
            if number is not None:
                dispatcher_sets.setdefault(number, []).append(str(destination))
        trusted_tags = {
            str(row[0]) for row in conn.execute("SELECT DISTINCT tag FROM trusted")
        }
        landline_keys = {
            str(row[0])
            for row in conn.execute("SELECT tprefix FROM tenant_landline")
        }
        dpids = {
            int(row[0])
            for row in conn.execute("SELECT DISTINCT dpid FROM dialplan")
            if int_or_none(row[0]) is not None
        }
    except sqlite3.Error:
        return unavailable

    summaries: dict[str, dict[str, Any]] = {}
    for domain in ordered:
        values = registry_values.get(domain)
        if values is None:
            summaries[domain] = {
                "tone": "warn",
                "text": "registry row missing",
                "missing": ["registry row"],
            }
            continue

        missing: list[str] = []
        onnet = int_or_none(values.get("ds_onnet"))
        pstn = int_or_none(values.get("ds_pstn"))
        if onnet in (None, 0):
            missing.append("no on-net set")
        elif onnet not in dispatcher_sets:
            missing.append(f"set {onnet} missing")
        elif not all(
            dispatcher_target_ok(destination)
            for destination in dispatcher_sets[onnet]
        ):
            missing.append(f"set {onnet} destination")
        if pstn not in (None, 0) and pstn not in dispatcher_sets:
            missing.append(f"set {pstn} missing")
        if domain not in trusted_tags:
            missing.append("no trusted source")
        cli = str(values.get("cli") or "").strip()
        if not cli or cli not in landline_keys:
            missing.append("landline row missing")
        dpid = int_or_none(values.get("dpid"))
        if dpid not in (None, 0) and dpid not in dpids:
            missing.append(f"dpid {dpid} missing")

        summaries[domain] = {
            "tone": "ok" if not missing else "warn",
            "text": "all green" if not missing else missing[0],
            "missing": missing,
        }
    return summaries


def verify_state(
    conn: sqlite3.Connection | None, domain: str
) -> tuple[Any, dict[str, Any], str]:
    """Registry row, unpacked values and the home's trusted source address."""
    registry = None
    values: dict[str, Any] = {}
    source_ip = ""
    if conn is None:
        return registry, values, source_ip
    try:
        registry = db.read_row(conn, "tenant_registry", domain)
    except sqlite3.Error:
        registry = None
    if registry is not None:
        try:
            values = descriptors.unpack_row(
                descriptors.load().tables["tenant_registry"], registry
            )
        except (ValueError, KeyError):
            values = {}
    try:
        row = conn.execute(
            "SELECT src_ip FROM trusted WHERE tag = ? ORDER BY id LIMIT 1",
            (domain,),
        ).fetchone()
    except sqlite3.Error:
        row = None
    if row is not None:
        source_ip = str(row[0])
    return registry, values, source_ip


def verify_checklist(
    conn: sqlite3.Connection | None, domain: str
) -> dict[str, Any]:
    """DB-only coherence checklist for the verify step (no RPC, no N+1)."""
    items: list[dict[str, Any]] = []

    registry, values, source_ip = verify_state(conn, domain)

    items.append(
        {
            "key": "registry",
            "label": "Home registered in the routing table",
            "ok": registry is not None,
            "detail": domain if registry is not None else "no registry row",
        }
    )

    onnet = int_or_none(values.get("ds_onnet"))
    pstn = int_or_none(values.get("ds_pstn"))
    set_ids = [value for value in (onnet, pstn) if value not in (None, 0)]
    rows_by_set: dict[int, list[str]] = {}
    if conn is not None and set_ids:
        try:
            for row in conn.execute(
                "SELECT setid, destination FROM dispatcher WHERE setid IN "
                f"({', '.join('?' for _ in set_ids)})",
                set_ids,
            ):
                rows_by_set.setdefault(int(row[0]), []).append(str(row[1]))
        except sqlite3.Error:
            rows_by_set = {}

    onnet_rows = rows_by_set.get(onnet, []) if onnet not in (None, 0) else []
    onnet_ok = bool(onnet_rows) and all(
        dispatcher_target_ok(destination) for destination in onnet_rows
    )
    if onnet in (None, 0):
        onnet_detail = "no on-net route set"
    elif not onnet_rows:
        onnet_detail = f"set {onnet} has no dispatcher row"
    elif not onnet_ok:
        onnet_detail = f"set {onnet} has a non-literal-IPv4 destination"
    else:
        onnet_detail = f"set {onnet} → {onnet_rows[0]}"
    items.append(
        {
            "key": "edge_trunk_out",
            "label": "Edge → PBX: on-net route set",
            "ok": onnet_ok,
            "detail": onnet_detail,
        }
    )

    pstn_rows = rows_by_set.get(pstn, []) if pstn not in (None, 0) else []
    pstn_ok = True
    if pstn in (None, 0):
        pstn_detail = "No PSTN trunk configured"
    elif pstn_rows:
        pstn_detail = f"set {pstn} → {pstn_rows[0]}"
    else:
        pstn_ok = False
        pstn_detail = f"set {pstn} has no dispatcher row"
    items.append(
        {
            "key": "pstn",
            "label": "PSTN trunk route set",
            "ok": pstn_ok,
            "neutral": pstn in (None, 0),
            "detail": pstn_detail,
        }
    )

    trusted_ok = bool(source_ip)
    trusted_detail = (
        f"{source_ip} → {domain}" if trusted_ok else "no trusted row"
    )
    items.append(
        {
            "key": "edge_trunk_in",
            "label": "PBX → edge: trusted tunnel source",
            "ok": trusted_ok,
            "detail": trusted_detail,
        }
    )

    landline_ok = False
    landline_detail = "no landline row"
    cli = str(values.get("cli") or "").strip()
    if conn is not None and cli:
        try:
            landline_row = db.read_row(conn, "tenant_landline", cli)
        except sqlite3.Error:
            landline_row = None
        if landline_row is not None:
            landline_ok = True
            target = ""
            try:
                unpacked = descriptors.unpack_row(
                    descriptors.load().tables["tenant_landline"], landline_row
                )
                target = str(unpacked.get("target") or "")
            except (ValueError, KeyError):
                pass
            landline_detail = f"landline {cli} → {target or 'ring group'}".strip()
    items.append(
        {
            "key": "landline",
            "label": "Landline points at the ring group",
            "ok": landline_ok,
            "detail": landline_detail,
        }
    )

    dpid = int_or_none(values.get("dpid"))
    dpid_ok = True
    dpid_detail = "no emergency dialplan partition"
    if conn is not None and dpid not in (None, 0):
        try:
            row = conn.execute(
                "SELECT 1 FROM dialplan WHERE dpid = ? LIMIT 1", (dpid,)
            ).fetchone()
        except sqlite3.Error:
            row = None
        dpid_ok = row is not None
        dpid_detail = f"dpid {dpid}" + ("" if dpid_ok else " not in dialplan")
    items.append(
        {
            "key": "dpid",
            "label": (
                "Emergency dialplan partition exists"
                if dpid not in (None, 0)
                else "Emergency dialplan partition (none configured — optional)"
            ),
            "ok": dpid_ok,
            "detail": dpid_detail,
        }
    )

    manual = [
        {
            "key": "call_out",
            "label": "Place a call from a phone on the PBX through the edge",
            "detail": "Confirm the call connects in both directions with audio.",
        }
    ]
    if pstn not in (None, 0):
        manual.append(
            {
                "key": "call_pstn",
                "label": "Dial out through the landline trunk",
                "detail": (
                    "Confirm the carrier answers; the edge cannot test the "
                    "PBX's internal trunk."
                ),
            }
        )

    return {
        "items": items,
        "all_green": all(item["ok"] for item in items),
        "manual": manual,
    }
