"""Simplified Number rules pages over the standard dialplan table."""
from __future__ import annotations

import re
import sqlite3
from urllib.parse import parse_qs, quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import db, descriptors, number_rules, settings as settings_module, validators
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
    all_warnings = list(warnings or [])
    if params.get("warn"):
        all_warnings.extend(
            part for part in params["warn"].split("; ") if part
        )
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
            "custom_partitions": [p for p in catalog.values() if "custom" in p["roles"]],
            "groups": groups,
            "values": values or {},
            "errors": errors or {},
            "warnings": all_warnings,
            "advanced_url": "/tables/dialplan",
            "saved": params.get("saved", ""),
            "status_code": status_code,
        },
        status_code=status_code,
        headers=NO_STORE,
    )


@router.get("/number-rules", response_class=HTMLResponse)
def number_rules_page(
    request: Request,
    tab: str = "emergency",
    conn: sqlite3.Connection | None = Depends(db.get_read_db),
):
    redirect = _requires_login(request)
    if redirect is not None:
        return redirect
    return _render(request, conn=conn, tab=tab)


def _worker(request: Request):
    getter = getattr(request.app.state, "get_worker", None)
    if getter is not None:
        return getter()
    worker = getattr(request.app.state, "worker", None)
    if worker is not None:
        return worker
    factory = getattr(request.app.state, "worker_factory", None)
    if factory is None:
        raise RuntimeError("reload worker is not configured")
    worker = factory()
    request.app.state.worker = worker
    return worker


async def _form_data(request: Request) -> dict[str, list[str]]:
    body = await request.body()
    return parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)


def _one(form: dict[str, list[str]], name: str) -> str:
    values = form.get(name)
    return values[0].strip() if values else ""


def _partition_sets(catalog: dict[int, dict]) -> tuple[set[int], set[int]]:
    emergency = {
        dpid for dpid, entry in catalog.items() if "emergency" in entry["roles"]
    }
    blocked = {
        dpid for dpid, entry in catalog.items() if "blocked" in entry["roles"]
    }
    return emergency, blocked


def _validate_row(
    values: dict,
    conn: sqlite3.Connection | None,
) -> tuple[dict[str, list[str]], list[str]]:
    descriptor = descriptors.load().tables["dialplan"]
    errors: dict[str, list[str]] = {}
    warnings: list[str] = []
    mapping = {
        "dpid": "dpid",
        "pr": "priority",
        "match_exp": "pattern",
        "match_len": "match_len",
        "subst_exp": "subst_exp",
        "repl_exp": "repl_exp",
        "attrs": "description",
    }
    for field_name, target in mapping.items():
        field = descriptor.field(field_name)
        context = validators.ValidationContext(
            descriptor=descriptor,
            field=field,
            values=values,
            conn=conn,
            mode="create",
            key=None,
        )
        field_errors, field_warnings = validators.validate_field(
            field, values[field_name], context
        )
        for message in field_errors:
            errors.setdefault(target, []).append(message)
        for message in field_warnings:
            warnings.append(message)
    return errors, warnings


def _save(
    request: Request,
    conn: sqlite3.Connection,
    *,
    tab: str,
    values: dict,
    label: str,
    warning: str = "",
):
    descriptor = descriptors.load().tables["dialplan"]
    errors, warnings = _validate_row(values, conn)
    if warning:
        warnings.append(warning)
    if errors:
        return _render(
            request,
            conn=conn,
            tab=tab,
            values=values,
            errors=errors,
            warnings=warnings,
            status_code=422,
        )

    duplicate_id = None
    with db.write_transaction(conn):
        existing = conn.execute(
            "SELECT id FROM dialplan WHERE dpid = ? AND match_exp = ?",
            (values["dpid"], values["match_exp"]),
        ).fetchone()
        if existing is not None:
            duplicate_id = existing[0]
        else:
            conn.execute(
                "INSERT INTO dialplan "
                "(dpid, pr, match_op, match_exp, match_len, subst_exp, repl_exp, attrs) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    values["dpid"],
                    values["pr"],
                    1,
                    values["match_exp"],
                    0,
                    "^(.*)$",
                    "\\1",
                    values["attrs"],
                ),
            )
    if duplicate_id is not None:
        errors = {"pattern": [f"This rule already exists (row {duplicate_id})."]}
        return _render(
            request,
            conn=conn,
            tab=tab,
            values=values,
            errors=errors,
            warnings=warnings,
            status_code=422,
        )

    try:
        worker = _worker(request)
        worker.enqueue(
            descriptor.table,
            descriptor.reload.rpc,
            list(descriptor.reload.params),
        )
    except Exception as exc:  # noqa: BLE001 - row is already saved
        warnings.append(f"The rule was saved, but the reload could not be scheduled: {exc}")

    target = f"/number-rules?tab={tab}&saved={quote(label, safe='')}"
    if warnings:
        target += "&warn=" + quote("; ".join(warnings))
    return RedirectResponse(target, status_code=303, headers=NO_STORE)


