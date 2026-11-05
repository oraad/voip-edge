"""Unit tests for the T4.5 dispatcher rule family (plan §5.2, scenario 18).

Contract under test is the shared validator contract::

    fn(value, context) -> (errors, warnings)

Errors block; warnings save with a badge.  The destination matrix locks the
exact MTU/omitted-transport wording because operators read it verbatim.
"""
from __future__ import annotations

import sqlite3

import pytest

from console import validators
from console.rules import dispatcher
from console.validators import ValidationContext
from tests.fixtures import seeddb


def run(name: str, value, context: ValidationContext | None = None):
    return validators.validate(name, value, context or ValidationContext())


@pytest.fixture()
def conn(tmp_path):
    path = seeddb.seed(tmp_path / "edge.sqlite")
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# registry wiring
# ---------------------------------------------------------------------------
def test_family_names_are_registered_and_no_longer_pending():
    expected = {
        "setid_int",
        "destination_ipv4_required",
        "dispatcher_flags",
        "priority_int",
        "attrs_free_text",
    }
    assert set(dispatcher.RULES) == expected
    assert dispatcher.PENDING == frozenset(expected)
    for name in expected:
        assert name in validators.REGISTRY
        assert name not in validators.PENDING_VALIDATORS


# ---------------------------------------------------------------------------
# destination matrix
# ---------------------------------------------------------------------------
def test_ipv4_tcp_destination_is_clean():
    assert run("destination_ipv4_required", "sip:10.0.0.10:5060;transport=tcp") == (
        [],
        [],
    )


def test_ipv4_udp_destination_warns_with_the_locked_mtu_text():
    errors, warnings = run(
        "destination_ipv4_required", "sip:10.0.0.10:5060;transport=udp"
    )
    assert errors == []
    assert warnings == [
        "OPTIONS pings stay green while INVITEs fragment and die in the tunnel — "
        "use ;transport=tcp for tunnel destinations"
    ]
    assert warnings == [dispatcher.MTU_WARNING]
    assert "REGISTER" not in warnings[0]


def test_fqdn_destination_is_an_error_with_the_lan_ip_hint():
    errors, warnings = run(
        "destination_ipv4_required", "sip:voip-stack.lan:5060;transport=tcp"
    )
    assert errors
    assert "use the voip-stack host's LAN IP" in errors[0]
    assert warnings == []


def test_missing_transport_is_accepted_with_the_omitted_warning():
    errors, warnings = run("destination_ipv4_required", "sip:10.0.0.10:5060")
    assert errors == []
    assert warnings == [
        "transport omitted — treated as tcp; set ;transport=tcp explicitly"
    ]
    assert warnings == [dispatcher.TRANSPORT_OMITTED_WARNING]


def test_sips_scheme_is_accepted():
    assert run(
        "destination_ipv4_required", "sips:10.0.0.10:5060;transport=tcp"
    ) == ([], [])


def test_uppercase_scheme_and_transport_are_accepted():
    assert run(
        "destination_ipv4_required", "SIP:10.0.0.10:5060;Transport=TCP"
    ) == ([], [])


@pytest.mark.parametrize(
    "value",
    [
        "sip:10.0.0.10:0;transport=tcp",
        "sip:10.0.0.10:65536;transport=tcp",
        "sip:10.0.0.10:notaport;transport=tcp",
        "sip:10.0.0.10:5060;transport=tls",
        "sip:10.0.0.10:5060;transport=sctp",
        "sip:10.0.0.10:5060;transport=",
        "sip:[::1]:5060;transport=tcp",
    ],
)
def test_bad_port_and_unsupported_transport_are_errors(value):
    errors, _ = run("destination_ipv4_required", value)
    assert errors


@pytest.mark.parametrize(
    "value",
    [
        "",
        "sip:",
        "10.0.0.10:5060",
        "udp:10.0.0.10:5060",
        "sip:10.0.0.10:5060;transport=tcp extra",
    ],
)
def test_malformed_destinations_are_errors(value):
    errors, _ = run("destination_ipv4_required", value)
    assert errors


def test_destination_without_transport_but_with_userpart_is_supported():
    errors, warnings = run(
        "destination_ipv4_required", "sip:edge@10.0.0.10:5060"
    )
    assert errors == []
    assert warnings == [dispatcher.TRANSPORT_OMITTED_WARNING]


