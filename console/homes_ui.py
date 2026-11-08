"""homes_ui.py — Homes landing page (plan §6, wireframe v1).

The landing page is a thin, descriptor-driven projection of
``tenant_registry``: one row per home, its landline and route-set ids, the
coherence placeholder (live checks arrive with T6) and an Active status badge
because a row is present.  Parked homes live in the sidecar and are a later
task.  With no registry rows the page shows the first-use empty state and the
"+ Onboard a home" CTA.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import db, descriptors
from .checks import coherence_summaries
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
