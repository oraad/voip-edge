"""tests for the closed tables.yaml grammar (plan §5.1) and row codecs (task T0)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import yaml

from console.descriptors import (
    Descriptor,
    Field,
    TableSet,
    load,
    pack_row,
    unpack_row,
    validate_against_db,
)
from console.pack import parse_flags

REPO = Path(__file__).resolve().parents[2]
TABLES_YAML = REPO / "console" / "tables.yaml"

DOMAIN = "sip.alpha.home.example.net"
GOLDEN_TVALUE = "T1,11,31,01111111,01111111,10,1+1+90"

GOLDEN_VALUES = {
    "tenantid": "T1",
    "ds_onnet": "11",
    "ds_pstn": "31",
    "cli": "01111111",
    "label": "01111111",
    "dpid": "10",
    "flags": "1+1+90",
}


# ---------------------------------------------------------------------------
# loading + schema
# ---------------------------------------------------------------------------
def test_load_default_path_has_five_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(REPO)
    tableset = load()
    assert isinstance(tableset, TableSet)
    assert tableset.version == 1
    assert set(tableset.tables) == {
        "tenant_registry",
        "tenant_landline",
        "dispatcher",
        "dialplan",
        "trusted",
    }


def test_storage_keys_and_reloads() -> None:
    tableset = load(TABLES_YAML)
    expected = {
        "tenant_registry": ("packed", "tprefix", "mtree.reload", ["tenantdom"]),
        "tenant_landline": ("packed", "tprefix", "mtree.reload", ["landline"]),
        "dispatcher": ("columns", None, "dispatcher.reload", []),
        "dialplan": ("columns", None, "dialplan.reload", []),
        "trusted": ("columns", None, "permissions.trustedReload", []),
    }
    for name, (storage, key, rpc, params) in expected.items():
        desc = tableset.tables[name]
        assert isinstance(desc, Descriptor)
        assert desc.table == name
        assert desc.storage == storage
        assert desc.key == key
        assert desc.reload.rpc == rpc
        assert desc.reload.params == params
        assert desc.reload.throttle_seconds == 5
        assert desc.delete_policy in {"blocked_if_refs", "lifecycle"}


def test_tenant_registry_has_seven_packed_positions_including_flags() -> None:
    desc = load(TABLES_YAML).tables["tenant_registry"]
    packed = {f.pack_position: f for f in desc.fields if f.pack_position is not None}
    assert sorted(packed) == list(range(7))
    assert [packed[i].name for i in range(7)] == [
        "tenantid",
        "ds_onnet",
        "ds_pstn",
        "cli",
        "label",
        "dpid",
        "flags",
    ]
    assert packed[6].validator == "flags_valid"
    assert desc.fields[0].name == "tprefix"
    assert desc.fields[0].column == "tprefix"


def test_tenant_landline_has_four_packed_positions() -> None:
    desc = load(TABLES_YAML).tables["tenant_landline"]
    packed = sorted(
        (f for f in desc.fields if f.pack_position is not None),
        key=lambda f: f.pack_position,
    )
    assert [f.name for f in packed] == ["tenantid", "target", "ds_onnet", "ih_on"]
    assert packed[2].readonly is True
    assert packed[2].derived == "tenant_ds_onnet"


def test_all_fields_use_the_closed_type_and_widget_sets() -> None:
    tableset = load(TABLES_YAML)
    allowed_types = {
        "str",
        "int",
        "enum01",
        "ipv4",
        "sip_uri",
        "regex",
        "digits",
        "domain",
        "label",
    }
    allowed_widgets = {"text", "number", "checkbox", "select", "textarea"}
    for desc in tableset.tables.values():
        names = [f.name for f in desc.fields]
        assert len(names) == len(set(names)), f"duplicate field in {desc.table}"
        for field in desc.fields:
            assert field.type in allowed_types, (desc.table, field.name, field.type)
            assert field.widget in allowed_widgets, (desc.table, field.name, field.widget)
            assert field.severity in {"error", "warn"}
            if field.validator is not None:
                assert isinstance(field.validator, str)


def test_unknown_keys_are_rejected_at_top_level_and_field_level(
    tmp_path: Path,
) -> None:
    raw = yaml.safe_load(TABLES_YAML.read_text())
    raw["bogus_top"] = True
    raw["tables"]["tenant_registry"]["fields"][1]["bogus_field_key"] = "x"
    bad = tmp_path / "tables.yaml"
    bad.write_text(yaml.safe_dump(raw, sort_keys=False))
    with pytest.raises(ValueError) as excinfo:
        load(bad)
    message = str(excinfo.value)
    assert "bogus_top" in message
    assert "bogus_field_key" in message


def test_unknown_descriptor_key_is_rejected(tmp_path: Path) -> None:
    raw = yaml.safe_load(TABLES_YAML.read_text())
    raw["tables"]["dispatcher"]["script"] = "not allowed"
    bad = tmp_path / "tables.yaml"
    bad.write_text(yaml.safe_dump(raw, sort_keys=False))
    with pytest.raises(ValueError, match="script"):
        load(bad)


# ---------------------------------------------------------------------------
# row codecs
# ---------------------------------------------------------------------------
def test_unpack_packed_row() -> None:
    desc = load(TABLES_YAML).tables["tenant_registry"]
    row = {"tprefix": DOMAIN, "tvalue": GOLDEN_TVALUE}
    unpacked = unpack_row(desc, row)
    assert unpacked == {"tprefix": DOMAIN, **GOLDEN_VALUES}
    assert parse_flags(unpacked["flags"]) == (True, True, 90)


def test_pack_packed_row_is_the_literal_golden_vector() -> None:
    desc = load(TABLES_YAML).tables["tenant_registry"]
    assert pack_row(desc, {"tprefix": DOMAIN, **GOLDEN_VALUES}) == {
        "tprefix": DOMAIN,
        "tvalue": GOLDEN_TVALUE,
    }


def test_packed_row_absent_block_round_trip() -> None:
    desc = load(TABLES_YAML).tables["tenant_registry"]
    values = {**GOLDEN_VALUES, "flags": "1+1"}
    row = pack_row(desc, {"tprefix": DOMAIN, **values})
    assert row["tvalue"] == "T1,11,31,01111111,01111111,10,1+1"
    unpacked = unpack_row(desc, row)
    assert unpacked["flags"] == "1+1"
    assert parse_flags(unpacked["flags"]) == (True, True, None)


@pytest.mark.parametrize(
    ("field", "value"),
    [("label", "Doe, John"), ("cli", "1+1")],
)
def test_pack_packed_row_rejects_separators(field: str, value: str) -> None:
    desc = load(TABLES_YAML).tables["tenant_registry"]
    values = {**GOLDEN_VALUES, field: value}
    with pytest.raises(ValueError):
        pack_row(desc, {"tprefix": DOMAIN, **values})


def test_unpack_and_pack_columns_row_excludes_auto_id() -> None:
    desc = load(TABLES_YAML).tables["dispatcher"]
    row = {
        "id": 7,
        "setid": 11,
        "destination": "sip:10.0.0.10:5060;transport=tcp",
        "flags": 0,
        "priority": 0,
        "attrs": "",
        "description": "alpha voip-stack",
    }
    unpacked = unpack_row(desc, row)
    assert "id" not in unpacked
    assert unpacked["setid"] == 11
    assert unpacked["destination"] == row["destination"]
    packed = pack_row(desc, unpacked)
    assert packed == {k: v for k, v in row.items() if k != "id"}


def test_columns_pack_row_does_not_invent_an_id() -> None:
    desc = load(TABLES_YAML).tables["trusted"]
    packed = pack_row(
        desc,
        {
            "src_ip": "10.88.0.5",
            "proto": "any",
            "from_pattern": None,
            "ruri_pattern": None,
            "tag": DOMAIN,
            "priority": 0,
        },
    )
    assert "id" not in packed
    assert packed["src_ip"] == "10.88.0.5"


# ---------------------------------------------------------------------------
# live-schema consistency check
# ---------------------------------------------------------------------------
REAL_DDL = """
CREATE TABLE tenant_registry (tprefix CHAR PRIMARY KEY, tvalue CHAR NOT NULL);
CREATE TABLE tenant_landline (tprefix CHAR PRIMARY KEY, tvalue CHAR NOT NULL);
CREATE TABLE dispatcher (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    setid INTEGER NOT NULL DEFAULT 0,
    destination CHAR NOT NULL DEFAULT '',
    flags INTEGER NOT NULL DEFAULT 0,
    priority INTEGER NOT NULL DEFAULT 0,
    attrs CHAR NOT NULL DEFAULT '',
    description CHAR NOT NULL DEFAULT ''
);
CREATE TABLE dialplan (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dpid INTEGER NOT NULL DEFAULT 0,
    pr INTEGER NOT NULL DEFAULT 1,
    match_op INTEGER NOT NULL DEFAULT 1,
    match_exp TEXT NOT NULL DEFAULT '',
    match_len INTEGER NOT NULL DEFAULT 0,
    subst_exp TEXT NOT NULL DEFAULT '^(.*)$',
    repl_exp TEXT NOT NULL DEFAULT '\\1',
    attrs TEXT NOT NULL DEFAULT ''
);
CREATE TABLE trusted (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    src_ip CHAR NOT NULL,
    proto CHAR NOT NULL DEFAULT 'any',
    from_pattern CHAR,
    ruri_pattern CHAR,
    tag CHAR,
    priority INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE version (table_name TEXT PRIMARY KEY, table_version INTEGER NOT NULL);
INSERT INTO version (table_name, table_version) VALUES
    ('dispatcher', 4), ('dialplan', 2), ('trusted', 6);
"""


def _db(tmp_path: Path, ddl: str = REAL_DDL) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "edge.sqlite")
    conn.executescript(ddl)
    return conn


def test_validate_against_db_is_clean_for_the_real_schema(tmp_path: Path) -> None:
    tableset = load(TABLES_YAML)
    conn = _db(tmp_path)
    try:
        assert validate_against_db(tableset, conn) == []
    finally:
        conn.close()


def test_validate_against_db_reports_a_dropped_column(tmp_path: Path) -> None:
    tableset = load(TABLES_YAML)
    conn = _db(tmp_path)
    conn.execute("ALTER TABLE dispatcher DROP COLUMN description")
    conn.commit()
    try:
        problems = validate_against_db(tableset, conn)
    finally:
        conn.close()
    assert len(problems) == 1
    assert "dispatcher" in problems[0]
    assert "description" in problems[0]


def test_validate_against_db_reports_a_wrong_version(tmp_path: Path) -> None:
    tableset = load(TABLES_YAML)
    conn = _db(tmp_path)
    conn.execute("UPDATE version SET table_version = 99 WHERE table_name = 'dispatcher'")
    conn.commit()
    try:
        problems = validate_against_db(tableset, conn)
    finally:
        conn.close()
    assert len(problems) == 1
    assert "dispatcher" in problems[0]
    assert "version" in problems[0].lower()
    assert "99" in problems[0]


def test_validate_against_db_reports_a_missing_table(tmp_path: Path) -> None:
    tableset = load(TABLES_YAML)
    conn = _db(tmp_path)
    conn.execute("DROP TABLE tenant_landline")
    conn.commit()
    try:
        problems = validate_against_db(tableset, conn)
    finally:
        conn.close()
    assert any("tenant_landline" in p for p in problems)


def test_descriptor_and_field_labels_are_explicit():
    tables = load()
    registry = tables.tables["tenant_registry"]
    assert registry.label == "Homes"
    assert registry.field("tprefix").label == "Domain"
    assert registry.field("ds_onnet").label == "On-net route set"
    assert tables.tables["dispatcher"].label == "Route sets"
