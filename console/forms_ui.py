"""forms_ui.py — descriptor-driven create/edit forms (plan §5.1, §5.2, §6, T4.1).

Routes
------
``GET  /tables/{table}/new``             create form
``POST /tables/{table}/new``             validate, write, enqueue reload
``GET  /tables/{table}/{key}/edit``      edit form
``POST /tables/{table}/{key}/edit``      validate, write, enqueue reload

Every table-shaped decision comes from a descriptor: fields and order from
``fields``/``form_order``, widgets from ``widget``, immutable fields from
``locked``/``readonly``/``derived``, hints from ``help``, selects from
``options_query``, the write shape from ``storage``/``key``, and the reload
intent from ``reload``.  **This module contains no table names and no
per-table branches**; the T4.2–T4.8 families add validators and tests, not
template or router changes.

Validation aggregation
----------------------
Every field collects its own ``(errors, warnings)`` (see
``console.validators``).  Errors block the save and produce a ``422`` render
with a focused summary (``tabindex=-1``) plus inline ``aria-invalid`` /
``aria-describedby``; warnings are rendered as a badge/hint and the save
proceeds.  Values are preserved on every re-render.

Writes and reloads
------------------
Successful writes happen inside :func:`console.db.write_transaction` using
named columns only (never the auto ``id``).  Packed rows are built with
:func:`console.descriptors.pack_row`.  After the commit the descriptor's
reload intent is enqueued exactly once::

    worker.enqueue(descriptor.table, descriptor.reload.rpc, descriptor.reload.params)

The table key is the descriptor table name in T4.1; the lifecycle lane maps
packed tables to their mtree tree names when it batches reloads (§5.6).  The
toast never says "verified" — verification is T4.9/T9, so the wording is
"Saved … — reload scheduled shortly".

Extension points (for T4.2–T4.8)
-------------------------------
``register_default(provider)``
    ``provider(descriptor, field, values) -> value | None`` supplies a value
    for a locked/readonly/derived field on **create** (for example a fixed
    ``match_op``).  First non-``None`` wins.
``register_derivation(name, fn)``
    ``fn(*, descriptor, field, values, conn, mode, existing) -> value`` backs
    a descriptor ``derived: name`` rule.  Unregistered names are either
    pending (skipped) or reported as a configuration error.
``DERIVATIONS`` / ``PENDING_DERIVATIONS``
    The live registry and the declarations that later families will fill.

Full page and fragment
----------------------
With ``HX-Request: true`` the response is a fragment (``form.html`` content or
``partials/toast.html``); otherwise it is a full document.  On success a plain
POST redirects ``303`` to the table list with a ``flash`` query message; an
htmx POST gets the toast fragment.
"""
from __future__ import annotations

# T4.2–T4.7 rule families register themselves on import (parallel-safe:
# each family owns console/rules/<family>.py).
from . import rules as _rule_families  # noqa: F401

import re
import sqlite3
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, quote, urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from markupsafe import escape

from . import db, descriptors, validators
from .security import read_session

router = APIRouter()

NO_STORE = {"Cache-Control": "no-store"}

#: Field types rendered in the monospace face (SIP-shaped values, plan §2).
MONO_TYPES = frozenset({"domain", "sip_uri", "digits", "ipv4", "regex"})

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_INTEGER = re.compile(r"^-?\d+$")


# ---------------------------------------------------------------------------
# extension hooks (T4.2–T4.8)
# ---------------------------------------------------------------------------
#: ``provider(descriptor, field, values) -> value | None``.
DefaultProvider = Callable[[Any, Any, Mapping[str, Any]], Any]
#: ``fn(*, descriptor, field, values, conn, mode, existing) -> value``.
Derivation = Callable[..., Any]

_DEFAULT_PROVIDERS: list[DefaultProvider] = []
DERIVATIONS: dict[str, Derivation] = {}

#: Derivation names declared in tables.yaml but owned by a later family.  They
#: are skipped (the stored/posted value is kept) until registered.
PENDING_DERIVATIONS: frozenset[str] = frozenset({"tenant_ds_onnet"})


