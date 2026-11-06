"""wizard_ui.py — four-step onboarding wizard (plan §5.5, T7).

The wizard is deliberately **stateless**: every value the operator has typed is
carried between steps in hidden form inputs and re-validated on the next POST.
There is no draft row in ``console.sqlite`` and no sidecar migration.  A draft
therefore cannot outlive the browser tab, be resumed by another session, or
leave orphaned state behind — it also keeps this lane independent of the
sidecar schema owned by T8.

Flow
----
``GET  /wizard``                       step 1 · identity
``POST /wizard/step1``                 validate identity       -> step 2
``POST /wizard/step2``                 validate network        -> step 3
``POST /wizard/step3``                 review/apply (alias of /wizard/apply)
``POST /wizard/apply``                 apply the write batch   -> step 4
``GET  /wizard/verify/<domain>``       DB-only coherence checklist

The apply path is one ``edge.sqlite`` transaction that writes:

* ``tenant_registry``  (packed)
* ``tenant_landline``  (packed)
* ``dispatcher``       on-net row, plus a PSTN row when the home has a trunk
* ``trusted``          wire-observed source address -> home domain

That is **5 rows with a trunk, 4 without**.  Before the transaction the console
takes a tagged online backup (``wizard-apply:<domain>``).  Any failure rolls
the transaction back and re-renders the review step with a 422 summary; no
partial rows and no reload intents are left behind.  After a clean commit the
four reload keys (``tenantdom``, ``landline``, ``dispatcher``, ``trusted``) are
enqueued on the shared reload worker and flushed.

Validation is reused, not reimplemented: each wizard field is projected onto
its descriptor field in ``tables.yaml`` and validated through
:func:`console.validators.validate_field`.  ``tenant_registry`` references the
two dispatcher sets the wizard is about to create, so validation runs against a
small read-through connection shim that treats the *planned* set ids and home
domain as already present.  That avoids the false "create set 14 first" error
without weakening the real uniqueness rules.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import parse_qs, quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import db, descriptors, validators
from .backups import create_backup
from .errors import ValidationError
from .security import read_session, is_authenticated

router = APIRouter()

NO_STORE = {"Cache-Control": "no-store"}

#: Onboarding defaults: no emergency profile and no premium block until the
#: operator creates them in Number rules (dialplan CRUD).  0 means "none";
#: inter-home calling and PSTN fallback are on.
DEFAULT_DPID = 0
DEFAULT_FLAGS = "1+1"


def effective_flags(has_pstn: bool) -> str:
    """Call-policy flags derived from the trunk checkbox.

    ``ih_on+pstn_on[+block_dpid]``: inter-home stays on; ``pstn_on`` is only
    meaningful when the home actually has a PSTN set; no block partition is
    assumed (create one in Number rules and reference it per home).
    """
    return DEFAULT_FLAGS if has_pstn else "1+0"


DEFAULT_SIP_PORT = 5060

#: Visible wizard fields per step (used to decide which values travel hidden).
STEP_VISIBLE: dict[int, frozenset[str]] = {
    1: frozenset({"domain", "landline", "ring_group", "label"}),
    2: frozenset({"lan_ip", "wire_ip", "has_pstn", "override_public_ip"}),
    3: frozenset(),
    4: frozenset(),
}

STEP_LABELS = {
    1: "Identity",
    2: "Network",
    3: "Review & apply",
    4: "Verify",
}

FIELD_LABELS = {
    "domain": "Domain",
    "landline": "Landline",
    "ring_group": "Ring group / extension",
    "label": "Display label",
    "lan_ip": "voip-stack LAN IP",
    "wire_ip": "Wire-observed native IP",
    "has_pstn": "PSTN trunk",
    "tenantid": "Home id",
}

#: Reload keys in the exact order the edge expects for an onboarding batch.
RELOAD_KEYS: tuple[tuple[str, str], ...] = (
    ("tenantdom", "tenant_registry"),
    ("landline", "tenant_landline"),
    ("dispatcher", "dispatcher"),
    ("trusted", "trusted"),
)

_INTEGER = re.compile(r"^-?\d+$")
_DOMAIN_INDEX = re.compile(r"([0-9]+)$")


# ---------------------------------------------------------------------------
# draft model
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class WizardDraft:
    """Everything the wizard carries between steps (stateless, hidden inputs)."""

    domain: str = ""
    landline: str = ""
    ring_group: str = ""
    label: str = ""
    lan_ip: str = ""
    wire_ip: str = ""
    has_pstn: bool = False
    override_public_ip: bool = False
    tenantid: str = ""
    ds_onnet: int | None = None
    ds_pstn: int | None = None
    dpid: int = DEFAULT_DPID
    flags: str = DEFAULT_FLAGS

    def as_form(self) -> dict[str, str]:
        """Flat string mapping (hidden inputs and tests)."""
        return {
            "domain": self.domain,
            "landline": self.landline,
            "ring_group": self.ring_group,
            "label": self.label,
            "lan_ip": self.lan_ip,
            "wire_ip": self.wire_ip,
            "has_pstn": "1" if self.has_pstn else "0",
            "override_public_ip": "1" if self.override_public_ip else "0",
            "tenantid": self.tenantid,
            "ds_onnet": "" if self.ds_onnet is None else str(self.ds_onnet),
            "ds_pstn": "" if self.ds_pstn is None else str(self.ds_pstn),
            "dpid": str(self.dpid),
            "flags": self.flags,
        }


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "on", "true", "yes"}


def _int_or_none(value: Any) -> int | None:
    text = "" if value is None else str(value).strip()
    if text == "":
        return None
    if not _INTEGER.fullmatch(text):
        return None
    return int(text)


def draft_from_form(form: Mapping[str, list[str]]) -> WizardDraft:
    """Build a draft from a URL-encoded form payload."""
    def one(name: str) -> str:
        values = form.get(name)
        return values[0] if values else ""

    # Canonical field names first; the aliases keep older forms/tests working
    # without duplicating the draft anywhere.
    domain = one("domain") or one("tprefix")
    landline = one("landline") or one("cli")
    ring_group = one("ring_group") or one("target")
    lan_ip = one("lan_ip") or one("voip_stack_ip")
    wire_ip = one("wire_ip") or one("native_ip")
    has_pstn = one("has_pstn") or one("pstn_trunk")

    return WizardDraft(
        domain=domain.strip(),
        landline=landline.strip(),
        ring_group=ring_group.strip(),
        label=one("label").strip(),
        lan_ip=lan_ip.strip(),
        wire_ip=wire_ip.strip(),
        has_pstn=_truthy(has_pstn),
        override_public_ip=_truthy(one("override_public_ip")),
        tenantid=one("tenantid").strip(),
        ds_onnet=_int_or_none(one("ds_onnet")),
        ds_pstn=_int_or_none(one("ds_pstn")),
        dpid=_int_or_none(one("dpid")) or DEFAULT_DPID,
        flags=one("flags").strip() or DEFAULT_FLAGS,
    )


# ---------------------------------------------------------------------------
# derived identifiers (home id + dispatcher set convention 10+N / 30+N)
# ---------------------------------------------------------------------------
def _existing_ids(conn: sqlite3.Connection | None, tableset: descriptors.TableSet):
    """Return ``(tenant_ids, set_ids)`` already present in the edge DB."""
    tenant_ids: set[str] = set()
    set_ids: set[int] = set()
    if conn is None:
        return tenant_ids, set_ids

    registry = tableset.tables.get("tenant_registry")
    if registry is not None:
        try:
            for raw in db.read_rows(conn, "tenant_registry"):
                try:
                    values = descriptors.unpack_row(registry, raw)
                except (ValueError, KeyError):
                    continue
                tenant = str(values.get("tenantid") or "").strip()
                if tenant:
                    tenant_ids.add(tenant)
        except sqlite3.Error:
            pass

    try:
        for row in conn.execute("SELECT DISTINCT setid FROM dispatcher"):
            number = _int_or_none(row[0])
            if number is not None and number > 0:
                set_ids.add(number)
    except sqlite3.Error:
        pass
    return tenant_ids, set_ids


def _index_from(draft: WizardDraft) -> int | None:
    """The convention index N, read from the home id or the on-net set."""
    match = _DOMAIN_INDEX.search(draft.tenantid or "")
    if match is not None:
        return int(match.group(1))
    if draft.ds_onnet is not None and draft.ds_onnet >= 10:
        return draft.ds_onnet - 10
    return None


def _assign_ids(
    draft: WizardDraft,
    conn: sqlite3.Connection | None,
    tableset: descriptors.TableSet,
) -> WizardDraft:
    """Fill/stabilise the derived home id and dispatcher set ids.

    The wizard asks only for the four identity fields, so the tenant id and
    route-set ids follow the deployment convention: ``T<N>``, on-net
    ``10+N`` and PSTN ``30+N``.  ``N`` starts after the highest existing home
    and is advanced until the id and every set the home needs are free.
    """
    tenant_ids, set_ids = _existing_ids(conn, tableset)
    base = _index_from(draft)
    if base is None:
        highest = 0
        for tenant in tenant_ids:
            match = _DOMAIN_INDEX.search(tenant)
            if match is not None:
                highest = max(highest, int(match.group(1)))
        base = highest + 1
    base = max(base, 1)

    index = base
    while True:
        tenant = f"T{index}"
        onnet = 10 + index
        pstn = 30 + index if draft.has_pstn else 0
        tenant_free = tenant not in tenant_ids
        onnet_free = onnet not in set_ids
        pstn_free = not draft.has_pstn or pstn not in set_ids
        if tenant_free and onnet_free and pstn_free:
            break
        index += 1

    return replace(
        draft,
        tenantid=tenant,
        ds_onnet=onnet,
        ds_pstn=pstn,
        # pstn_on mirrors the trunk checkbox: a home without a PSTN set must
        # not advertise PSTN failover it cannot perform.
        flags=effective_flags(draft.has_pstn),
    )


# ---------------------------------------------------------------------------
# review rows (pure)
# ---------------------------------------------------------------------------
def destination_for(lan_ip: str, port: int = DEFAULT_SIP_PORT) -> str:
    """The literal-IPv4, TCP dispatcher destination the wizard writes."""
    return f"sip:{lan_ip}:{port};transport=tcp"


def plan_writes(
    draft: WizardDraft | Mapping[str, Any] | None = None,
    tableset: descriptors.TableSet | None = None,
    **overrides: Any,
) -> list[tuple[str, dict[str, Any]]]:
    """Pure plan: ``[(table, row), ...]`` for the whole wizard apply.

    Returns five rows when the home has a PSTN trunk and four when it does
    not.  Packed rows go through the descriptor codec, so the layout is
    declared exactly once in ``tables.yaml``.

    Accepting a mapping or keyword fields (in addition to a ``WizardDraft``)
    keeps the pure function easy to drive from tests without constructing the
    dataclass by hand.
    """
    if draft is None:
        draft = WizardDraft(**overrides)
    elif not isinstance(draft, WizardDraft):
        draft = WizardDraft(**{**dict(draft), **overrides})
    elif overrides:
        draft = replace(draft, **overrides)
    if tableset is None:
        tableset = descriptors.load()
    if not draft.tenantid:
        raise ValueError("wizard draft has no tenant id")
    if draft.ds_onnet is None:
        raise ValueError("wizard draft has no on-net set id")
    if draft.has_pstn and draft.ds_pstn in (None, 0):
        raise ValueError("wizard draft has no PSTN set id")

    registry = tableset.tables["tenant_registry"]
    landline = tableset.tables["tenant_landline"]

    registry_row = descriptors.pack_row(
        registry,
        {
            "tprefix": draft.domain,
            "tenantid": draft.tenantid,
            "ds_onnet": draft.ds_onnet,
            "ds_pstn": draft.ds_pstn or 0,
            "cli": draft.landline,
            "label": draft.label,
            "dpid": draft.dpid,
            "flags": effective_flags(draft.has_pstn),
        },
    )
    landline_row = descriptors.pack_row(
        landline,
        {
            "tprefix": draft.landline,
            "tenantid": draft.tenantid,
            "target": draft.ring_group,
            "ds_onnet": draft.ds_onnet,
            "ih_on": "1",
        },
    )

    destination = destination_for(draft.lan_ip)
    writes: list[tuple[str, dict[str, Any]]] = [
        ("tenant_registry", registry_row),
        ("tenant_landline", landline_row),
        (
            "dispatcher",
            {
                "setid": draft.ds_onnet,
                "destination": destination,
                "flags": 0,
                "priority": 0,
                "attrs": "",
                "description": f"{draft.domain} voip-stack (native routing)",
            },
        ),
    ]
    if draft.has_pstn and draft.ds_pstn:
        writes.append(
            (
                "dispatcher",
                {
                    "setid": draft.ds_pstn,
                    "destination": destination,
                    "flags": 0,
                    "priority": 0,
                    "attrs": "",
                    "description": (
                        f"{draft.domain} PSTN via voip-stack (LAN-only)"
                    ),
                },
            )
        )
    writes.append(
        (
            "trusted",
            {
                "src_ip": draft.wire_ip,
                "proto": "any",
                "from_pattern": "",
                "ruri_pattern": "",
                "tag": draft.domain,
                "priority": 0,
            },
        )
    )
    return writes


# ---------------------------------------------------------------------------
# read-through connection shim for planned rows
# ---------------------------------------------------------------------------
class _Rows:
    """Tiny cursor-ish wrapper returning an explicit row list."""

    def __init__(self, rows: list[Any]):
        self._rows = rows

    def fetchall(self) -> list[Any]:
        return list(self._rows)

    def fetchone(self) -> Any | None:
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class _PlannedConn:
    """Delegate to a real sqlite connection, adding the rows this draft plans.

    Validators read existence through ordinary SQL.  During the wizard the
    two dispatcher sets and the home domain do not exist yet (they are written
    by the same transaction), so the shim answers those two queries with the
    planned values.  Everything else — uniqueness, duplicates, dialplan — is
    answered by the real database.
    """

    def __init__(
        self,
        conn: sqlite3.Connection | None,
        *,
        domains: Iterable[str] = (),
        setids: Iterable[int] = (),
    ) -> None:
        self._conn = conn
        self._domains = {str(value) for value in domains if str(value)}
        self._setids = {int(value) for value in setids if value}

    def execute(self, sql: str, params: Iterable[Any] = ()):  # noqa: ANN001
        params = tuple(params)
        if self._conn is None:
            return _Rows([])
        normal = " ".join(str(sql).split())

        if (
            normal.startswith('SELECT 1 FROM "dispatcher"')
            and '"setid"' in normal
            and params
        ):
            number = _int_or_none(params[0])
            if number is not None and number in self._setids:
                return _Rows([(1,)])

        if (
            "DISTINCT tprefix" in normal
            and "FROM tenant_registry" in normal
        ):
            rows = list(self._conn.execute(sql, params).fetchall())
            seen = {str(row[0]) for row in rows}
            rows.extend(
                (domain,) for domain in sorted(self._domains) if domain not in seen
            )
            return _Rows(rows)

        return self._conn.execute(sql, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


# ---------------------------------------------------------------------------
# validation (descriptor fields + console.validators)
# ---------------------------------------------------------------------------
def _validation_conn(
    conn: sqlite3.Connection | None, draft: WizardDraft
) -> _PlannedConn:
    setids = [draft.ds_onnet] if draft.ds_onnet is not None else []
    if draft.has_pstn and draft.ds_pstn:
        setids.append(draft.ds_pstn)
    return _PlannedConn(conn, domains=[draft.domain], setids=setids)


def _add(
    bucket: dict[str, list[str]], target: str, messages: Iterable[str]
) -> None:
    for message in messages:
        bucket.setdefault(target, []).append(message)


def _field_rule(
    descriptor: descriptors.Descriptor,
    field_name: str,
    value: Any,
    values: Mapping[str, Any],
    conn: sqlite3.Connection | None,
) -> tuple[list[str], list[str]]:
    field = descriptor.field(field_name)
    context = validators.ValidationContext(
        descriptor=descriptor,
        field=field,
        values=values,
        conn=conn,
        mode="create",
        key=None,
    )
    return validators.validate_field(field, value, context)


def _required(value: Any, name: str) -> list[str]:
    errors, _ = validators.required(value, validators.ValidationContext())
    if errors:
        return [f"Enter the {FIELD_LABELS.get(name, name).lower()}."]
    return []


def validate_step(
    step: int,
    draft: WizardDraft,
    conn: sqlite3.Connection | None,
    tableset: descriptors.TableSet | None = None,
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Validate one wizard step.

    Returns ``(errors, warnings)`` keyed by the **wizard field name** used in
    the template (``domain``, ``landline``, ``lan_ip``, …).  Step 1 covers
    identity, step 2 network; apply calls both.
    """
    if tableset is None:
        tableset = descriptors.load()
    errors: dict[str, list[str]] = {}
    warnings: dict[str, list[str]] = {}
    proxy = _validation_conn(conn, draft)
    registry = tableset.tables["tenant_registry"]
    landline = tableset.tables["tenant_landline"]

    if step == 1:
        _add(errors, "domain", _required(draft.domain, "domain"))
        _add(errors, "landline", _required(draft.landline, "landline"))
        _add(errors, "ring_group", _required(draft.ring_group, "ring_group"))
        _add(errors, "label", _required(draft.label, "label"))

        registry_values = {
            "tprefix": draft.domain,
            "tenantid": draft.tenantid,
            "ds_onnet": "" if draft.ds_onnet is None else draft.ds_onnet,
            "ds_pstn": 0 if draft.ds_pstn is None else draft.ds_pstn,
            "cli": draft.landline,
            "label": draft.label,
            "dpid": draft.dpid,
            "flags": effective_flags(draft.has_pstn),
        }
        landline_values = {
            "tprefix": draft.landline,
            "tenantid": draft.tenantid,
            "target": draft.ring_group,
            "ds_onnet": "" if draft.ds_onnet is None else draft.ds_onnet,
            "ih_on": "1",
        }

        # Domain / landline / label / ring-group shapes come from the
        # descriptors, never from a duplicated regex here.
        for field_name, target in (
            ("tprefix", "domain"),
            ("cli", "landline"),
            ("label", "label"),
        ):
            field_errors, field_warnings = _field_rule(
                registry, field_name, registry_values[field_name], registry_values, proxy
            )
            _add(errors, target, field_errors)
            _add(warnings, target, field_warnings)

        target_errors, target_warnings = _field_rule(
            landline, "target", landline_values["target"], landline_values, proxy
        )
        _add(errors, "ring_group", target_errors)
        _add(warnings, "ring_group", target_warnings)

        # The wizard writes tenant_landline directly, so it must run the same
        # cross-home longest-prefix guard as the generic form router.  A
        # cross-home clash is always blocking; the wizard has no "keep both"
        # acknowledgement.
        if conn is not None:
            from .rules.landline import check_prefix_conflict

            conflict = check_prefix_conflict(
                landline,
                landline_values,
                conn=proxy,
                mode="create",
                existing={},
                key=None,
            )
            if conflict is not None:
                _add(
                    errors,
                    "landline",
                    conflict.get("errors", {}).get(
                        "tprefix", ["This landline overlaps another home."]
                    ),
                )

        # Packed keys cannot contain commas/pluses; landline digits are part
        # of the landline family, but the generic rules keep the wizard honest
        # while that family is still landing.
        if True:  # always validate the draft landline (digits >= 3)
            digit_errors, _ = validators.digits(draft.landline, validators.ValidationContext())
            _add(errors, "landline", digit_errors)
            if draft.landline.isdigit() and len(draft.landline) < 3:
                _add(errors, "landline", ["Use at least three digits for the landline."])
        if not validators.is_known("target_identifier"):
            separator_errors, _ = validators.label_pack_safe(
                draft.ring_group, validators.ValidationContext()
            )
            _add(errors, "ring_group", separator_errors)

        # Derived fields are still validated: a duplicate tenant id or a
        # dialplan partition that vanished must block before any write.
        for field_name, target in (
            ("tenantid", "domain"),
            ("dpid", "domain"),
            ("flags", "domain"),
            ("ds_onnet", "domain"),
            ("ds_pstn", "domain"),
        ):
            value = registry_values[field_name]
            if value in (None, ""):
                continue
            field_errors, field_warnings = _field_rule(
                registry, field_name, value, registry_values, proxy
            )
            _add(errors, target, field_errors)
            _add(warnings, target, field_warnings)

        # The landline's tenant reference is the home being created by this
        # very transaction; running tenant_exists here would only see the old
        # registry rows and report a false negative.  The reference is
        # guaranteed structurally by plan_writes.

        # Structural key uniqueness (the descriptor's shape rule does not do
        # this; forms_ui adds the same check for its create path).
        if conn is not None and draft.domain:
            try:
                duplicate = db.read_row(conn, "tenant_registry", draft.domain)
            except sqlite3.Error:
                duplicate = None
            if duplicate is not None:
                _add(errors, "domain", [
                    "A home with this domain already exists — choose another."
                ])
        return errors, warnings

    if step == 2:
        _add(errors, "lan_ip", _required(draft.lan_ip, "lan_ip"))
        _add(errors, "wire_ip", _required(draft.wire_ip, "wire_ip"))

        destination = destination_for(draft.lan_ip)
        dispatcher = tableset.tables["dispatcher"]
        dispatcher_values = {
            "setid": draft.ds_onnet,
            "destination": destination,
            "flags": 0,
            "priority": 0,
            "attrs": "",
            "description": f"{draft.domain} voip-stack (native routing)",
        }
        for field_name in ("setid", "destination"):
            field_errors, field_warnings = _field_rule(
                dispatcher,
                field_name,
                dispatcher_values[field_name],
                dispatcher_values,
                proxy,
            )
            _add(errors, "lan_ip", field_errors)
            # The "not referenced by any home yet" warning would be false on
            # the review screen: this same transaction is the reference.
            if field_name != "setid":
                _add(warnings, "lan_ip", field_warnings)

        if draft.has_pstn:
            pstn_values = dict(dispatcher_values, setid=draft.ds_pstn)
            for field_name in ("setid", "destination"):
                field_errors, field_warnings = _field_rule(
                    dispatcher,
                    field_name,
                    pstn_values[field_name],
                    pstn_values,
                    proxy,
                )
                _add(errors, "wire_ip", field_errors)
                if field_name != "setid":
                    _add(warnings, "wire_ip", field_warnings)

        trusted = tableset.tables["trusted"]
        trusted_values = {
            "src_ip": draft.wire_ip,
            "proto": "any",
            "from_pattern": "",
            "ruri_pattern": "",
            "tag": draft.domain,
            "priority": 0,
            "override_public_ip": "1" if draft.override_public_ip else "",
        }
        for field_name in ("src_ip", "tag", "proto", "priority"):
            field_errors, field_warnings = _field_rule(
                trusted,
                field_name,
                trusted_values[field_name],
                trusted_values,
                proxy,
            )
            _add(errors, "wire_ip", field_errors)
            _add(warnings, "wire_ip", field_warnings)
        return errors, warnings

    raise ValueError(f"unknown wizard step {step!r}")


