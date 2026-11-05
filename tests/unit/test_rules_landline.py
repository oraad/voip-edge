"""Unit tests for the T4.4 ``tenant_landline`` rule family.

The field rules use the shared validator contract
``fn(value, context) -> (errors, warnings)``.  The longest-prefix rule is a
cross-row check, so it is exposed as ``check_prefix_conflict`` and invoked by
the descriptor-driven form router for every family that declares it.
"""
from __future__ import annotations

import sqlite3

import pytest

from console import db, descriptors, validators
from console.rules import landline
from console.validators import ValidationContext
from tests.fixtures import seeddb

TABLESET = descriptors.load()
LANDLINE = TABLESET.tables["tenant_landline"]

EXISTING_SHORT = "0111"
EXISTING_FULL = "01111111"
OTHER_FULL = "02222222"


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(seeddb.seed(tmp_path / "edge.sqlite"))
    try:
        yield connection
    finally:
        connection.close()


def context(
    conn,
    values: dict | None = None,
    *,
    mode: str = "create",
    key: str | None = None,
) -> ValidationContext:
    return ValidationContext(
        descriptor=LANDLINE,
        values=values or {},
        conn=conn,
        mode=mode,
        key=key,
    )


def run(name: str, value, ctx: ValidationContext):
    return validators.validate(name, value, ctx)


# ---------------------------------------------------------------------------
# registry wiring
# ---------------------------------------------------------------------------
def test_family_names_are_registered_and_no_longer_pending():
    expected = {"landline_digits", "tenant_exists", "target_identifier"}
    assert set(landline.RULES) == expected
    assert landline.PENDING == frozenset(expected)
    for name in expected:
        assert name in validators.REGISTRY
        assert name not in validators.PENDING_VALIDATORS
    assert landline.check_prefix_conflict in landline.FORM_CHECKS
    assert landline.landline_prefix_conflict is landline.check_prefix_conflict


# ---------------------------------------------------------------------------
# landline_digits (V-LAND-DIGITS-MIN3)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value", ["100", "01111111", "000"])
def test_landline_digits_accepts_three_or_more_digits(value, conn):
    assert run("landline_digits", value, context(conn)) == ([], [])


@pytest.mark.parametrize("value", ["12", "1"])
def test_landline_digits_rejects_shorter_than_three(value, conn):
    errors, warnings = run("landline_digits", value, context(conn))
    assert errors and "at least 3" in errors[0]
    assert warnings == []


@pytest.mark.parametrize("value", ["26a", "26 41", "+0111", "26.41", "26-41"])
def test_landline_digits_rejects_non_digits(value, conn):
    errors, warnings = run("landline_digits", value, context(conn))
    assert errors and "digits only" in errors[0]
    assert warnings == []


def test_landline_digits_empty_defers_to_the_packed_required_rule(conn):
    assert run("landline_digits", "", context(conn)) == ([], [])


# ---------------------------------------------------------------------------
# tenant_exists (V-LAND-TENANT-EXISTS)
# ---------------------------------------------------------------------------
def test_tenant_exists_accepts_a_registered_home(conn):
    assert run("tenant_exists", "T1", context(conn)) == ([], [])


def test_tenant_exists_rejects_an_unknown_home(conn):
    errors, warnings = run("tenant_exists", "T9", context(conn))
    assert errors and "T9" in errors[0]
    assert "home registry" in errors[0]
    assert warnings == []


def test_tenant_exists_is_quiet_without_a_connection():
    assert run("tenant_exists", "T9", ValidationContext()) == ([], [])


# ---------------------------------------------------------------------------
# target_identifier (V-LAND-TARGET-CHARS)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value", ["home", "501", "ring-group_1", "ext.20", "a~b"])
def test_target_identifier_accepts_safe_identifiers(value, conn):
    assert run("target_identifier", value, context(conn)) == ([], [])


@pytest.mark.parametrize("value", ["a,b", "a+b", "a b", "sip:x@y", "a/b"])
def test_target_identifier_rejects_unsafe_characters(value, conn):
    errors, warnings = run("target_identifier", value, context(conn))
    assert errors
    assert warnings == []


def test_target_identifier_mentions_the_comma_explicitly(conn):
    errors, _ = run("target_identifier", "ring,main", context(conn))
    assert "comma" in errors[0].lower()


# ---------------------------------------------------------------------------
# longest-prefix matrix (V-LAND-PREFIX-CROSS-TENANT / -SAME-TENANT)
# ---------------------------------------------------------------------------
def _check(conn, values, *, mode="create", key=None):
    return landline.check_prefix_conflict(
        LANDLINE,
        values,
        conn=conn,
        mode=mode,
        existing={},
        key=key,
    )


def _submitted(number: str, tenantid: str) -> dict:
    return {"tprefix": number, "tenantid": tenantid, "target": "700", "ih_on": "1"}


def test_cross_tenant_shorter_new_prefix_is_blocked(conn):
    result = _check(conn, _submitted(EXISTING_SHORT, "T2"))
    assert result is not None
    assert result["interruption"]["same_tenant"] is False
    assert result["interruption"]["existing"]["number"] == EXISTING_FULL
    assert result["interruption"]["submitted"]["number"] == EXISTING_SHORT
    assert result["errors"]["tprefix"]


def test_cross_tenant_longer_new_number_is_blocked(conn):
    result = _check(conn, _submitted(EXISTING_FULL + "9", "T2"))
    assert result is not None
    assert result["interruption"]["same_tenant"] is False
    assert result["interruption"]["existing"]["number"] == EXISTING_FULL


def test_cross_tenant_checkbox_does_not_bypass_the_block(conn):
    values = dict(_submitted(EXISTING_SHORT, "T2"), allow_same_tenant_shadow="1")
    result = _check(conn, values)
    assert result is not None
    assert result["interruption"]["same_tenant"] is False


def test_same_tenant_shorter_prefix_needs_the_checkbox(conn):
    result = _check(conn, _submitted(EXISTING_SHORT, "T1"))
    assert result is not None
    assert result["interruption"]["same_tenant"] is True
    assert "allow_same_tenant_shadow" in result["errors"]["tprefix"][0]


def test_same_tenant_shorter_prefix_is_allowed_with_the_checkbox(conn):
    values = dict(_submitted(EXISTING_SHORT, "T1"), allow_same_tenant_shadow="1")
    assert _check(conn, values) is None


def test_same_tenant_longer_number_needs_and_accepts_the_checkbox(conn):
    longer = EXISTING_FULL + "9"
    assert _check(conn, _submitted(longer, "T1")) is not None
    values = dict(_submitted(longer, "T1"), allow_same_tenant_shadow="1")
    assert _check(conn, values) is None


def test_non_overlapping_number_is_clean(conn):
    assert _check(conn, _submitted("555000", "T1")) is None


def test_editing_a_row_does_not_conflict_with_itself(conn):
    values = _submitted(EXISTING_FULL, "T1")
    assert _check(conn, values, mode="edit", key=EXISTING_FULL) is None


def test_conflict_check_is_ignored_for_other_tables(conn):
    registry = TABLESET.tables["tenant_registry"]
    result = landline.check_prefix_conflict(
        registry,
        {"tprefix": "sip.alpha.home.example.net", "tenantid": "T1"},
        conn=conn,
        mode="create",
        existing={},
        key=None,
    )
    assert result is None