# ---------------------------------------------------------------------------
# setid and set_referenced (shared with T5's delete guard)
# ---------------------------------------------------------------------------
def test_referenced_setid_has_no_warning(conn):
    errors, warnings = run(
        "setid_int", 11, ValidationContext(conn=conn, values={"setid": 11})
    )
    assert (errors, warnings) == ([], [])


def test_unreferenced_setid_warns_without_blocking(conn):
    errors, warnings = run(
        "setid_int", 14, ValidationContext(conn=conn, values={"setid": 14})
    )
    assert errors == []
    assert len(warnings) == 1
    assert "not referenced by any home yet" in warnings[0]
    assert "14" in warnings[0]


def test_setid_without_a_connection_cannot_be_checked_and_does_not_warn():
    assert run("setid_int", 14) == ([], [])


@pytest.mark.parametrize("value", ["abc", "1.5", -1, "-2"])
def test_setid_rejects_non_integers_and_negatives(value):
    errors, _ = run("setid_int", value)
    assert errors


def test_setid_accepts_zero_as_a_whole_number():
    errors, warnings = run("setid_int", 0)
    assert errors == []
    assert warnings == []


def test_set_referenced_returns_onnet_and_pstn_domains(conn):
    assert dispatcher.set_referenced(conn, 11) == [
        "sip.alpha.home.example.net"
    ]
    assert dispatcher.set_referenced(conn, 31) == [
        "sip.alpha.home.example.net"
    ]
    assert dispatcher.set_referenced(conn, "32") == [
        "sip.beta.home.example.net"
    ]


def test_set_referenced_returns_empty_for_an_unused_set(conn):
    assert dispatcher.set_referenced(conn, 14) == []
    assert dispatcher.set_referenced(conn, 999) == []


def test_set_referenced_tolerates_a_missing_connection_or_bad_value(conn):
    assert dispatcher.set_referenced(None, 11) == []
    assert dispatcher.set_referenced(conn, "not-a-number") == []


def test_set_referenced_handles_several_domains(conn):
    conn.execute(
        "INSERT INTO tenant_registry (tprefix, tvalue) VALUES (?, ?)",
        ("sip.z.home.example.net", "T9,11,0,07719999,07719999,10,1+1"),
    )
    conn.commit()
    assert dispatcher.set_referenced(conn, 11) == [
        "sip.alpha.home.example.net",
        "sip.z.home.example.net",
    ]


# ---------------------------------------------------------------------------
# flags / priority
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value", [0, 1, 2, 4, 8, 16, "16"])
def test_known_flag_values_are_clean(value):
    assert run("dispatcher_flags", value) == ([], [])


@pytest.mark.parametrize("value", [3, 32, 99])
def test_unknown_flag_values_warn_without_blocking(value):
    errors, warnings = run("dispatcher_flags", value)
    assert errors == []
    assert warnings and "unusual" in warnings[0]
    assert "0, 1, 2, 4, 8, 16" in warnings[0]


@pytest.mark.parametrize("value", ["abc", -1, "-4"])
def test_flags_reject_non_integers_and_negatives(value):
    errors, _ = run("dispatcher_flags", value)
    assert errors


@pytest.mark.parametrize("value", [0, 5, "10"])
def test_priority_accepts_non_negative_integers(value):
    assert run("priority_int", value) == ([], [])


@pytest.mark.parametrize("value", ["abc", -1, "1.5"])
def test_priority_rejects_bad_integers(value):
    errors, _ = run("priority_int", value)
    assert errors


# ---------------------------------------------------------------------------
# attrs free text
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value",
    ["", "emergency Example", "transport=udp;note=keep", "a,b", "x\ty"],
)
def test_attrs_free_text_is_permissive(value):
    assert dispatcher.attrs_free_text(value, ValidationContext()) == ([], [])


@pytest.mark.parametrize("value", ["line\nbreak", "carriage\rreturn", "nul\x00byte"])
def test_attrs_free_text_rejects_nul_and_line_breaks(value):
    errors, warnings = dispatcher.attrs_free_text(value, ValidationContext())
    assert errors
    assert warnings == []
