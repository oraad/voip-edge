"""tables_ui.py — generic, read-only table browser (plan §6/§8 T3).

Every table-shaped decision comes from ``console/tables.yaml``: columns and
their order are the descriptor's ``show_in_list`` fields, values go through
``descriptors.unpack_row``, labels/help come from the field metadata.  This
module contains **no** table names and no per-table branches.

Routes
------
``GET /tables/{table}``       list, optional ``?q=`` substring filter
``GET /tables/{table}/{key}`` detail, labelled descriptor fields + help

Both are authenticated.  With ``HX-Request: true`` the response is the page
content only; otherwise it is a full document (``base.html`` decides from the
``fragment`` flag).
"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from markupsafe import escape

from . import db, descriptors
from .security import read_session, is_authenticated

router = APIRouter()

NO_STORE = {"Cache-Control": "no-store"}

#: Field types rendered in the monospace face (SIP-shaped values, plan §2).
MONO_TYPES = frozenset({"domain", "sip_uri", "digits", "ipv4"})


def _wants_fragment(request: Request) -> bool:
    return request.headers.get("HX-Request", "").strip().lower() == "true"


def _tableset() -> descriptors.TableSet:
    return descriptors.load()


def _descriptor(table: str) -> descriptors.Descriptor | None:
    return _tableset().tables.get(table)


def _humanize(name: str) -> str:
    return name.replace("_", " ").replace("-", " ").title()


def _column_view(field: descriptors.Field) -> dict[str, object]:
    return {
        "name": field.name,
        "label": _humanize(field.name),
        "mono": field.type in MONO_TYPES,
    }


def _requires_login(request: Request) -> RedirectResponse | None:
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303, headers=NO_STORE)
    return None


def _not_found(request: Request, message: str) -> HTMLResponse:
    """HTML 404 that also honours fragment mode (no doctype inside a swap)."""
    panel = (
        '<section class="panel empty">'
        "<h1>Not found</h1>"
        f"<p>{escape(message)}</p>"
        '<p><a href="/homes">Back to homes</a></p>'
        "</section>"
    )
    if _wants_fragment(request):
        body = panel
    else:
        body = (
            "<!DOCTYPE html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<title>Not found — VoIP Edge Console</title></head>"
            f'<body><main class="main" id="main">{panel}</main></body></html>'
        )
    return HTMLResponse(body, status_code=404, headers=NO_STORE)


def _templates(request: Request):
    return request.app.state.templates


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------
@router.get("/tables/{table}", response_class=HTMLResponse)
def table_list(
    request: Request,
    table: str,
    q: str = "",
    conn: sqlite3.Connection | None = Depends(db.get_read_db),
):
    redirect = _requires_login(request)
    if redirect is not None:
        return redirect

    descriptor = _descriptor(table)
    if descriptor is None:
        return _not_found(request, f"No table named “{table}”.")

    columns = [_column_view(field) for field in descriptor.fields if field.show_in_list]
    query = q.strip()
    needle = query.casefold()

    rows: list[dict] = []
    has_rows = False
    if conn is not None:
        try:
            key_name = db.key_column(conn, table)
            raw_rows = db.read_rows(conn, table)
        except sqlite3.Error:
            # An existing but not-yet-created schema is an empty first-use DB.
            raw_rows = []
            key_name = None
        has_rows = bool(raw_rows)
        corrupt = 0
        for raw in raw_rows:
            try:
                values = descriptors.unpack_row(descriptor, raw)
            except (KeyError, ValueError):
                corrupt += 1
                continue
            values["_key"] = raw.get(key_name) if key_name else None
            if needle and not any(
                needle in str(values.get(column["name"], "")).casefold()
                for column in columns
            ):
                continue
            rows.append(values)

    return _templates(request).TemplateResponse(
        request,
        "table_list.html",
        {
            "nav": "tables",
            "fragment": _wants_fragment(request),
            "table": descriptor.table,
            "table_label": _humanize(descriptor.table),
            "columns": columns,
            "rows": rows,
            "corrupt_rows": corrupt,
            "q": q,
            "query": query,
            # Empty DB = first use; filtered to zero with rows behind it = filter.
            "filtered_empty": bool(query) and has_rows and not rows,
        },
        headers=NO_STORE,
    )


# ---------------------------------------------------------------------------
# detail
# ---------------------------------------------------------------------------
@router.get("/tables/{table}/{key}", response_class=HTMLResponse)
def table_detail(
    request: Request,
    table: str,
    key: str,
    conn: sqlite3.Connection | None = Depends(db.get_read_db),
):
    redirect = _requires_login(request)
    if redirect is not None:
        return redirect

    descriptor = _descriptor(table)
    if descriptor is None:
        return _not_found(request, f"No table named “{table}”.")

    raw = None
    if conn is not None:
        try:
            raw = db.read_row(conn, table, key)
        except sqlite3.Error:
            raw = None
    if raw is None:
        return _not_found(request, f"No row “{key}” in {_humanize(table)}.")

    try:
        values = descriptors.unpack_row(descriptor, raw)
    except (KeyError, ValueError):
        return _not_found(
            request, f"Row “{key}” is corrupt (bad packed record) — fix it via the CLI."
        )
    fields = [
        {
            "name": field.name,
            "label": _humanize(field.name),
            "help": field.help,
            "mono": field.type in MONO_TYPES,
            "value": values.get(field.name, ""),
        }
        for field in descriptor.fields
    ]

    return _templates(request).TemplateResponse(
        request,
        "table_detail.html",
        {
            "nav": "tables",
            "fragment": _wants_fragment(request),
            "table": descriptor.table,
            "table_label": _humanize(descriptor.table),
            "key": key,
            "deletable": descriptor.delete_policy != "lifecycle",
            "fields": fields,
        },
        headers=NO_STORE,
    )