def register_default(provider: DefaultProvider) -> None:
    """Register a create-time default provider (see module docstring)."""
    _DEFAULT_PROVIDERS.append(provider)


def register_derivation(name: str, fn: Derivation) -> None:
    """Register the implementation of a descriptor ``derived: name`` rule."""
    DERIVATIONS[name] = fn


def _default_for(descriptor, field, values: Mapping[str, Any]) -> Any:
    for provider in _DEFAULT_PROVIDERS:
        value = provider(descriptor, field, values)
        if value is not None:
            return value
    return None


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _wants_fragment(request: Request) -> bool:
    return request.headers.get("HX-Request", "").strip().lower() == "true"


def _requires_login(request: Request) -> RedirectResponse | None:
    if read_session(request) is None:
        return RedirectResponse("/login", status_code=303, headers=NO_STORE)
    return None


def _tableset() -> descriptors.TableSet:
    return descriptors.load()


def _descriptor(table: str) -> descriptors.Descriptor | None:
    return _tableset().tables.get(table)


def _humanize(name: str) -> str:
    return name.replace("_", " ").replace("-", " ").title()


def _checked_identifier(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"unsafe SQL identifier: {value!r}")
    return value


def _templates(request: Request):
    return request.app.state.templates


def _key_field(descriptor: descriptors.Descriptor):
    """The descriptor field that owns a packed key, or ``None``."""
    if not descriptor.key:
        return None
    for field in descriptor.fields:
        if field.column == descriptor.key and field.pack_position is None:
            return field
    return None


def _is_key_field(descriptor: descriptors.Descriptor, field: descriptors.Field) -> bool:
    key_field = _key_field(descriptor)
    return key_field is not None and field.name == key_field.name


def _is_immutable(
    descriptor: descriptors.Descriptor, field: descriptors.Field, mode: str
) -> bool:
    """True when the control is disabled and its value is taken from the row."""
    if field.locked or field.readonly or field.derived:
        return True
    return mode == "edit" and _is_key_field(descriptor, field)


def _row_key(
    conn: sqlite3.Connection, descriptor: descriptors.Descriptor, row: Mapping[str, Any]
) -> Any:
    if descriptor.key:
        return row.get(descriptor.key)
    return row.get(db.key_column(conn, descriptor.table))


def _action(descriptor: descriptors.Descriptor, mode: str, key: Any = None) -> str:
    if mode == "create":
        return f"/tables/{descriptor.table}/new"
    return f"/tables/{descriptor.table}/{quote(str(key), safe='')}/edit"


def _submit_label(mode: str) -> str:
    return "Create & reload routing" if mode == "create" else "Save & reload routing"


# ---------------------------------------------------------------------------
# form value collection / type coercion
# ---------------------------------------------------------------------------
def _posted(form: Mapping[str, list[str]], field: descriptors.Field) -> str:
    values = form.get(field.name)
    if field.widget == "checkbox":
        # An unchecked checkbox is simply absent from the payload; a disabled
        # checkbox is represented by its hidden input, whose value must win.
        return values[0] if values else "0"
    return values[0] if values else ""