def _base_values(
    dpid: int, pattern: str, description: str, priority: int = 1
) -> dict:
    return {
        "dpid": dpid,
        "pr": priority,
        "match_op": 1,
        "match_exp": pattern,
        "match_len": 0,
        "subst_exp": "^(.*)$",
        "repl_exp": "\\1",
        "attrs": description,
    }


@router.post("/number-rules/emergency", response_class=HTMLResponse)
async def add_emergency(request: Request, conn: sqlite3.Connection = Depends(db.get_db)):
    redirect = _requires_login(request)
    if redirect is not None:
        return redirect
    if settings_module.settings.readonly:
        return HTMLResponse("The console is read-only.", status_code=403, headers=NO_STORE)

    form = await _form_data(request)
    raw = _one(form, "numbers")
    description = _one(form, "description") or "Emergency numbers"
    dpid = _one(form, "dpid")
    emergency, _blocked = _partition_sets(_catalog(conn))
    errors: dict[str, list[str]] = {}
    values = {
        "dpid": dpid,
        "numbers": raw,
        "description": _one(form, "description"),
    }
    try:
        numbers = number_rules.parse_entries(raw)
        if not numbers:
            errors["numbers"] = ["Enter at least one emergency number."]
        elif any(len(number) < 2 for number in numbers):
            errors["numbers"] = ["Use at least two digits per emergency number."]
    except ValueError as exc:
        errors["numbers"] = [str(exc)]
        numbers = []
    try:
        partition = int(dpid)
    except ValueError:
        partition = 0
    warning = ""
    if partition <= 0:
        errors["dpid"] = ["Enter a positive partition id."]
    elif partition not in emergency:
        warning = (
            f"Partition {partition} is not assigned as an emergency partition; "
            "set it on the home after saving."
        )
    if errors:
        return _render(
            request, conn=conn, tab="emergency", values=values, errors=errors,
            status_code=422,
        )
    return _save(
        request,
        conn,
        tab="emergency",
        values=_base_values(partition, number_rules.exact_pattern(numbers), description),
        label="Emergency numbers",
        warning=warning,
    )


@router.post("/number-rules/blocked", response_class=HTMLResponse)
async def add_blocked(request: Request, conn: sqlite3.Connection = Depends(db.get_db)):
    redirect = _requires_login(request)
    if redirect is not None:
        return redirect
    if settings_module.settings.readonly:
        return HTMLResponse("The console is read-only.", status_code=403, headers=NO_STORE)

    form = await _form_data(request)
    mode = _one(form, "mode") or "exact"
    raw = _one(form, "numbers")
    pattern = _one(form, "pattern")
    description = _one(form, "description")
    dpid = _one(form, "dpid")
    _emergency, blocked = _partition_sets(_catalog(conn))
    values = {
        "dpid": dpid,
        "mode": mode,
        "numbers": raw,
        "pattern": pattern,
        "range_start": _one(form, "range_start"),
        "range_end": _one(form, "range_end"),
        "description": description,
    }
    errors: dict[str, list[str]] = {}
    generated = ""
    try:
        if mode == "exact":
            generated = number_rules.exact_pattern(number_rules.parse_entries(raw))
        elif mode == "prefix":
            generated = number_rules.prefix_pattern(number_rules.parse_entries(raw))
        elif mode == "range":
            generated = number_rules.range_pattern(
                _one(form, "range_start"), _one(form, "range_end")
            )
        elif mode == "custom":
            generated = pattern
        else:
            errors["mode"] = ["Choose a valid match type."]
    except ValueError as exc:
        errors["range" if mode == "range" else "numbers"] = [str(exc)]
    try:
        partition = int(dpid)
    except ValueError:
        partition = 0
    warning = ""
    if partition <= 0:
        errors["dpid"] = ["Enter a positive partition id."]
    elif partition not in blocked:
        warning = (
            f"Partition {partition} is not assigned as a blocked-destinations "
            "partition; set it on the home after saving."
        )
    if not generated and not errors:
        errors["pattern"] = ["Enter a match pattern or the numbers to block."]
    if errors:
        return _render(
            request, conn=conn, tab="blocked", values=values, errors=errors,
            status_code=422,
        )
    return _save(
        request,
        conn,
        tab="blocked",
        values=_base_values(
            partition, generated, description or "Blocked numbers"
        ),
        label="Blocked rule",
        warning=warning,
    )


