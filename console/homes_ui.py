"""homes_ui.py — Homes list and per-home checks.

The list is a descriptor-driven projection of ``tenant_registry`` with a
DB-only coherence badge; opening a home reaches ``/homes/<domain>`` for the
full checklist, PBX/PSTN targets and an on-demand live probe. Parked homes
live in the sidecar; with no registry rows the page shows the first-use
empty state and the "+ Onboard a home" CTA.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from markupsafe import escape

from . import db, descriptors
from .checks import (
    build_home_live,
    coherence_summaries,
    int_or_none,
    verify_checklist,
    verify_state,
)
from .security import read_session, is_authenticated

router = APIRouter()

NO_STORE = {"Cache-Control": "no-store"}

REGISTRY_TABLE = "tenant_registry"


def _wants_fragment(request: Request) -> bool:
    return request.headers.get("HX-Request", "").strip().lower() == "true"


def _requires_login(request: Request) -> RedirectResponse | None:
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303, headers=NO_STORE)
    return None


def _route_sets(values: dict) -> str:
    onnet = values.get("ds_onnet")
    pstn = values.get("ds_pstn")
    if onnet in (None, ""):
        return "—"
    pstn_text = "no PSTN" if pstn in (None, "", 0, "0") else f"{pstn} PSTN"
    return f"{onnet} on-net · {pstn_text}"


@router.get("/homes", response_class=HTMLResponse)
def homes(
    request: Request,
    conn: sqlite3.Connection | None = Depends(db.get_read_db),
):
    redirect = _requires_login(request)
    if redirect is not None:
        return redirect

    descriptor = descriptors.load().tables.get(REGISTRY_TABLE)
    homes: list[dict] = []
    corrupt = 0

    if descriptor is not None and conn is not None:
        try:
            key_name = db.key_column(conn, REGISTRY_TABLE)
            raw_rows = db.read_rows(conn, REGISTRY_TABLE)
        except sqlite3.Error:
            raw_rows = []
            key_name = None
        for raw in raw_rows:
            try:
                values = descriptors.unpack_row(descriptor, raw)
            except (KeyError, ValueError):
                corrupt += 1
                continue
            domain = str(values.get("tprefix", ""))
            homes.append(
                {
                    "domain": domain,
                    "landline": values.get("cli", ""),
                    "route_sets": _route_sets(values),
                    "check_tone": "warn",
                    "check_text": "checks unavailable",
                    "verify_url": f"/homes/{quote(domain, safe='')}",
                    "status": "Active",  # present in the registry = active
                    "_key": raw.get(key_name) if key_name else values.get("tprefix"),
                }
            )

    summaries = coherence_summaries(conn, [home["domain"] for home in homes])
    for home in homes:
        summary = summaries.get(home["domain"], {})
        home["check_tone"] = summary.get("tone", "warn")
        home["check_text"] = summary.get("text", "checks unavailable")

    parked: list[dict] = []
    try:
        from . import settings as settings_module
        from .sidecar import Sidecar

        rows = Sidecar(
            Path(settings_module.settings.sidecar_dir) / "console.sqlite"
        ).list_parked()
        active = {home["domain"] for home in homes}
        parked = [
            {"domain": row["domain"], "parked_at": str(row.get("created_at", ""))[:10]}
            for row in rows
            if row["domain"] not in active
        ]
    except BaseException:
        parked = []

    return request.app.state.templates.TemplateResponse(
        request,
        "homes.html",
        {
            "nav": "homes",
            "fragment": _wants_fragment(request),
            "homes": homes,
            "parked": parked,
            "corrupt_rows": corrupt,
        },
        headers=NO_STORE,
    )


_RELOAD_TABLES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("home registry", ("tenant_registry", "tenantdom")),
    ("landline", ("tenant_landline", "landline")),
    ("route sets", ("dispatcher",)),
    ("trusted sources", ("trusted",)),
    ("number rules", ("dialplan",)),
)


def _reload_messages(request: Request) -> list[str]:
    """Merge in-process reload errors with sidecar pending rows."""
    messages: dict[str, str] = {}
    worker = getattr(request.app.state, "worker", None)
    worker_errors = getattr(worker, "errors", {}) if worker is not None else {}
    for label, keys in _RELOAD_TABLES:
        for key in keys:
            error = worker_errors.get(key)
            if error is not None:
                messages[label] = f"{label}: {error}"
                break

    try:
        from . import settings as settings_module
        from .sidecar import Sidecar

        sidecar = Sidecar(
            Path(settings_module.settings.sidecar_dir) / "console.sqlite"
        )
        for row in sidecar.pending_reloads():
            table = str(row.get("table") or "")
            label = next(
                (name for name, keys in _RELOAD_TABLES if table in keys),
                None,
            )
            if label is None:
                continue
            error = row.get("last_error")
            if error:
                messages[label] = f"{label}: {error}"
            else:
                messages.setdefault(label, f"{label}: reload pending")
    except BaseException:
        pass
    return list(messages.values())


def _not_found_home(domain: str) -> HTMLResponse:
    return HTMLResponse(
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<title>Home not found</title></head><body>"
        "<main><h1>Home not found</h1>"
        f"<p>No registry row for {escape(domain)}.</p>"
        '<p><a href="/homes">Back to homes</a></p>'
        "</main></body></html>",
        status_code=404,
        headers=NO_STORE,
    )


def _dispatcher_targets(
    conn: sqlite3.Connection | None, setid: int | None
) -> list[str]:
    if conn is None or setid in (None, 0):
        return []
    try:
        rows = conn.execute(
            "SELECT destination FROM dispatcher WHERE setid = ? ORDER BY destination",
            (setid,),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [str(row[0]) for row in rows]


@router.get("/homes/{domain}", response_class=HTMLResponse)
def home_detail(
    request: Request,
    domain: str,
    conn: sqlite3.Connection | None = Depends(db.get_read_db),
):
    redirect = _requires_login(request)
    if redirect is not None:
        return redirect

    registry, values, source_ip = verify_state(conn, domain)
    if registry is None:
        return _not_found_home(domain)

    onnet = int_or_none(values.get("ds_onnet"))
    pstn = int_or_none(values.get("ds_pstn"))
    checklist = verify_checklist(conn, domain, state=(registry, values, source_ip))

    reload_errors = _reload_messages(request)

    return request.app.state.templates.TemplateResponse(
        request,
        "home_detail.html",
        {
            "nav": "homes",
            "domain": domain,
            "landline": str(values.get("cli") or ""),
            "route_sets": _route_sets(values),
            "onnet": onnet,
            "pstn": pstn,
            "onnet_targets": _dispatcher_targets(conn, onnet),
            "pstn_targets": _dispatcher_targets(conn, pstn),
            "source_ip": source_ip,
            "checklist": checklist,
            "reload_errors": reload_errors,
            "live_url": f"/homes/{quote(domain, safe='')}/live",
            "edit_url": f"/tables/tenant_registry/{quote(domain, safe='')}/edit",
            "registry_url": f"/tables/tenant_registry/{quote(domain, safe='')}",
            "park_url": f"/homes/{quote(domain, safe='')}/park",
            "destroy_url": f"/homes/{quote(domain, safe='')}/destroy",
        },
        headers=NO_STORE,
    )


@router.get("/homes/{domain}/live", response_class=HTMLResponse)
def home_live(
    request: Request,
    domain: str,
    conn: sqlite3.Connection | None = Depends(db.get_read_db),
):
    redirect = _requires_login(request)
    if redirect is not None:
        return redirect

    registry, values, source_ip = verify_state(conn, domain)
    if registry is None:
        return _not_found_home(domain)
    onnet = int_or_none(values.get("ds_onnet"))
    pstn = int_or_none(values.get("ds_pstn"))

    state = request.app.state
    getter = getattr(state, "get_rpc", None) or (
        lambda: getattr(state, "rpc", None)
    )
    live = build_home_live(getter, domain, source_ip, onnet, pstn)
    return request.app.state.templates.TemplateResponse(
        request,
        "partials/home_live.html",
        {"live": live, "domain": domain},
        headers=NO_STORE,
    )