def _collect_values(
    descriptor: descriptors.Descriptor,
    form: Mapping[str, list[str]],
    *,
    mode: str,
    existing: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge posted values with the immutable values that must survive.

    Disabled controls (packed key on edit, ``locked``, ``readonly`` and
    ``derived`` fields) are carried in a hidden input, but the server treats
    the stored value as authoritative on edit.  That is the key-immutability
    contract: a forged POST cannot rename a packed key.
    """
    values: dict[str, Any] = {}
    for field in descriptor.fields:
        if _is_immutable(descriptor, field, mode):
            if mode == "edit" and field.name in existing:
                values[field.name] = existing[field.name]
            else:
                values[field.name] = _posted(form, field)
        else:
            values[field.name] = _posted(form, field)

    if mode == "create":
        # A create form has no stored row; give later families a place to
        # supply fixed values for locked controls without touching templates.
        for field in descriptor.fields:
            if field.locked or field.readonly or field.derived:
                if str(values.get(field.name, "")).strip() == "":
                    default = _default_for(descriptor, field, values)
                    if default is not None:
                        values[field.name] = default
    return values


def _apply_derivations(
    descriptor: descriptors.Descriptor,
    values: dict[str, Any],
    *,
    conn: sqlite3.Connection,
    mode: str,
    existing: Mapping[str, Any],
) -> list[tuple[descriptors.Field, str]]:
    """Run registered ``derived`` rules; return per-field configuration errors."""
    problems: list[tuple[descriptors.Field, str]] = []
    for field in descriptor.fields:
        if not field.derived:
            continue
        derivation = DERIVATIONS.get(field.derived)
        if derivation is None:
            if field.derived not in PENDING_DERIVATIONS:
                problems.append(
                    (
                        field,
                        f"Unsupported derivation {field.derived!r} "
                        "(console configuration error).",
                    )
                )
            continue
        try:
            values[field.name] = derivation(
                descriptor=descriptor,
                field=field,
                values=values,
                conn=conn,
                mode=mode,
                existing=existing,
            )
        except Exception as exc:  # a family rule must not take down the form
            problems.append((field, f"Could not derive this value: {exc}."))
    return problems


def _coerce(
    descriptor: descriptors.Descriptor, values: dict[str, Any]
) -> tuple[dict[str, Any], list[tuple[descriptors.Field, str]]]:
    """Coerce submitted text to DB types and report type errors."""
    coerced: dict[str, Any] = {}
    problems: list[tuple[descriptors.Field, str]] = []
    for field in descriptor.fields:
        value = values.get(field.name, "")
        if field.type == "int":
            text = "" if value is None else str(value).strip()
            if text == "":
                # Packed fields are caught by the mandatory rule; a columns
                # integer with no value means the DB default (0).
                coerced[field.name] = 0 if descriptor.storage == "columns" else ""
            elif _INTEGER.fullmatch(text):
                coerced[field.name] = int(text)
            else:
                coerced[field.name] = value
                problems.append((field, "Enter a whole number."))
        else:
            coerced[field.name] = "" if value is None else str(value)
    return coerced, problems


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------
def _cross_ref_exists(
    conn: sqlite3.Connection, ref_table: str, ref_field: str, value: Any
) -> bool:
    """True when one referenced row has *value* in *ref_field*.

    Real columns are checked with a single indexed SELECT.  A reference into a
    **packed** table that points at a non-key field has no SQL column to
    compare (the value lives in ``tvalue``), so it is checked by unpacking the
    rows through the descriptor.  Keeping this generic means the family
    validators can still add richer per-family messages without duplicating
    existence checks.
    """
    descriptor = _tableset().tables.get(ref_table)
    if descriptor is not None and descriptor.storage == "packed":
        try:
            packed_field = descriptor.field(ref_field)
        except KeyError:
            packed_field = None
        if packed_field is not None and packed_field.column is None:
            try:
                rows = db.read_rows(conn, ref_table)
            except sqlite3.Error:
                return False
            for row in rows:
                try:
                    unpacked = descriptors.unpack_row(descriptor, row)
                except (ValueError, KeyError):
                    continue
                if str(unpacked.get(packed_field.name, "")) == str(value):
                    return True
            return False

    try:
        _checked_identifier(ref_table)
        _checked_identifier(ref_field)
        found = conn.execute(
            f'SELECT 1 FROM "{ref_table}" WHERE "{ref_field}" = ? LIMIT 1',
            (value,),
        ).fetchone()
    except sqlite3.Error:
        return False
    return found is not None


def _validate(
    descriptor: descriptors.Descriptor,
    values: Mapping[str, Any],
    *,
    conn: sqlite3.Connection,
    mode: str,
    key: Any,
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    errors: dict[str, list[str]] = {}
    warnings: dict[str, list[str]] = {}

    def add(bucket: dict[str, list[str]], field_name: str, message: str) -> None:
        bucket.setdefault(field_name, []).append(message)

    context = validators.ValidationContext(
        descriptor=descriptor,
        values=values,
        conn=conn,
        mode=mode,
        key=None if key is None else str(key),
    )

    for field in descriptor.fields:
        value = values.get(field.name, "")

        # Packed records have no way to express "absent": an empty segment
        # shifts every later field and corrupts the row, so every packed field
        # is mandatory.  Columns tables may be empty; a family validator can
        # add `required` where a value is genuinely needed.
        if descriptor.storage == "packed":
            field_errors, field_warnings = validators.required(value, context)
            for message in field_errors:
                add(errors, field.name, message)
            for message in field_warnings:
                add(warnings, field.name, message)

        field_errors, field_warnings = validators.validate_field(field, value, context)
        for message in field_errors:
            add(errors, field.name, message)
        for message in field_warnings:
            add(warnings, field.name, message)

    # Key uniqueness is structural: catch a duplicate before the DB does so
    # the summary can point at the key field.
    key_field = _key_field(descriptor)
    if mode == "create" and key_field is not None:
        key_value = values.get(key_field.name, "")
        if str(key_value).strip():
            try:
                duplicate = db.read_row(conn, descriptor.table, key_value)
            except sqlite3.Error:
                duplicate = None
            if duplicate is not None:
                add(errors, key_field.name, "A row with this key already exists.")

    # Generic descriptor cross-refs (plan §5.1).  "0"/empty means "none" for
    # integer reference fields, matching the registry's ds_pstn=0 convention.
    for cross_ref in descriptor.cross_refs:
        field = descriptor.field(cross_ref.field)
        value = values.get(field.name, "")
        text = "" if value is None else str(value).strip()
        if text == "" or text == "0":
            continue
        try:
            ref_table, ref_column = cross_ref.ref.split(".", 1)
        except ValueError:
            ref_table, ref_column = cross_ref.ref, ""
        if not _cross_ref_exists(conn, ref_table, ref_column, value):
            message = (
                f"Choose an existing {_humanize(ref_table)} {ref_column} ({text}), "
                "or create it first."
            )
            if cross_ref.on_missing == "warn":
                add(warnings, field.name, message)
            else:
                add(errors, field.name, message)

    return errors, warnings


# ---------------------------------------------------------------------------
# field view models for the template
# ---------------------------------------------------------------------------
def _options(
    conn: sqlite3.Connection | None,
    field: descriptors.Field,
    current: Any,
) -> list[dict[str, Any]]:
    values: list[str] = []
    if field.options_query and conn is not None:
        try:
            table, column = field.options_query.split(".", 1)
            _checked_identifier(table)
            _checked_identifier(column)
            rows = conn.execute(
                f'SELECT DISTINCT "{column}" FROM "{table}" '
                f'WHERE "{column}" IS NOT NULL ORDER BY 1'
            ).fetchall()
            values = [str(row[0]) for row in rows]
        except sqlite3.Error:
            values = []
    elif field.type == "enum01":
        values = ["1", "0"]

    current_text = "" if current is None else str(current)
    if current_text and current_text not in values:
        values.append(current_text)
    options = [
        {"value": option, "label": option, "selected": option == current_text}
        for option in values
    ]
    if field.options_query and current_text == "":
        options.insert(0, {"value": "", "label": "— choose —", "selected": True})
    return options


def _field_views(
    descriptor: descriptors.Descriptor,
    values: Mapping[str, Any],
    *,
    conn: sqlite3.Connection | None,
    mode: str,
    errors: Mapping[str, list[str]],
    warnings: Mapping[str, list[str]],
) -> list[dict[str, Any]]:
    ordered = sorted(
        enumerate(descriptor.fields), key=lambda item: (item[1].form_order, item[0])
    )
    views: list[dict[str, Any]] = []
    for _, field in ordered:
        field_errors = list(errors.get(field.name, []))
        field_warnings = list(warnings.get(field.name, []))
        describedby: list[str] = []
        if field.help:
            describedby.append(f"help-{field.name}")
        describedby.extend(f"err-{field.name}-{i}" for i in range(1, len(field_errors) + 1))
        describedby.extend(
            f"warn-{field.name}-{i}" for i in range(1, len(field_warnings) + 1)
        )

        immutable_key = mode == "edit" and _is_key_field(descriptor, field)
        disabled = (
            immutable_key
            or field.locked
            or field.readonly
            or bool(field.derived)
        )
        extra_hint = ""
        if immutable_key:
            extra_hint = (
                "Create-only — this identifier cannot be changed after the row "
                "is created."
            )
        elif field.locked:
            extra_hint = "Fixed by design — this value is not editable."
        elif field.readonly or field.derived:
            extra_hint = "Read-only — derived by the console."

        value = values.get(field.name, "")
        text = "" if value is None else str(value)
        checked = text.strip().lower() in {"1", "on", "true", "yes"}

        views.append(
            {
                "name": field.name,
                "label": _humanize(field.name),
                "help": field.help,
                "extra_hint": extra_hint,
                "widget": field.widget,
                "type": field.type,
                "mono": field.type in MONO_TYPES,
                "value": text,
                "checked": checked,
                "disabled": disabled,
                "locked": field.locked,
                "readonly": field.readonly or bool(field.derived),
                "key_locked": immutable_key,
                "required": descriptor.storage == "packed",
                "errors": field_errors,
                "warnings": field_warnings,
                "aria_invalid": bool(field_errors),
                "describedby": " ".join(describedby),
                "options": _options(conn, field, value),
            }
        )
    return views


def _summary(
    descriptor: descriptors.Descriptor, errors: Mapping[str, list[str]]
) -> list[dict[str, str]]:
    flat: list[dict[str, str]] = []
    for field in descriptor.fields:
        for message in errors.get(field.name, []):
            flat.append(
                {
                    "field": field.name,
                    "label": _humanize(field.name),
                    "message": message,
                }
            )
    return flat


def _warning_messages(
    descriptor: descriptors.Descriptor, warnings: Mapping[str, list[str]]
) -> list[str]:
    flat: list[str] = []
    for field in descriptor.fields:
        flat.extend(warnings.get(field.name, []))
    return flat


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------
def _not_found(request: Request, message: str) -> HTMLResponse:
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
            '<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">'
            "<title>Not found — VoIP Edge Console</title></head>"
            f'<body><main class="main" id="main">{panel}</main></body></html>'
        )
    return HTMLResponse(body, status_code=404, headers=NO_STORE)


def _render(
    request: Request,
    descriptor: descriptors.Descriptor,
    *,
    mode: str,
    key: Any,
    values: Mapping[str, Any],
    conn: sqlite3.Connection | None,
    errors: Mapping[str, list[str]] | None = None,
    warnings: Mapping[str, list[str]] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    errors = errors or {}
    warnings = warnings or {}
    fields = _field_views(
        descriptor,
        values,
        conn=conn,
        mode=mode,
        errors=errors,
        warnings=warnings,
    )
    summary = _summary(descriptor, errors)
    warning_list = _warning_messages(descriptor, warnings)
    return _templates(request).TemplateResponse(
        request,
        "form.html",
        {
            "nav": "tables",
            "fragment": _wants_fragment(request),
            "table": descriptor.table,
            "table_label": _humanize(descriptor.table),
            "mode": mode,
            "key": "" if key is None else str(key),
            "form_title": (
                f"New {_humanize(descriptor.table)}"
                if mode == "create"
                else f"Edit {_humanize(descriptor.table)}: {key}"
            ),
            "action": _action(descriptor, mode, key),
            "cancel_url": f"/tables/{descriptor.table}",
            "submit_label": _submit_label(mode),
            "fields": fields,
            "summary": summary,
            "error_count": len(summary),
            "warning_count": len(warning_list),
            "warnings": warning_list,
        },
        status_code=status_code,
        headers=NO_STORE,
    )


def _toast_response(
    request: Request,
    *,
    message: str,
    warnings: list[str],
    tone: str = "scheduled",
    status_code: int = 200,
    table: str | None = None,
) -> HTMLResponse:
    return _templates(request).TemplateResponse(
        request,
        "partials/toast.html",
        {
            "message": message,
            "warnings": warnings,
            "tone": tone,
            "table": table,
            "table_label": _humanize(table) if table else None,
        },
        status_code=status_code,
        headers=NO_STORE,
    )


# ---------------------------------------------------------------------------
# reload worker (lazily constructed in app.py; tests may pre-inject)
# ---------------------------------------------------------------------------
def _worker(request: Request):
    getter = getattr(request.app.state, "get_worker", None)
    if getter is not None:
        return getter()
    worker = getattr(request.app.state, "worker", None)
    if worker is None:
        factory = getattr(request.app.state, "worker_factory", None)
        if factory is None:
            raise RuntimeError("reload worker is not configured on app.state")
        worker = factory()
        request.app.state.worker = worker
    return worker


def _reload_intent(descriptor: descriptors.Descriptor):
    """The exact tuple this form enqueues after a successful write."""
    return (descriptor.table, descriptor.reload.rpc, list(descriptor.reload.params))


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------
def _blank_values(descriptor: descriptors.Descriptor) -> dict[str, Any]:
    values = {field.name: "" for field in descriptor.fields}
    for field in descriptor.fields:
        if field.locked or field.readonly or field.derived:
            default = _default_for(descriptor, field, values)
            if default is not None:
                values[field.name] = default
    return values


@router.get("/tables/{table}/new", response_class=HTMLResponse)
def new_form(
    request: Request,
    table: str,
    conn: sqlite3.Connection | None = Depends(db.get_read_db),
):
    redirect = _requires_login(request)
    if redirect is not None:
        return redirect
    descriptor = _descriptor(table)
    if descriptor is None:
        return _not_found(request, f"No table named “{table}”.")

    values = _blank_values(descriptor)
    return _render(
        request,
        descriptor,
        mode="create",
        key=None,
        values=values,
        conn=conn,
    )


@router.get("/tables/{table}/{key}/edit", response_class=HTMLResponse)
def edit_form(
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

    values = descriptors.unpack_row(descriptor, raw)
    if conn is not None:
        _apply_derivations(
            descriptor,
            values,
            conn=conn,
            mode="edit",
            existing=descriptors.unpack_row(descriptor, raw),
        )
    return _render(
        request,
        descriptor,
        mode="edit",
        key=key,
        values=values,
        conn=conn,
    )


# ---------------------------------------------------------------------------
# writes
# ---------------------------------------------------------------------------
async def _form_data(request: Request) -> dict[str, list[str]]:
    # The forms are application/x-www-form-urlencoded; parsing them directly
    # keeps python-multipart out of the dependency set (same as /login).
    body = await request.body()
    return parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)


def _insert(
    conn: sqlite3.Connection,
    descriptor: descriptors.Descriptor,
    values: Mapping[str, Any],
) -> Any:
    row = descriptors.pack_row(descriptor, values)
    if not row:
        raise ValueError(f"{descriptor.table}: nothing to insert")
    columns = list(row)
    column_sql = ", ".join(f'"{_checked_identifier(column)}"' for column in columns)
    mark_sql = ", ".join("?" for _ in columns)
    cursor = conn.execute(
        f'INSERT INTO "{descriptor.table}" ({column_sql}) VALUES ({mark_sql})',
        [row[column] for column in columns],
    )
    return cursor.lastrowid


def _update(
    conn: sqlite3.Connection,
    descriptor: descriptors.Descriptor,
    values: Mapping[str, Any],
    original_key: Any,
) -> None:
    row = descriptors.pack_row(descriptor, values)
    pk = descriptor.key or db.key_column(conn, descriptor.table)
    _checked_identifier(pk)
    assignments = [column for column in row if column != pk]
    if not assignments:
        return
    set_sql = ", ".join(
        f'"{_checked_identifier(column)}" = ?' for column in assignments
    )
    conn.execute(
        f'UPDATE "{descriptor.table}" SET {set_sql} WHERE "{pk}" = ?',
        [row[column] for column in assignments] + [original_key],
    )


def _row_identifier(
    descriptor: descriptors.Descriptor,
    values: Mapping[str, Any],
    original_key: Any,
) -> str:
    key_field = _key_field(descriptor)
    if key_field is not None:
        identifier = values.get(key_field.name) or original_key
        return str(identifier)
    return f"{_humanize(descriptor.table)} #{original_key}"


async def _submit(
    request: Request,
    table: str,
    mode: str,
    key: str | None,
    conn: sqlite3.Connection,
):
    redirect = _requires_login(request)
    if redirect is not None:
        return redirect
    descriptor = _descriptor(table)
    if descriptor is None:
        return _not_found(request, f"No table named “{table}”.")

    existing_raw = None
    existing: dict[str, Any] = {}
    if mode == "edit":
        try:
            existing_raw = db.read_row(conn, table, key)
        except sqlite3.Error:
            existing_raw = None
        if existing_raw is None:
            return _not_found(request, f"No row “{key}” in {_humanize(table)}.")
        existing = descriptors.unpack_row(descriptor, existing_raw)

    form = await _form_data(request)
    values = _collect_values(descriptor, form, mode=mode, existing=existing)
    derivation_problems = _apply_derivations(
        descriptor, values, conn=conn, mode=mode, existing=existing
    )
    values, type_problems = _coerce(descriptor, values)

    errors, warnings = _validate(
        descriptor, values, conn=conn, mode=mode, key=key
    )
    for field, message in derivation_problems + type_problems:
        errors.setdefault(field.name, []).append(message)

    if errors:
        return _render(
            request,
            descriptor,
            mode=mode,
            key=key,
            values=values,
            conn=conn,
            errors=errors,
            warnings=warnings,
            status_code=422,
        )

    original_key = key
    if existing_raw is not None:
        original_key = _row_key(conn, descriptor, existing_raw)

    inserted_key: Any = None
    try:
        with db.write_transaction(conn):
            if mode == "create":
                inserted_key = _insert(conn, descriptor, values)
            else:
                _update(conn, descriptor, values, original_key)
    except sqlite3.IntegrityError:
        key_field = _key_field(descriptor)
        target = key_field.name if key_field is not None else descriptor.fields[0].name
        errors.setdefault(target, []).append(
            "A row with this key already exists."
        )
        return _render(
            request,
            descriptor,
            mode=mode,
            key=key,
            values=values,
            conn=conn,
            errors=errors,
            warnings=warnings,
            status_code=422,
        )

    row_key = original_key if mode == "edit" else inserted_key
    identifier = _row_identifier(descriptor, values, row_key)
    message = f"Saved {identifier} — reload scheduled shortly"

    warning_messages = _warning_messages(descriptor, warnings)
    try:
        worker = _worker(request)
        worker_table, rpc_name, params = _reload_intent(descriptor)
        worker.enqueue(worker_table, rpc_name, params)
    except Exception as exc:  # a scheduling failure must not undo the write
        warning_messages.append(
            f"The row was saved, but the reload could not be scheduled: {exc}"
        )

    if _wants_fragment(request):
        return _toast_response(
            request,
            message=message,
            warnings=warning_messages,
            tone="scheduled",
            table=descriptor.table,
        )

    flash_message = message
    if warning_messages:
        flash_message = (
            message + " · Warning: " + " | ".join(warning_messages)
        )
    query = urlencode({"flash": flash_message, "tone": "scheduled"})
    return RedirectResponse(
        f"/tables/{descriptor.table}?{query}",
        status_code=303,
        headers=NO_STORE,
    )


# The POST handlers take the write connection as a normal FastAPI
# dependency, so both create and edit share one implementation.
async def _new_submit(
    request: Request,
    table: str,
    conn: sqlite3.Connection = Depends(db.get_db),
):
    return await _submit(request, table, "create", None, conn)


async def _edit_submit(
    request: Request,
    table: str,
    key: str,
    conn: sqlite3.Connection = Depends(db.get_db),
):
    return await _submit(request, table, "edit", key, conn)


router.add_api_route(
    "/tables/{table}/new",
    _new_submit,
    methods=["POST"],
    response_class=HTMLResponse,
)
router.add_api_route(
    "/tables/{table}/{key}/edit",
    _edit_submit,
    methods=["POST"],
    response_class=HTMLResponse,
)
