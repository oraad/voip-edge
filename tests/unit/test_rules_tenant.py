"""Unit tests for the T4.2/T4.3 tenant_registry rule family.

The rules are exercised through their public validator signature
``fn(value, context) -> (errors, warnings)`` against a real seeded edge
database, so the packed rows are parsed exactly as the form parses them.
"""
from __future__ import annotations

import pytest

from console import db, descriptors
from console.rules import tenant as tenant_rules
from console.validators import REGISTRY, ValidationContext
from tests.fixtures import seeddb

TABLESET = descriptors.load()
REGISTRY_DESCRIPTOR = TABLESET.tables["tenant_registry"]

T1 = "sip.alpha.home.example.net"
T2 = "sip.beta.home.example.net"
T3 = "sip.gamma.home.example.net"


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
        descriptor=REGISTRY_DESCRIPTOR,
        values=values or {},
        conn=conn,
        mode=mode,
        key=key,
    )


def run(name: str, value, ctx: ValidationContext):
    return REGISTRY[name](value, ctx)


def assert_no_errors(result):
    errors, _ = result
    assert errors == []


# ---------------------------------------------------------------------------
# tenantid_unique
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value", ["T4", "A1", "0", "ABCDEFGHIJKLMNOP"])
def test_tenantid_accepts_uppercase_alphanumeric_up_to_16(value, conn):
    assert_no_errors(run("tenantid_unique", value, context(conn)))


@pytest.mark.parametrize("value", ["t4", "T-4", "T 4", "T_4", "TÉ", "T4!"])
def test_tenantid_rejects_bad_charset(value, conn):
    errors, warnings = run("tenantid_unique", value, context(conn))
    assert errors and "uppercase letters and digits" in errors[0]
    assert warnings == []


def test_tenantid_rejects_more_than_16_characters(conn):
    errors, _ = run("tenantid_unique", "A" * 17, context(conn))
    assert errors and "16 characters" in errors[0]


def test_tenantid_rejects_a_duplicate(conn):
    errors, _ = run("tenantid_unique", "T1", context(conn))
    assert errors and "already used by" in errors[0]
    assert T1 in errors[0]


def test_tenantid_edit_does_not_collide_with_itself(conn):
    ctx = context(conn, {"tenantid": "T1"}, mode="edit", key=T1)
    assert run("tenantid_unique", "T1", ctx) == ([], [])


def test_tenantid_empty_defers_to_the_packed_required_rule(conn):
    assert run("tenantid_unique", "", context(conn)) == ([], [])


# ---------------------------------------------------------------------------
# set_exists_onnet
# ---------------------------------------------------------------------------
def test_onnet_accepts_an_existing_set(conn):
    ctx = context(conn, {"tenantid": "T1", "ds_pstn": "31"})
    assert run("set_exists_onnet", "11", ctx) == ([], [])


def test_onnet_missing_set_is_an_imperative_error(conn):
    ctx = context(conn, {"tenantid": "T4", "ds_pstn": "31"})
    errors, warnings = run("set_exists_onnet", "99", ctx)
    assert errors and "create set 99" in errors[0]
    assert "Route sets" in errors[0]
    assert warnings == []


def test_onnet_convention_warns_but_keeps_the_save(conn):
    # The paired PSTN set 31 says N=1, so the convention expects on-net 11;
    # set 12 is legal but unusual and must only warn.
    ctx = context(conn, {"tenantid": "T2", "ds_pstn": "31"})
    errors, warnings = run("set_exists_onnet", "12", ctx)
    assert errors == []
    assert warnings and "10+N" in warnings[0]
    assert "expected set 11" in warnings[0]


def test_onnet_convention_is_quiet_for_the_seeded_pair(conn):
    # T2's real row is 12/32: both sides of the convention agree.
    ctx = context(conn, {"tenantid": "T2", "ds_pstn": "32"})
    assert run("set_exists_onnet", "12", ctx) == ([], [])


def test_onnet_no_warning_when_the_convention_is_not_applicable(conn):
    ctx = context(conn, {"tenantid": "HOME", "ds_pstn": "0"})
    assert run("set_exists_onnet", "13", ctx) == ([], [])


def test_onnet_rejects_a_non_number(conn):
    errors, _ = run("set_exists_onnet", "eleven", context(conn))
    assert errors and "whole number" in errors[0]


# ---------------------------------------------------------------------------
# pstn_set_exists_or_zero
# ---------------------------------------------------------------------------
def test_pstn_zero_is_allowed(conn):
    assert run("pstn_set_exists_or_zero", "0", context(conn)) == ([], [])


