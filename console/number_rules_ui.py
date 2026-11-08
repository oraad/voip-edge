"""Simplified Number rules pages over the standard dialplan table."""
from __future__ import annotations

import re
import sqlite3
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import db, descriptors
from .pack import parse_flags
from .security import is_authenticated

router = APIRouter()
NO_STORE = {"Cache-Control": "no-store"}

TABS = ("emergency", "blocked", "custom")
TAB_LABELS = {
    "emergency": "Emergency",
    "blocked": "Blocked",
    "custom": "Custom",
}
_EXACT = re.compile(r"\^\(?([\d|]+)\)?\$")
_PREFIX = re.compile(r"\^\(?([\d|]+)\)?")


def _requires_login(request: Request) -> RedirectResponse | None:
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303, headers=NO_STORE)
    return None


def _catalog(conn: sqlite3.Connection | None) -> dict[int, dict]:
    """Partition id -> roles, homes and rule count."""
    entries: dict[int, dict] = {}

    def add(dpid: int, role: str | None = None, home: str = "") -> None:
        entry = entries.setdefault(
            dpid, {"dpid": dpid, "roles": set(), "homes": [], "rules": 0}
        )
        if role:
            entry["roles"].add(role)
        if home and home not in entry["homes"]:
            entry["homes"].append(home)

    if conn is not None:
        descriptor = descriptors.load().tables.get("tenant_registry")
        try:
            rows = db.read_rows(conn, "tenant_registry")
        except sqlite3.Error:
            rows = []
        for raw in rows:
            try:
                values = descriptors.unpack_row(descriptor, raw)
            except (KeyError, ValueError):
                continue
            home = str(values.get("tprefix") or "")
            try:
                dpid = int(str(values.get("dpid") or "0"))
            except ValueError:
                dpid = 0
            if dpid > 0:
                add(dpid, "emergency", home)
            try:
                _ih, _pstn, block = parse_flags(str(values.get("flags") or ""))
            except ValueError:
                block = None
            if block:
                add(block, "blocked", home)

        try:
            for row in conn.execute("SELECT DISTINCT dpid FROM dialplan"):
                add(int(row[0]))
        except (sqlite3.Error, ValueError, TypeError):
            pass
        try:
            for row in conn.execute(
                "SELECT dpid, COUNT(*) FROM dialplan GROUP BY dpid"
            ):
                if int(row[0]) in entries:
                    entries[int(row[0])]["rules"] = int(row[1])
        except (sqlite3.Error, ValueError, TypeError):
            pass

    for entry in entries.values():
        if not entry["roles"]:
            entry["roles"].add("custom")
        entry["role_text"] = " + ".join(
            role.capitalize() for role in sorted(entry["roles"])
        )
    return dict(sorted(entries.items()))


def _rule_summary(pattern: str) -> str:
    pattern = str(pattern or "")
    match = _EXACT.fullmatch(pattern)
    if match:
        return ", ".join(match.group(1).split("|"))
    match = _PREFIX.fullmatch(pattern)
    if match:
        return "Starts with " + ", ".join(match.group(1).split("|"))
    return pattern or "—"


def _rules(conn: sqlite3.Connection | None) -> list[dict]:
    if conn is None:
        return []
    try:
        rows = db.read_rows(conn, "dialplan")
    except sqlite3.Error:
        return []
    out: list[dict] = []
    for raw in rows:
        try:
            dpid = int(raw.get("dpid") or 0)
        except (TypeError, ValueError):
            continue
        pattern = str(raw.get("match_exp") or "")
        out.append(
            {
                "id": raw.get("id"),
                "dpid": dpid,
                "priority": raw.get("pr"),
                "pattern": pattern,
                "summary": _rule_summary(pattern),
                "description": str(raw.get("attrs") or ""),
                "edit_url": f"/tables/dialplan/{quote(str(raw.get('id')), safe='')}/edit",
                "delete_url": f"/tables/dialplan/{quote(str(raw.get('id')), safe='')}/delete",
            }
        )
    return sorted(out, key=lambda row: (row["dpid"], row["priority"] or 0, row["id"] or 0))


def _render(
    request: Request,
    *,
    conn: sqlite3.Connection | None,
    tab: str = "emergency",
    values: dict | None = None,
    errors: dict | None = None,
    warnings: list[str] | None = None,
    status_code: int = 200,
):
    if tab not in TABS:
        tab = "emergency"
    catalog = _catalog(conn)
    rules = _rules(conn)
    groups = {
        "emergency": [row for row in rules if "emergency" in catalog.get(row["dpid"], {}).get("roles", set())],
        "blocked": [row for row in rules if "blocked" in catalog.get(row["dpid"], {}).get("roles", set())],
        "custom": [row for row in rules if "custom" in catalog.get(row["dpid"], {}).get("roles", set())],
    }
    params = request.query_params
    return request.app.state.templates.TemplateResponse(
        request,
        "number_rules.html",
        {
            "nav": "number-rules",
            "tab": tab,
            "tabs": [(name, TAB_LABELS[name]) for name in TABS],
            "partitions": list(catalog.values()),
            "emergency_partitions": [p for p in catalog.values() if "emergency" in p["roles"]],
            "blocked_partitions": [p for p in catalog.values() if "blocked" in p["roles"]],
            "groups": groups,
            "values": values or {},
            "errors": errors or {},
            "warnings": warnings or [],
            "advanced_url": "/tables/dialplan",
            "saved": params.get("saved", ""),
            "status_code": status_code,
        },
        status_code=status_code,
        headers=NO_STORE,
    )


@router.get("/number-rules", response_class=HTMLResponse)
def number_rules(
    request: Request,
    tab: str = "emergency",
    conn: sqlite3.Connection | None = Depends(db.get_read_db),
):
    redirect = _requires_login(request)
    if redirect is not None:
        return redirect
    return _render(request, conn=conn, tab=tab)
