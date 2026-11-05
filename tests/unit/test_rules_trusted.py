"""Unit tests for the trusted family rules (T4.6, plan §5.2/§7.19).

The contract is the T4.1 validator signature: ``fn(value, context) ->
(errors, warnings)``.  Errors block the save, warnings are badges, and the
duplicate check needs a live connection (the same one the form request uses).
"""
from __future__ import annotations

import sqlite3

import pytest

from console.rules import trusted
from console.validators import (
    PENDING_VALIDATORS,
    REGISTRY,
    ValidationContext,
    validate,
)
from tests.fixtures import seeddb

O_EXAMPLE = "sip.alpha.home.example.net"
A_EXAMPLE = "sip.beta.home.example.net"


@pytest.fixture()
def conn():
    connection = sqlite3.connect(":memory:")
    connection.executescript(seeddb.SCHEMA)
    connection.executemany(
        "INSERT INTO tenant_registry (tprefix, tvalue) VALUES (?, ?)",
        [
            (O_EXAMPLE, "T1,11,31,01111111,01111111,10,1+1+90"),
            (A_EXAMPLE, "T2,12,32,02222222,02222222,10,1+1+90"),
        ],
    )
    connection.executemany(
        "INSERT INTO trusted (src_ip, proto, tag, priority) VALUES (?, ?, ?, ?)",
        [
            ("100.64.10.10", "any", O_EXAMPLE, 0),
            ("100.64.10.12", "any", A_EXAMPLE, 0),
        ],
    )
    connection.commit()
    try:
        yield connection
    finally:
        connection.close()


def run(name: str, value, context: ValidationContext | None = None):
    return validate(name, value, context or ValidationContext())


def context(conn=None, *, tag: str = O_EXAMPLE, mode: str = "create", key=None, **values):
    return ValidationContext(
        values={"tag": tag, **values},
        conn=conn,
        mode=mode,
        key=key,
    )


# ---------------------------------------------------------------------------
# registry contract
# ---------------------------------------------------------------------------
def test_family_names_match_tables_yaml_and_merge_into_the_registry():
    assert set(trusted.RULES) == {
        "trusted_src_ip",
        "tag_options",
        "proto_any_locked",
        "priority_int",
    }
    assert set(trusted.RULES) <= set(REGISTRY)
    # The merged names must no longer be advertised as pending.
    assert set(trusted.RULES) & set(PENDING_VALIDATORS) == set()


# ---------------------------------------------------------------------------
# src_ip: private / CGNAT accepted
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "address",
    [
        "10.0.0.10",
        "10.0.0.1",
        "172.16.5.5",
        "172.31.255.254",
        "192.168.1.20",
        "100.64.0.1",
        "100.64.10.10",
        "100.127.255.254",
    ],
)
def test_private_and_cgnat_addresses_are_accepted(conn, address):
    errors, warnings = run("trusted_src_ip", address, context(conn))
    assert errors == []
    assert warnings == []


@pytest.mark.parametrize("address", ["8.8.8.8", "1.1.1.1", "203.0.113.7"])
def test_public_address_is_an_error_with_the_onboarding_hint(conn, address):
    errors, warnings = run("trusted_src_ip", address, context(conn))
    assert len(errors) == 1
    assert "wire-observed tunnel address" in errors[0]
    assert "onboarding step 2" in errors[0]
    assert "tick the override" in errors[0]
    assert warnings == []


@pytest.mark.parametrize("flag", ["1", "on", "true", "TRUE"])
def test_public_address_with_override_downgrades_to_a_warning(conn, flag):
    ctx = context(conn, override_public_ip=flag)
    errors, warnings = run("trusted_src_ip", "8.8.8.8", ctx)
    assert errors == []
    assert len(warnings) == 1
    assert "generic non-tunnel path" in warnings[0]
    assert "not a private/CGNAT address — continuing anyway" in warnings[0]


def test_override_values_that_are_not_truthy_do_not_downgrade(conn):
    ctx = context(conn, override_public_ip="0")
    errors, warnings = run("trusted_src_ip", "8.8.8.8", ctx)
    assert errors and "wire-observed" in errors[0]
    assert warnings == []