@router.post("/number-rules/custom", response_class=HTMLResponse)
async def add_custom(request: Request, conn: sqlite3.Connection = Depends(db.get_db)):
    redirect = _requires_login(request)
    if redirect is not None:
        return redirect
    if settings_module.settings.readonly:
        return HTMLResponse("The console is read-only.", status_code=403, headers=NO_STORE)

    form = await _form_data(request)
    dpid = _one(form, "dpid")
    pattern = _one(form, "pattern")
    priority = _one(form, "priority") or "1"
    description = _one(form, "description")
    values = {
        "dpid": dpid,
        "priority": priority,
        "pattern": pattern,
        "description": description,
    }
    errors: dict[str, list[str]] = {}
    try:
        partition = int(dpid)
        if partition <= 0:
            raise ValueError
    except ValueError:
        partition = 0
        errors["dpid"] = ["Enter a positive partition id."]
    try:
        pr = int(priority)
    except ValueError:
        pr = 0
        errors["priority"] = ["Priority must be a whole number from 1 to 100."]
    warning = ""
    catalog = _catalog(conn)
    entry = catalog.get(partition)
    if entry is not None and not ({"emergency", "blocked"} & entry["roles"]):
        warning = (
            f"Partition {partition} is not assigned to a home, so this rule "
            "will not affect calls."
        )
    if errors:
        return _render(
            request, conn=conn, tab="custom", values=values, errors=errors,
            status_code=422,
        )
    return _save(
        request,
        conn,
        tab="custom",
        values=_base_values(partition, pattern, description, priority=pr),
        label="Custom rule",
        warning=warning,
    )


@router.get("/number-rules/preview", response_class=HTMLResponse)
def preview(
    request: Request,
    kind: str = "emergency",
    numbers: str = "",
    mode: str = "exact",
    range_start: str = "",
    range_end: str = "",
    pattern: str = "",
):
    redirect = _requires_login(request)
    if redirect is not None:
        return HTMLResponse("", status_code=401, headers=NO_STORE)
    try:
        if kind == "emergency":
            generated = number_rules.exact_pattern(number_rules.parse_entries(numbers))
        elif kind == "blocked" and mode == "exact":
            generated = number_rules.exact_pattern(number_rules.parse_entries(numbers))
        elif kind == "blocked" and mode == "prefix":
            generated = number_rules.prefix_pattern(number_rules.parse_entries(numbers))
        elif kind == "blocked" and mode == "range":
            generated = number_rules.range_pattern(range_start, range_end)
        elif kind == "blocked" and mode == "custom":
            generated = pattern
        else:
            generated = pattern
    except ValueError:
        generated = ""
    if not generated:
        return HTMLResponse(
            '<span class="hint">Enter values to preview the pattern.</span>',
            headers=NO_STORE,
        )
    return HTMLResponse(
        '<span>Generated pattern</span> <code>'
        + str(generated).replace("&", "&amp;").replace("<", "&lt;")
        + "</code>",
        headers=NO_STORE,
    )