# ---------------------------------------------------------------------------
# helpers: form, auth, templates, reloads
# ---------------------------------------------------------------------------
def _wants_fragment(request: Request) -> bool:
    return request.headers.get("HX-Request", "").strip().lower() == "true"


def _requires_login(request: Request) -> RedirectResponse | None:
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303, headers=NO_STORE)
    return None


def _tableset() -> descriptors.TableSet:
    return descriptors.load()


async def _form_data(request: Request) -> dict[str, list[str]]:
    body = await request.body()
    return parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)


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


def _enqueue_reloads(
    request: Request, tableset: descriptors.TableSet
) -> tuple[list[str], list[str]]:
    """Enqueue tenantdom/landline/dispatcher/trusted once each, then flush."""
    worker = _worker(request)
    keys: list[str] = []
    warnings: list[str] = []
    for key, table in RELOAD_KEYS:
        descriptor = tableset.tables.get(table)
        if descriptor is None:
            continue
        worker.enqueue(key, descriptor.reload.rpc, list(descriptor.reload.params))
        keys.append(key)
    flush = getattr(worker, "flush", None)
    drained = True
    if callable(flush):
        try:
            # Must exceed the largest per-table throttle (5 s) plus the RPC
            # timeout, otherwise every apply reports a false failure.
            drained = bool(flush(timeout=30.0))
        except BaseException as exc:
            drained = False
            warnings.append(f"Reload queue did not drain: {exc}")
    errors = {
        table: err
        for table, err in (getattr(worker, "errors", {}) or {}).items()
        if table in keys
    }
    if errors:
        detail = ", ".join(f"{table}: {err}" for table, err in errors.items())
        warnings.append(
            "The home was saved, but these reloads failed and remain pending: "
            + detail
        )
    elif not drained:
        warnings.append(
            "The home was saved; reloads are still queued (Kamailio unreachable?)."
        )
    return keys, warnings