@pytest.mark.parametrize("address", ["100.96.128.1", "100.96.128.254"])
def test_utility_alias_shape_warns_but_saves(conn, address):
    errors, warnings = run("trusted_src_ip", address, context(conn))
    assert errors == []
    assert warnings == [
        "Use the wire-observed native IP, not the utility alias."
    ]


@pytest.mark.parametrize("address", ["", "not-an-ip", "10.0.4.999", "::1"])
def test_invalid_ipv4_uses_the_generic_ipv4_errors(conn, address):
    errors, _ = run("trusted_src_ip", address, context(conn))
    assert errors


# ---------------------------------------------------------------------------
# src_ip: one address cannot serve two tags
# ---------------------------------------------------------------------------
def test_duplicate_src_ip_across_tags_is_an_error_naming_the_existing_tag(conn):
    ctx = context(conn, tag=A_EXAMPLE)
    errors, warnings = run("trusted_src_ip", "100.64.10.10", ctx)
    assert len(errors) == 1
    assert "already trusted for" in errors[0]
    assert O_EXAMPLE in errors[0]
    assert warnings == []


def test_duplicate_src_ip_for_the_same_tag_is_allowed(conn):
    errors, warnings = run("trusted_src_ip", "100.64.10.10", context(conn))
    assert errors == []
    assert warnings == []


def test_edit_can_keep_its_own_source_address(conn):
    row_id = conn.execute(
        "SELECT id FROM trusted WHERE src_ip = ?", ("100.64.10.10",)
    ).fetchone()[0]
    ctx = context(conn, mode="edit", key=str(row_id))
    errors, warnings = run("trusted_src_ip", "100.64.10.10", ctx)
    assert errors == []
    assert warnings == []


def test_duplicate_check_is_skipped_without_a_connection():
    errors, _ = run("trusted_src_ip", "100.64.10.10", context(None, tag=A_EXAMPLE))
    assert errors == []


# ---------------------------------------------------------------------------
# tag must name an existing home
# ---------------------------------------------------------------------------
def test_existing_tag_is_accepted(conn):
    assert run("tag_options", O_EXAMPLE, context(conn)) == ([], [])


def test_missing_tag_names_the_available_count(conn):
    errors, warnings = run(
        "tag_options", "sip.unknown.home.example.net", context(conn)
    )
    assert len(errors) == 1
    assert "2 available" in errors[0]
    assert "sip.unknown.home.example.net" in errors[0]
    assert warnings == []


def test_empty_tag_is_rejected(conn):
    errors, _ = run("tag_options", "", context(conn))
    assert errors and "2 existing home domains" in errors[0]


def test_tag_with_no_homes_says_onboard_first(empty_db):
    connection = sqlite3.connect(empty_db)
    try:
        errors, _ = run("tag_options", O_EXAMPLE, context(connection))
    finally:
        connection.close()
    assert errors and "onboard a home" in errors[0]


# ---------------------------------------------------------------------------
# proto + priority invariants
# ---------------------------------------------------------------------------
def test_proto_is_locked_to_any():
    assert run("proto_any_locked", "any") == ([], [])
    assert run("proto_any_locked", "ANY") == ([], [])


@pytest.mark.parametrize("value", ["udp", "tcp", "sip", "", None])
def test_proto_other_values_are_rejected(value):
    errors, warnings = run("proto_any_locked", value)
    assert errors and "locked" in errors[0]
    assert warnings == []


@pytest.mark.parametrize("value", ["0", "1", "25", 0, 25])
def test_priority_accepts_non_negative_integers(value):
    assert run("priority_int", value) == ([], [])


def test_priority_rejects_negative_and_non_integers():
    errors, _ = run("priority_int", "-1")
    assert errors and "negative" in errors[0].lower()

    for value in ("", "abc", "1.5", None):
        errors, _ = run("priority_int", value)
        assert errors and "whole number" in errors[0].lower()