def test_pstn_accepts_an_existing_set(conn):
    assert run("pstn_set_exists_or_zero", "31", context(conn)) == ([], [])


def test_pstn_missing_set_names_the_set(conn):
    errors, _ = run("pstn_set_exists_or_zero", "99", context(conn))
    assert errors and "set 99" in errors[0]
    assert "PSTN" in errors[0]


def test_pstn_rejects_a_non_number(conn):
    errors, _ = run("pstn_set_exists_or_zero", "trunk", context(conn))
    assert errors and "whole number" in errors[0]


# ---------------------------------------------------------------------------
# cli_unique
# ---------------------------------------------------------------------------
def test_cli_accepts_digits_with_no_neighbour(conn):
    ctx = context(conn, {"tenantid": "T4"})
    assert run("cli_unique", "05555555", ctx) == ([], [])


def test_cli_rejects_non_digits(conn):
    errors, _ = run("cli_unique", "077-100", context(conn))
    assert errors and "digits" in errors[0].lower()


def test_cli_exact_collision_with_another_landline_is_an_error(conn):
    ctx = context(conn, {"tenantid": "T4"})
    errors, warnings = run("cli_unique", "01111111", ctx)
    assert errors and "already the landline of T1" in errors[0]
    assert warnings == []


def test_cli_prefix_overlap_shorter_is_a_warning(conn):
    ctx = context(conn, {"tenantid": "T4"})
    errors, warnings = run("cli_unique", "0222222", ctx)
    assert errors == []
    assert warnings and "prefix-overlaps" in warnings[0]


def test_cli_prefix_overlap_longer_is_a_warning(conn):
    ctx = context(conn, {"tenantid": "T4"})
    errors, warnings = run("cli_unique", "022222221", ctx)
    assert errors == []
    assert warnings and "prefix-overlaps" in warnings[0]


def test_cli_edit_never_matches_its_own_tenant(conn):
    ctx = context(conn, {"tenantid": "T2"}, mode="edit", key=T2)
    assert run("cli_unique", "02222222", ctx) == ([], [])


def test_cli_edit_ignores_its_own_registry_cli_even_if_tenantid_changes(conn):
    # The stored row is still T2's landline; changing the submitted tenantid
    # must not turn the home's own CLI into a collision.
    ctx = context(conn, {"tenantid": "T9"}, mode="edit", key=T2)
    assert run("cli_unique", "02222222", ctx) == ([], [])


# ---------------------------------------------------------------------------
# dpid_exists
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value", ["10", "90"])
def test_dpid_accepts_existing_partitions(value, conn):
    assert run("dpid_exists", value, context(conn)) == ([], [])


def test_dpid_missing_partition_is_an_imperative_error(conn):
    errors, _ = run("dpid_exists", "99", context(conn))
    assert errors and "partition 99" in errors[0]
    assert "Number rules" in errors[0]


def test_dpid_rejects_a_non_number(conn):
    errors, _ = run("dpid_exists", "emergency", context(conn))
    assert errors and "whole number" in errors[0]


# ---------------------------------------------------------------------------
# flags_valid
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value",
    ["1+1+90", "0+0", "1+0+10", "1+1", "1+1+0", "1+1+"],
)
def test_flags_accepts_the_documented_forms_and_empty_block(value, conn):
    assert run("flags_valid", value, context(conn)) == ([], [])


@pytest.mark.parametrize("value", ["2+1", "1+x", "1+1+y", "1+1+90+5", "1+1+-5"])
def test_flags_rejects_malformed_values(value, conn):
    errors, _ = run("flags_valid", value, context(conn))
    assert errors and "ih_on+pstn_on" in errors[0]


def test_flags_missing_block_partition_is_an_imperative_error(conn):
    errors, _ = run("flags_valid", "1+1+91", context(conn))
    assert errors and "partition 91" in errors[0]
    assert "Number rules" in errors[0]


def test_flags_empty_defers_to_the_packed_required_rule(conn):
    assert run("flags_valid", "", context(conn)) == ([], [])


# ---------------------------------------------------------------------------
# family registration contract
# ---------------------------------------------------------------------------
def test_family_registers_every_name_and_clears_pending():
    from console.validators import PENDING_VALIDATORS, is_known

    assert set(tenant_rules.RULES) == {
        "tenantid_unique",
        "set_exists_onnet",
        "pstn_set_exists_or_zero",
        "cli_unique",
        "dpid_exists",
        "flags_valid",
    }
    assert set(tenant_rules.PENDING) == set(tenant_rules.RULES)
    for name in tenant_rules.RULES:
        assert is_known(name), name
        assert name not in PENDING_VALIDATORS, name
