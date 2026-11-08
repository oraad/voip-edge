"""tables.yaml loader + row codecs + startup DB consistency check (plan §5.1).

The grammar is deliberately closed: a typo (``valdator``) must fail loudly at
startup instead of silently dropping a rule, so every mapping is checked
against its allowed key set.  Validators are *names only* here; T4 supplies the
implementations.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Any, Mapping

import yaml

from .pack import pack_fields, pack_flags, parse_flags, unpack_fields

DEFAULT_PATH = "console/tables.yaml"

ALLOWED_TYPES = frozenset(
    {"str", "int", "enum01", "ipv4", "sip_uri", "regex", "digits", "domain", "label"}
)
ALLOWED_WIDGETS = frozenset({"text", "number", "checkbox", "select", "textarea"})
ALLOWED_SEVERITIES = frozenset({"error", "warn"})
ALLOWED_STORAGE = frozenset({"packed", "columns"})
ALLOWED_DELETE_POLICIES = frozenset({"blocked_if_refs", "lifecycle"})
ALLOWED_ON_MISSING = frozenset({"error", "warn"})

# Plan §5.1: modules verify these bookkeeping rows at startup.
EXPECTED_TABLE_VERSIONS = {"dispatcher": 4, "dialplan": 2, "trusted": 6}

# The auto primary key on the three standard (columns-storage) tables.  It is
# not a descriptor field: the UI never edits it, but it is expected in the DB.
AUTO_PK = "id"

_TABLESET_KEYS = frozenset({"version", "tables"})
_DESCRIPTOR_KEYS = frozenset(
    {
        "table",
        "label",
        "storage",
        "key",
        "fields",
        "reload",
        "delete_policy",
        "cross_refs",
    }
)
_FIELD_KEYS = frozenset(
    {
        "name",
        "label",
        "column",
        "pack_position",
        "type",
        "validator",
        "severity",
        "help",
        "widget",
        "readonly",
        "locked",
        "derived",
        "default",
        "options_query",
        "show_in_list",
        "form_order",
    }
)
_RELOAD_KEYS = frozenset({"rpc", "params", "throttle_seconds"})
_CROSS_REF_KEYS = frozenset({"field", "ref", "on_missing"})


@dataclass(frozen=True)
class Field:
    name: str
    label: str = ""
    column: str | None = None
    pack_position: int | None = None
    type: str = "str"
    validator: str | None = None
    severity: str = "error"
    help: str = ""
    widget: str = "text"
    readonly: bool = False
    locked: bool = False
    derived: str | None = None
    #: Fixed create-form value for a field the operator normally does not edit
    #: (e.g. dialplan's locked identity rewrite).  Applied only on create.
    default: Any = None
    options_query: str | None = None
    show_in_list: bool = False
    form_order: int = 0


@dataclass(frozen=True)
class Reload:
    rpc: str
    params: list[Any] = dataclass_field(default_factory=list)
    throttle_seconds: int = 5


@dataclass(frozen=True)
class CrossRef:
    field: str
    ref: str
    on_missing: str = "error"


@dataclass(frozen=True)
class Descriptor:
    table: str
    storage: str
    key: str | None
    fields: list[Field]
    reload: Reload
    label: str = ""
    delete_policy: str = "blocked_if_refs"
    cross_refs: list[CrossRef] = dataclass_field(default_factory=list)

    def field(self, name: str) -> Field:
        for candidate in self.fields:
            if candidate.name == name:
                return candidate
        raise KeyError(f"{self.table} has no field {name!r}")

    def field_by_column(self, column: str) -> Field:
        for candidate in self.fields:
            if candidate.column == column:
                return candidate
        raise KeyError(f"{self.table} has no column {column!r}")

    @property
    def packed_fields(self) -> list[Field]:
        return sorted(
            (f for f in self.fields if f.pack_position is not None),
            key=lambda f: f.pack_position,  # type: ignore[arg-type,return-value]
        )


@dataclass(frozen=True)
class TableSet:
    version: int
    tables: dict[str, Descriptor]

    @property
    def table_names(self) -> list[str]:
        return list(self.tables)


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------
def _unknown_keys(
    mapping: Mapping[str, Any], allowed: frozenset[str], where: str, problems: list[str]
) -> None:
    for key in mapping:
        if key not in allowed:
            problems.append(f"{where}: unknown key {key!r}")


def _collect_unknown_keys(raw: Any, problems: list[str]) -> None:
    """Collect every unknown key in one pass so the error names them all."""
    if not isinstance(raw, Mapping):
        problems.append("tables.yaml: top level must be a mapping")
        return
    _unknown_keys(raw, _TABLESET_KEYS, "tables.yaml", problems)

    tables = raw.get("tables")
    if not isinstance(tables, Mapping):
        problems.append("tables.yaml: 'tables' must be a mapping")
        return

    for table_name, raw_desc in tables.items():
        where = f"tables.{table_name}"
        if not isinstance(raw_desc, Mapping):
            problems.append(f"{where}: descriptor must be a mapping")
            continue
        _unknown_keys(raw_desc, _DESCRIPTOR_KEYS, where, problems)

        raw_fields = raw_desc.get("fields")
        if isinstance(raw_fields, list):
            for index, raw_field in enumerate(raw_fields):
                field_where = f"{where}.fields[{index}]"
                if not isinstance(raw_field, Mapping):
                    problems.append(f"{field_where}: field must be a mapping")
                    continue
                _unknown_keys(raw_field, _FIELD_KEYS, field_where, problems)

        raw_reload = raw_desc.get("reload")
        if isinstance(raw_reload, Mapping):
            _unknown_keys(raw_reload, _RELOAD_KEYS, f"{where}.reload", problems)

        raw_refs = raw_desc.get("cross_refs")
        if isinstance(raw_refs, list):
            for index, raw_ref in enumerate(raw_refs):
                ref_where = f"{where}.cross_refs[{index}]"
                if not isinstance(raw_ref, Mapping):
                    problems.append(f"{ref_where}: cross_ref must be a mapping")
                    continue
                _unknown_keys(raw_ref, _CROSS_REF_KEYS, ref_where, problems)


def _require_text(raw: Mapping[str, Any], key: str, where: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{where}: {key!r} must be a non-empty string")
    return value


def _optional_text(raw: Mapping[str, Any], key: str, where: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{where}: {key!r} must be a string or null")
    return value


def _boolean(raw: Mapping[str, Any], key: str, where: str) -> bool:
    value = raw.get(key, False)
    if not isinstance(value, bool):
        raise ValueError(f"{where}: {key!r} must be a boolean")
    return value


def _integer(raw: Mapping[str, Any], key: str, where: str, default: int) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{where}: {key!r} must be an integer")
    return value


def _parse_field(raw_field: Any, where: str) -> Field:
    if not isinstance(raw_field, Mapping):
        raise ValueError(f"{where}: field must be a mapping")

    name = _require_text(raw_field, "name", where)
    where = f"{where} ({name})"

    column = _optional_text(raw_field, "column", where)
    pack_position = raw_field.get("pack_position")
    if pack_position is not None and (
        isinstance(pack_position, bool) or not isinstance(pack_position, int)
    ):
        raise ValueError(f"{where}: 'pack_position' must be an integer or null")

    field_type = raw_field.get("type", "str")
    if field_type not in ALLOWED_TYPES:
        raise ValueError(
            f"{where}: type {field_type!r} not in {sorted(ALLOWED_TYPES)}"
        )

    validator = _optional_text(raw_field, "validator", where)
    severity = raw_field.get("severity", "error")
    if severity not in ALLOWED_SEVERITIES:
        raise ValueError(f"{where}: severity must be one of {sorted(ALLOWED_SEVERITIES)}")

    help_text = raw_field.get("help", "")
    if not isinstance(help_text, str):
        raise ValueError(f"{where}: 'help' must be a string")

    widget = raw_field.get("widget", "text")
    if widget not in ALLOWED_WIDGETS:
        raise ValueError(f"{where}: widget must be one of {sorted(ALLOWED_WIDGETS)}")

    options_query = _optional_text(raw_field, "options_query", where)
    if options_query is not None and "." not in options_query:
        raise ValueError(f"{where}: options_query must be 'table.field'")

    derived = _optional_text(raw_field, "derived", where)
    default = raw_field.get("default")

    return Field(
        name=name,
        label=_optional_text(raw_field, "label", where) or "",
        column=column,
        pack_position=pack_position,
        type=field_type,
        validator=validator,
        severity=severity,
        help=help_text,
        widget=widget,
        readonly=_boolean(raw_field, "readonly", where),
        locked=_boolean(raw_field, "locked", where),
        derived=derived,
        default=default,
        options_query=options_query,
        show_in_list=_boolean(raw_field, "show_in_list", where),
        form_order=_integer(raw_field, "form_order", where, 0),
    )


def _parse_reload(raw_reload: Any, where: str) -> Reload:
    if not isinstance(raw_reload, Mapping):
        raise ValueError(f"{where}: 'reload' must be a mapping")
    rpc = _require_text(raw_reload, "rpc", f"{where}.reload")
    params = raw_reload.get("params", [])
    if not isinstance(params, list):
        raise ValueError(f"{where}.reload: 'params' must be a list")
    throttle = raw_reload.get("throttle_seconds", 5)
    if isinstance(throttle, bool) or not isinstance(throttle, int) or throttle < 0:
        raise ValueError(
            f"{where}.reload: 'throttle_seconds' must be a non-negative integer"
        )
    return Reload(rpc=rpc, params=list(params), throttle_seconds=throttle)


def _parse_cross_refs(raw_refs: Any, where: str) -> list[CrossRef]:
    if raw_refs is None:
        return []
    if not isinstance(raw_refs, list):
        raise ValueError(f"{where}: 'cross_refs' must be a list")
    refs: list[CrossRef] = []
    for index, raw_ref in enumerate(raw_refs):
        ref_where = f"{where}.cross_refs[{index}]"
        if not isinstance(raw_ref, Mapping):
            raise ValueError(f"{ref_where}: cross_ref must be a mapping")
        field_name = _require_text(raw_ref, "field", ref_where)
        ref = _require_text(raw_ref, "ref", ref_where)
        if ref.count(".") != 1 or not all(ref.split(".")):
            raise ValueError(f"{ref_where}: 'ref' must be 'table.field'")
        on_missing = raw_ref.get("on_missing", "error")
        if on_missing not in ALLOWED_ON_MISSING:
            raise ValueError(
                f"{ref_where}: on_missing must be one of {sorted(ALLOWED_ON_MISSING)}"
            )
        refs.append(CrossRef(field=field_name, ref=ref, on_missing=on_missing))
    return refs


def _parse_descriptor(name: str, raw_desc: Any) -> Descriptor:
    where = f"tables.{name}"
    if not isinstance(raw_desc, Mapping):
        raise ValueError(f"{where}: descriptor must be a mapping")

    table = _require_text(raw_desc, "table", where)
    if table != name:
        raise ValueError(f"{where}: 'table' must equal the descriptor key {name!r}")
    if not table.replace("_", "").isalnum() or not table[0].isalpha():
        raise ValueError(f"{where}: unsafe table name {table!r}")

    storage = raw_desc.get("storage")
    if storage not in ALLOWED_STORAGE:
        raise ValueError(f"{where}: storage must be one of {sorted(ALLOWED_STORAGE)}")

    raw_fields = raw_desc.get("fields")
    if not isinstance(raw_fields, list) or not raw_fields:
        raise ValueError(f"{where}: 'fields' must be a non-empty list")
    fields = [
        _parse_field(raw_field, f"{where}.fields[{index}]")
        for index, raw_field in enumerate(raw_fields)
    ]

    if len({f.name for f in fields}) != len(fields):
        raise ValueError(f"{where}: duplicate field names")

    delete_policy = raw_desc.get("delete_policy", "blocked_if_refs")
    if delete_policy not in ALLOWED_DELETE_POLICIES:
        raise ValueError(
            f"{where}: delete_policy must be one of {sorted(ALLOWED_DELETE_POLICIES)}"
        )
    if "reload" not in raw_desc:
        raise ValueError(f"{where}: missing required key 'reload'")
    reload_spec = _parse_reload(raw_desc["reload"], where)
    cross_refs = _parse_cross_refs(raw_desc.get("cross_refs"), where)

    key = raw_desc.get("key")
    if key is not None and not isinstance(key, str):
        raise ValueError(f"{where}: 'key' must be a string or null")

    if storage == "packed":
        if not key:
            raise ValueError(f"{where}: packed storage requires a 'key' column")
        key_fields = [
            f for f in fields if f.column == key and f.pack_position is None
        ]
        if len(key_fields) != 1:
            raise ValueError(
                f"{where}: packed key {key!r} must be declared exactly once "
                "with 'column' and without 'pack_position'"
            )
        for candidate in fields:
            if candidate is key_fields[0]:
                continue
            if candidate.column is not None:
                raise ValueError(
                    f"{where}: packed field {candidate.name!r} may not declare "
                    "'column' (use pack_position)"
                )
            if candidate.pack_position is None:
                raise ValueError(
                    f"{where}: packed field {candidate.name!r} needs a pack_position"
                )
        positions = sorted(f.pack_position for f in fields if f.pack_position is not None)
        if positions != list(range(len(positions))):
            raise ValueError(
                f"{where}: pack_position values must be contiguous from 0, got {positions}"
            )
    else:  # columns
        if key:
            raise ValueError(f"{where}: columns storage must not declare a 'key'")
        for candidate in fields:
            if candidate.pack_position is not None:
                raise ValueError(
                    f"{where}: columns field {candidate.name!r} may not use pack_position"
                )
            if candidate.column is None:
                raise ValueError(
                    f"{where}: columns field {candidate.name!r} needs a 'column'"
                )
        if len({f.column for f in fields}) != len(fields):
            raise ValueError(f"{where}: duplicate column names")

    field_names = {f.name for f in fields}
    for cross_ref in cross_refs:
        if cross_ref.field not in field_names:
            raise ValueError(
                f"{where}: cross_ref names unknown field {cross_ref.field!r}"
            )

    return Descriptor(
        table=table,
        label=_optional_text(raw_desc, "label", where) or "",
        storage=storage,
        key=key,
        fields=fields,
        reload=reload_spec,
        delete_policy=delete_policy,
        cross_refs=cross_refs,
    )


def load(path: str | Path = DEFAULT_PATH) -> TableSet:
    """Load and strictly validate tables.yaml.  Unknown keys raise ValueError."""
    source = Path(path)
    if not source.is_absolute() and not source.exists():
        # Default path is repo-relative; keep it working from any CWD.
        fallback = Path(__file__).resolve().parent.parent / source
        if fallback.exists():
            source = fallback

    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    if raw is None:
        raw = {}

    problems: list[str] = []
    _collect_unknown_keys(raw, problems)
    if problems:
        raise ValueError("tables.yaml: " + "; ".join(problems))

    assert isinstance(raw, Mapping)  # _collect_unknown_keys proved it
    if "version" not in raw:
        raise ValueError("tables.yaml: missing required key 'version'")
    version = raw["version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise ValueError(f"tables.yaml: unsupported version {version!r} (expected 1)")
    if "tables" not in raw or not isinstance(raw["tables"], Mapping):
        raise ValueError("tables.yaml: 'tables' must be a mapping")

    tables: dict[str, Descriptor] = {}
    for name, raw_desc in raw["tables"].items():
        if not isinstance(name, str):
            raise ValueError(f"tables.yaml: table key {name!r} must be a string")
        tables[name] = _parse_descriptor(name, raw_desc)

    return TableSet(version=version, tables=tables)


# ---------------------------------------------------------------------------
# row codecs
# ---------------------------------------------------------------------------
def _key_field(descriptor: Descriptor) -> Field:
    if not descriptor.key:
        raise ValueError(f"{descriptor.table}: no packed key declared")
    for candidate in descriptor.fields:
        if candidate.column == descriptor.key and candidate.pack_position is None:
            return candidate
    raise ValueError(f"{descriptor.table}: key column {descriptor.key!r} not declared")


def unpack_row(descriptor: Descriptor, row: Mapping[str, Any]) -> dict[str, Any]:
    """DB row -> descriptor field names.

    ``packed`` rows (``{key, tvalue}``) are split by pack_position; ``columns``
    rows are projected onto the declared fields, so the auto ``id`` PK and any
    future helper columns are ignored.
    """
    if descriptor.storage == "packed":
        key_field = _key_field(descriptor)
        if descriptor.key not in row:
            raise ValueError(
                f"{descriptor.table}: row has no key column {descriptor.key!r}"
            )
        packed_fields = descriptor.packed_fields
        values = unpack_fields(row.get("tvalue", ""), expected=len(packed_fields))
        unpacked: dict[str, Any] = {key_field.name: row[descriptor.key]}
        for field, value in zip(packed_fields, values):
            unpacked[field.name] = value
        return unpacked

    return {
        field.name: row[field.column]
        for field in descriptor.fields
        if field.column in row
    }


def _pack_flags_value(value: Any) -> str:
    """Flags are the one packed field that contains '+' by design."""
    if isinstance(value, (tuple, list)):
        if len(value) not in (2, 3):
            raise ValueError(
                "flags tuple must be (ih_on, pstn_on[, block_dpid])"
            )
        return pack_flags(
            bool(value[0]),
            bool(value[1]),
            value[2] if len(value) == 3 else None,
        )
    text = "" if value is None else str(value).strip()
    parse_flags(text)  # validate now, before the row reaches the database
    return text


def pack_row(descriptor: Descriptor, values: Mapping[str, Any]) -> dict[str, Any]:
    """Descriptor field names -> DB columns ready for INSERT/UPDATE."""
    if descriptor.storage == "packed":
        key_field = _key_field(descriptor)
        if key_field.name not in values:
            raise ValueError(
                f"{descriptor.table}: missing key field {key_field.name!r}"
            )
        try:
            # pack_fields also enforces the separator rule on the key.
            key_value = pack_fields([values[key_field.name]])
        except ValueError as exc:
            raise ValueError(
                f"{descriptor.table}.{key_field.name}: {exc}"
            ) from exc

        parts: list[str] = []
        for field in descriptor.packed_fields:
            if field.name not in values:
                raise ValueError(
                    f"{descriptor.table}: missing packed field {field.name!r}"
                )
            try:
                if field.name == "flags":
                    # The one packed field whose own separator is '+'.
                    parts.append(_pack_flags_value(values[field.name]))
                else:
                    parts.append(pack_fields([values[field.name]]))
            except ValueError as exc:
                raise ValueError(
                    f"{descriptor.table}.{field.name}: {exc}"
                ) from exc
        return {descriptor.key: key_value, "tvalue": ",".join(parts)}

    packed: dict[str, Any] = {}
    for field in descriptor.fields:
        if field.name in values:
            assert field.column is not None
            packed[field.column] = values[field.name]
    return packed


# ---------------------------------------------------------------------------
# live-schema consistency check
# ---------------------------------------------------------------------------
def _expected_columns(descriptor: Descriptor) -> set[str]:
    if descriptor.storage == "packed":
        assert descriptor.key is not None
        return {descriptor.key, "tvalue"}
    return {field.column for field in descriptor.fields if field.column} | {AUTO_PK}


def validate_against_db(
    tableset: TableSet, conn: sqlite3.Connection
) -> list[str]:
    """Compare descriptors with the live sqlite schema and version rows.

    Returns human-readable problems; an empty list means the console may write.
    """
    problems: list[str] = []

    version_rows: dict[str, int] | None
    try:
        version_rows = {
            str(name): int(value)
            for name, value in conn.execute(
                "SELECT table_name, table_version FROM version"
            )
        }
    except sqlite3.Error as exc:
        problems.append(f"version: bookkeeping table missing or unreadable ({exc})")
        version_rows = None

    for descriptor in tableset.tables.values():
        table = descriptor.table

        expected_version = EXPECTED_TABLE_VERSIONS.get(table)
        if version_rows is not None and expected_version is not None:
            actual_version = version_rows.get(table)
            if actual_version is None:
                problems.append(
                    f"{table}: no version row (expected {expected_version})"
                )
            elif actual_version != expected_version:
                problems.append(
                    f"{table}: version {actual_version} != expected {expected_version}"
                )

        try:
            info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        except sqlite3.Error as exc:
            problems.append(f"{table}: PRAGMA table_info failed ({exc})")
            continue

        actual_columns = {str(row[1]) for row in info}
        if not actual_columns:
            problems.append(f"{table}: table not found in database")
            continue

        expected_columns = _expected_columns(descriptor)
        missing = sorted(expected_columns - actual_columns)
        unexpected = sorted(actual_columns - expected_columns)
        if missing:
            problems.append(f"{table}: missing column(s): {', '.join(missing)}")
        if unexpected:
            problems.append(f"{table}: unexpected column(s): {', '.join(unexpected)}")

    return problems