def _backup_paths() -> tuple[Path, Path]:
    from . import settings as settings_module

    sidecar_dir = Path(settings_module.settings.sidecar_dir)
    return sidecar_dir / "console.sqlite", sidecar_dir / "backups"


def wire_ip_hint(domain: str) -> str:
    """Inline one-liner for the wire-observed address (README onboarding 2)."""
    target = domain or "this home"
    return (
        "On the VPS, while the site is up, run "
        "`tcpdump -i pangolin -n 'udp port 5060'` and read the source address "
        f"of a REGISTER from {target}. Paste that address — not a DNS answer "
        "and not a 100.96.128.x utility alias."
    )


def verify_checklist(
    conn: sqlite3.Connection | None, domain: str
) -> dict[str, Any]:
    """DB-only coherence checklist for the verify step (no RPC, no N+1)."""
    items: list[dict[str, Any]] = []

    registry = None
    values: dict[str, Any] = {}
    if conn is not None:
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

    items.append(
        {
            "key": "registry",
            "label": "Home registered in the routing table",
            "ok": registry is not None,
            "detail": domain if registry is not None else "no registry row",
        }
    )

    onnet = _int_or_none(values.get("ds_onnet"))
    pstn = _int_or_none(values.get("ds_pstn"))
    set_ids = [value for value in (onnet, pstn) if value not in (None, 0)]
    sets_ok = False
    if conn is not None and set_ids:
        try:
            found = {
                int(row[0])
                for row in conn.execute(
                    "SELECT DISTINCT setid FROM dispatcher WHERE setid IN "
                    f"({', '.join('?' for _ in set_ids)})",
                    set_ids,
                )
            }
            sets_ok = found == set(set_ids)
        except sqlite3.Error:
            sets_ok = False
    set_text = " · ".join(str(value) for value in set_ids) or "none"
    items.append(
        {
            "key": "sets",
            "label": "Route sets present in the dispatcher",
            "ok": sets_ok,
            "detail": (
                f"sets {set_text}"
                if pstn not in (None, 0)
                else f"set {set_text} (no PSTN trunk)"
            ),
        }
    )

    trusted_ok = False
    trusted_detail = "no trusted row"
    if conn is not None:
        try:
            row = conn.execute(
                "SELECT src_ip FROM trusted WHERE tag = ? ORDER BY id LIMIT 1",
                (domain,),
            ).fetchone()
        except sqlite3.Error:
            row = None
        if row is not None:
            trusted_ok = True
            trusted_detail = f"{row[0]} → {domain}"
    items.append(
        {
            "key": "trusted",
            "label": "Trusted source mapped to this home",
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

    dpid = _int_or_none(values.get("dpid"))
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

    return {
        "items": items,
        "all_green": all(item["ok"] for item in items),
    }


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------
def _hidden_fields(draft: WizardDraft, step: int) -> list[tuple[str, str]]:
    visible = STEP_VISIBLE.get(step, frozenset())
    hidden: list[tuple[str, str]] = []
    for name, value in draft.as_form().items():
        if name in visible or value == "":
            continue
        hidden.append((name, value))
    return hidden


def _summary(errors: Mapping[str, list[str]], step: int) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for field in FIELD_LABELS:
        for message in errors.get(field, []):
            items.append(
                {
                    "field": field,
                    "label": FIELD_LABELS[field],
                    "message": message,
                    "step": str(step),
                }
            )
    return items


def _warning_items(
    warnings: Mapping[str, list[str]], step: int
) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for field in FIELD_LABELS:
        for message in warnings.get(field, []):
            items.append(
                {
                    "field": field,
                    "label": FIELD_LABELS[field],
                    "message": message,
                    "step": str(step),
                }
            )
    return items


def _render(
    request: Request,
    *,
    step: int,
    draft: WizardDraft,
    errors: Mapping[str, list[str]] | None = None,
    warnings: Mapping[str, list[str]] | None = None,
    status_code: int = 200,
    apply_error: str = "",
    checklist: Mapping[str, Any] | None = None,
    reload_warnings: Iterable[str] = (),
) -> HTMLResponse:
    from . import settings as settings_module

    tableset = _tableset()
    errors = errors or {}
    warnings = warnings or {}
    summary = _summary(errors, step)
    warning_items = _warning_items(warnings, step)

    plan: list[tuple[str, dict[str, Any]]] = []
    plan_error = ""
    if step == 3 and not apply_error:
        try:
            plan = plan_writes(draft, tableset)
        except ValueError as exc:
            plan_error = f"The review could not be built: {exc}"

    context = {
        "nav": "homes",
        "fragment": _wants_fragment(request),
        "step": step,
        "steps": [(number, STEP_LABELS[number]) for number in (1, 2, 3, 4)],
        "draft": draft,
        "hidden": _hidden_fields(draft, step),
        "summary": summary,
        "error_count": len(summary),
        "warnings": warning_items,
        "field_meta": _all_field_meta(errors),
        "apply_error": apply_error or plan_error,
        "checklist": checklist,
        "plan": plan,
        "row_count": len(plan),
        "wire_hint": wire_ip_hint(draft.domain),
        "tls_live": bool(settings_module.settings.tls_live),
        "tls_domain": draft.domain,
        "reload_warnings": list(reload_warnings),
    }
    return request.app.state.templates.TemplateResponse(
        request,
        "wizard.html",
        context,
        status_code=status_code,
        headers=NO_STORE,
    )


def _field_error_messages(
    errors: Mapping[str, list[str]], name: str
) -> list[str]:
    return list(errors.get(name, []))


def _field_meta(errors: Mapping[str, list[str]], name: str) -> dict[str, Any]:
    messages = _field_error_messages(errors, name)
    return {
        "errors": messages,
        "aria_invalid": bool(messages),
        "describedby": " ".join(
            [f"help-{name}", *[f"err-{name}-{i}" for i in range(1, len(messages) + 1)]]
        ),
    }


# Exposed for the template without making it call Python functions.
def _all_field_meta(
    errors: Mapping[str, list[str]],
) -> dict[str, dict[str, Any]]:
    return {
        name: _field_meta(errors, name)
        for name in (
            "domain",
            "landline",
            "ring_group",
            "label",
            "lan_ip",
            "wire_ip",
        )
    }


# ---------------------------------------------------------------------------
# insert
# ---------------------------------------------------------------------------
def _checked_identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError(f"unsafe SQL identifier: {value!r}")
    return value


def _insert_row(
    conn: sqlite3.Connection, table: str, row: Mapping[str, Any]
) -> None:
    """Insert one fully-formed row using named columns only (no auto id)."""
    if not row:
        raise ValueError(f"{table}: nothing to insert")
    columns = list(row)
    column_sql = ", ".join(f'"{_checked_identifier(column)}"' for column in columns)
    mark_sql = ", ".join("?" for _ in columns)
    conn.execute(
        f'INSERT INTO "{_checked_identifier(table)}" ({column_sql}) '
        f"VALUES ({mark_sql})",
        [row[column] for column in columns],
    )


def _apply_rows(conn: sqlite3.Connection, writes: Iterable[tuple[str, dict]]) -> None:
    for table, row in writes:
        _insert_row(conn, table, row)


def _assert_apply_conflicts(
    conn: sqlite3.Connection,
    draft: WizardDraft,
    writes: Iterable[tuple[str, dict]],
    tableset: descriptors.TableSet,
) -> None:
    """Re-check identity/prefix uniqueness inside the write transaction.

    Step validation happens before the write lock; two concurrent applies can
    otherwise pick the same tenantid/set/landline.  Re-run the DB rules here
    and refuse the batch (the transaction rolls back).
    """
    # A parked snapshot or unfinished lifecycle intent owns this domain; do
    # not overwrite it by onboarding a second home with the same name.
    from pathlib import Path

    from . import settings as settings_module
    from .sidecar import Sidecar

    sidecar_path = (
        Path(settings_module.settings.sidecar_dir) / "console.sqlite"
    )
    if sidecar_path.is_file():
        try:
            sc = Sidecar(sidecar_path)
            pending = sc.latest_intent_any(draft.domain, include_done=False)
            parked = sc.get_parked(draft.domain)
        except Exception as exc:
            raise ValidationError(
                summary=f"Cannot verify lifecycle state: {exc}"
            ) from exc
        if pending is not None:
            raise ValidationError(
                summary=f"{draft.domain} has an unfinished lifecycle operation "
                f"({pending['op']} at {pending['state']}); resume it first."
            )
        if parked is not None:
            raise ValidationError(
                summary=f"{draft.domain} already has a parked snapshot; "
                "restore or destroy it first."
            )

    errors, _ = validate_step(1, draft, conn, tableset)
    network_errors, _ = validate_step(2, draft, conn, tableset)
    for name, messages in network_errors.items():
        errors.setdefault(name, []).extend(messages)
    if errors:
        raise ValidationError(
            summary="The home changed while you were reviewing: "
            + "; ".join(message for messages in errors.values() for message in messages)
        )
    for table, row in writes:
        if table != "dispatcher":
            continue
        setid = row.get("setid")
        taken = conn.execute(
            "SELECT 1 FROM dispatcher WHERE setid = ? LIMIT 1", (setid,)
        ).fetchone()
        if taken is not None:
            raise ValidationError(
                summary=f"Route set {setid} was taken by another home; retry."
            )


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------
@router.get("/wizard", response_class=HTMLResponse)
def wizard_start(request: Request):
    redirect = _requires_login(request)
    if redirect is not None:
        return redirect
    return _render(request, step=1, draft=WizardDraft())


@router.post("/wizard/step1", response_class=HTMLResponse)
async def wizard_step1(request: Request, conn: sqlite3.Connection = Depends(db.get_db)):
    redirect = _requires_login(request)
    if redirect is not None:
        return redirect

    tableset = _tableset()
    draft = draft_from_form(await _form_data(request))
    draft = _assign_ids(draft, conn, tableset)

    errors, warnings = validate_step(1, draft, conn, tableset)
    if errors:
        return _render(
            request,
            step=1,
            draft=draft,
            errors=errors,
            warnings=warnings,
            status_code=422,
        )
    return _render(request, step=2, draft=draft, warnings=warnings)


@router.post("/wizard/step2", response_class=HTMLResponse)
async def wizard_step2(request: Request, conn: sqlite3.Connection = Depends(db.get_db)):
    redirect = _requires_login(request)
    if redirect is not None:
        return redirect

    tableset = _tableset()
    draft = draft_from_form(await _form_data(request))
    draft = _assign_ids(draft, conn, tableset)

    identity_errors, identity_warnings = validate_step(1, draft, conn, tableset)
    if identity_errors:
        return _render(
            request,
            step=1,
            draft=draft,
            errors=identity_errors,
            warnings=identity_warnings,
            status_code=422,
        )

    errors, warnings = validate_step(2, draft, conn, tableset)
    if errors:
        return _render(
            request,
            step=2,
            draft=draft,
            errors=errors,
            warnings=warnings,
            status_code=422,
        )
    return _render(request, step=3, draft=draft, warnings=warnings)


async def _apply(request: Request, conn: sqlite3.Connection):
    redirect = _requires_login(request)
    if redirect is not None:
        return redirect

    tableset = _tableset()
    draft = draft_from_form(await _form_data(request))
    draft = _assign_ids(draft, conn, tableset)

    identity_errors, identity_warnings = validate_step(1, draft, conn, tableset)
    if identity_errors:
        return _render(
            request,
            step=1,
            draft=draft,
            errors=identity_errors,
            warnings=identity_warnings,
            status_code=422,
        )
    network_errors, network_warnings = validate_step(2, draft, conn, tableset)
    if network_errors:
        return _render(
            request,
            step=2,
            draft=draft,
            errors=network_errors,
            warnings=network_warnings,
            status_code=422,
        )

    warnings = {**identity_warnings}
    for name, messages in network_warnings.items():
        warnings.setdefault(name, []).extend(messages)

    try:
        writes = plan_writes(draft, tableset)
    except (ValueError, KeyError) as exc:
        return _render(
            request,
            step=3,
            draft=draft,
            warnings=warnings,
            apply_error=f"The apply plan is incomplete: {exc}",
            status_code=422,
        )

    sidecar_path, backup_dir = _backup_paths()
    from . import settings as settings_module

    try:
        create_backup(
            settings_module.settings.db_path,
            sidecar_path,
            backup_dir,
            f"wizard-apply:{draft.domain}",
        )
    except Exception as exc:  # noqa: BLE001 — surface every backup failure
        return _render(
            request,
            step=3,
            draft=draft,
            warnings=warnings,
            apply_error=(
                "The pre-apply backup could not be taken, so nothing was "
                f"written: {exc}"
            ),
            status_code=422,
        )

    try:
        with db.write_transaction(conn):
            _assert_apply_conflicts(conn, draft, writes, tableset)
            _apply_rows(conn, writes)
    except Exception as exc:  # noqa: BLE001 — any failure rolls back
        return _render(
            request,
            step=3,
            draft=draft,
            warnings=warnings,
            apply_error=(
                "Apply failed and the transaction was rolled back — no rows "
                f"were written. Database error: {exc}"
            ),
            status_code=422,
        )

    reload_warnings: list[str] = []
    try:
        from starlette.concurrency import run_in_threadpool

        # flush() is blocking (throttle + RPC); keep the event loop free so
        # /health and other requests are not stalled behind a 5–30 s apply.
        _keys, _warnings = await run_in_threadpool(
            _enqueue_reloads, request, tableset
        )
        reload_warnings.extend(_warnings)
    except Exception as exc:  # noqa: BLE001 — the rows are already committed
        reload_warnings.append(
            f"The home was saved, but the four reloads could not be "
            f"scheduled: {exc}"
        )

    if _wants_fragment(request):
        return _render(
            request,
            step=4,
            draft=draft,
            checklist=verify_checklist(conn, draft.domain),
            reload_warnings=reload_warnings,
        )
    target = f"/wizard/verify/{quote(draft.domain, safe='')}"
    if reload_warnings:
        from urllib.parse import quote as _quote

        target += "?warn=" + _quote("; ".join(reload_warnings))
    return RedirectResponse(target, status_code=303, headers=NO_STORE)


@router.post("/wizard/apply", response_class=HTMLResponse)
async def wizard_apply(request: Request, conn: sqlite3.Connection = Depends(db.get_db)):
    return await _apply(request, conn)


@router.post("/wizard/step3", response_class=HTMLResponse)
async def wizard_step3(request: Request, conn: sqlite3.Connection = Depends(db.get_db)):
    """The review step's submit is the apply action (step-number alias)."""
    return await _apply(request, conn)


@router.get("/wizard/verify/{domain}", response_class=HTMLResponse)
def wizard_verify(
    request: Request,
    domain: str,
    conn: sqlite3.Connection | None = Depends(db.get_read_db),
):
    redirect = _requires_login(request)
    if redirect is not None:
        return redirect
    warn = request.query_params.get("warn", "")
    return _render(
        request,
        step=4,
        draft=WizardDraft(domain=domain),
        checklist=verify_checklist(conn, domain),
        reload_warnings=[warn] if warn else [],
    )
