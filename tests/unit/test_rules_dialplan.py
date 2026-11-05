"""Unit tests for the T4.7 dialplan rule family (plan §5.2, matrix V-DP-*).

Contract under test is the shared validator contract::

    fn(value, context) -> (errors, warnings)

Errors block; warnings save with a badge.  The dialplan family is deliberately
narrow: regex compilation/PCRE parity is reused from
:func:`console.validators.regex_compiles`; everything else here is the
match-only invariant of this deployment.

``translate_proof`` is *not* a field validator — it is the post-reload
behavioural proof helper, exercised against a real unix-datagram ``FakeRPC``.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from console import validators
from console.descriptors import Field
from console.rpc import UnixDatagramRPC
from console.rules import dialplan
from console.validators import ValidationContext
from tests.fixtures import seeddb
from tests.fixtures.fakerpc import FakeRPC

REPO = Path(__file__).resolve().parents[2]
TABLES_YAML = REPO / "console" / "tables.yaml"

IDENTITY = {"subst_exp": r"^(.*)$", "repl_exp": "\\1"}


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


@pytest.fixture()
def rpc_fake(tmp_path):
    fake = FakeRPC(tmp_path / "kamailio_rpc.sock", timeout_ms=200)
    try:
        yield fake
    finally:
        fake.stop()


# ---------------------------------------------------------------------------
# registry wiring
# ---------------------------------------------------------------------------
def test_family_names_are_registered_and_no_longer_pending():
    expected = {
        "dpid_int",
        "pr_int",
        "match_op_locked",
        "match_len_zero",
        "identity_locked",
        "attrs_free_text",
    }
    assert set(dialplan.RULES) == expected
    assert dialplan.PENDING == frozenset(expected)
    for name in expected:
        assert name in validators.REGISTRY
        assert name not in validators.PENDING_VALIDATORS


def test_translate_proof_is_not_a_field_validator():
    assert "translate_proof" not in dialplan.RULES
    assert "translate_proof" not in validators.REGISTRY


# ---------------------------------------------------------------------------
# regex pre-check parity (reused shared rule, never re-implemented here)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "pattern",
    [
        r"(?<area>999)",  # PCRE named group (Python wants (?P<area>…))
        r"(?>999|998)",  # atomic group
        r"^999\K$",  # PCRE \K
    ],
)
def test_pcre_only_match_exp_warns_instead_of_erroring(pattern):
    errors, warnings = run("regex_compiles", pattern)
    assert errors == []
    assert warnings and "PCRE-only" in warnings[0]
    assert "approximate" in warnings[0]


def test_python_regex_match_exp_is_clean():
    assert run("regex_compiles", r"^(999|998)$") == ([], [])


# ---------------------------------------------------------------------------
# dpid_int — non-negative integer, unreferenced partition is a warning
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value", [0, 10, 90, "0", "10", " 77 "])
def test_dpid_int_accepts_non_negative_integers(value):
    assert run("dpid_int", value, ValidationContext(conn=None)) == ([], [])


@pytest.mark.parametrize("value", [-1, "-1", "x", "", None, True, 1.5])
def test_dpid_int_rejects_non_integers_and_negatives(value):
    errors, warnings = run("dpid_int", value, ValidationContext(conn=None))
    assert errors
    assert warnings == []


def test_dpid_int_is_clean_when_a_home_uses_the_partition(conn):
    # Every seeded home has dpid=10; their flags also reference the block
    # partition 90 (1+1+90), so both are "referenced".
    assert run("dpid_int", 10, ValidationContext(conn=conn)) == ([], [])
    assert run("dpid_int", 90, ValidationContext(conn=conn)) == ([], [])


def test_dpid_int_warns_when_no_registry_row_references_it(conn):
    errors, warnings = run("dpid_int", 77, ValidationContext(conn=conn))
    assert errors == []
    assert warnings and "not referenced" in warnings[0].lower()
    assert "77" in warnings[0]


def test_dpid_int_skips_the_reference_warning_without_a_connection():
    assert run("dpid_int", 77, ValidationContext()) == ([], [])


# ---------------------------------------------------------------------------
# pr_int — priority 1..100
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value", [1, 50, 100, "1", "100", " 42 "])
def test_pr_int_accepts_one_to_one_hundred(value):
    assert run("pr_int", value) == ([], [])


@pytest.mark.parametrize("value", [0, 101, -1, "0", "101", "x", "", None, True])
def test_pr_int_rejects_outside_the_range(value):
    errors, warnings = run("pr_int", value)
    assert errors
    assert warnings == []


# ---------------------------------------------------------------------------
# match_op_locked — regex matching only
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value", [1, "1", " 1 "])
def test_match_op_locked_accepts_one(value):
    assert run("match_op_locked", value) == ([], [])


@pytest.mark.parametrize("value", [0, 2, "0", "2", "", None, "regex"])
def test_match_op_locked_rejects_every_other_value(value):
    errors, warnings = run("match_op_locked", value)
    assert errors
    assert "regex matching only in this console" in errors[0]
    assert warnings == []


# ---------------------------------------------------------------------------
# match_len_zero — this deployment matches on the expression only
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value", [0, "0", " 0 "])
def test_match_len_zero_accepts_zero(value):
    assert run("match_len_zero", value) == ([], [])


@pytest.mark.parametrize("value", [1, -1, "1", "12", "", None, True])
def test_match_len_zero_rejects_other_values(value):
    errors, warnings = run("match_len_zero", value)
    assert errors
    assert "0" in errors[0]
    assert warnings == []


# ---------------------------------------------------------------------------
# identity_locked — subst/repl are the locked identity rewrite
# ---------------------------------------------------------------------------
def test_identity_locked_accepts_the_identity_pair():
    assert run("identity_locked", r"^(.*)$", ValidationContext(values=IDENTITY)) == (
        [],
        [],
    )


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"subst_exp": r"^(.*)$"},
        {"repl_exp": "\\1"},
    ],
)
def test_identity_locked_ignores_absent_fields(values):
    assert run("identity_locked", "tampered", ValidationContext(values=values)) == (
        [],
        [],
    )


def test_identity_locked_rejects_a_tampered_subst_exp():
    values = dict(IDENTITY, subst_exp=r"^(.*)x$")
    errors, warnings = run(
        "identity_locked", r"^(.*)x$", ValidationContext(values=values)
    )
    assert errors
    assert "identity rewrite by design" in errors[0].lower()
    assert "matching only" in errors[0].lower()
    assert warnings == []


def test_identity_locked_rejects_a_tampered_repl_exp():
    values = dict(IDENTITY, repl_exp="\\1x")
    errors, warnings = run(
        "identity_locked", "\\1x", ValidationContext(values=values)
    )
    assert errors
    assert "identity rewrite by design" in errors[0].lower()


def test_identity_locked_is_field_precise_when_the_context_names_the_field():
    tampered = dict(IDENTITY, repl_exp="\\1x")

    subst_only = ValidationContext(
        field=Field(name="subst_exp"), values=tampered
    )
    assert run("identity_locked", r"^(.*)$", subst_only) == ([], [])

    repl_only = ValidationContext(field=Field(name="repl_exp"), values=tampered)
    errors, _ = run("identity_locked", "\\1x", repl_only)
    assert errors


# ---------------------------------------------------------------------------
# attrs_free_text — single-line free text; only NUL/newlines are rejected
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value",
    [
        "",
        "emergency example",
        "a,b;transport=udp",
        "9000 premium range\tblocked",
        "T1+1+90",
    ],
)
def test_attrs_free_text_accepts_plain_text(value):
    assert run("attrs_free_text", value) == ([], [])


@pytest.mark.parametrize(
    "value", ["line\nbreak", "carriage\rreturn", "nul\x00byte", "both\r\n"]
)
def test_attrs_free_text_rejects_newlines_and_nuls(value):
    errors, warnings = run("attrs_free_text", value)
    assert errors
    assert warnings == []


# ---------------------------------------------------------------------------
# translate_proof — post-reload behavioural proof over the real socket
# ---------------------------------------------------------------------------
def _rpc(fake: FakeRPC) -> UnixDatagramRPC:
    return UnixDatagramRPC(fake.sock_path, timeout_ms=200)


def test_translate_proof_reports_a_match_with_plain_string_params(rpc_fake):
    rpc_fake.script("dialplan.translate", "01111111")

    matched, message = dialplan.translate_proof(_rpc(rpc_fake), 10, "01111111")

    assert matched is True
    assert "matched" in message.lower()
    assert "01111111" in message

    call = rpc_fake.last_call
    assert call is not None
    assert call.method == "dialplan.translate"
    # Plain JSON-RPC values: integer dpid, plain sample string.  The kamctl
    # "s:" type prefix must never reach the edge.
    assert call.params == [10, "01111111"]
    assert all(not str(param).startswith("s:") for param in call.params)


@pytest.mark.parametrize("result", ["", -1, "0", 0, False, []])
def test_translate_proof_reports_no_match(rpc_fake, result):
    rpc_fake.script("dialplan.translate", result)

    matched, message = dialplan.translate_proof(_rpc(rpc_fake), 90, "01234567")

    assert matched is False
    assert "no dialplan rule" in message.lower()
    assert "90" in message
    assert "01234567" in message


def test_translate_proof_surfaces_a_fault_verbatim(rpc_fake):
    rpc_fake.fault("dialplan.translate", code=500, message="scripted fault")

    matched, message = dialplan.translate_proof(_rpc(rpc_fake), 10, "999")

    assert matched is False
    assert "scripted fault" in message


def test_translate_proof_reads_a_wrapped_rpc_result(rpc_fake):
    rpc_fake.script(
        "dialplan.translate", {"result": "sip:01111111@10.0.0.10"}
    )

    matched, message = dialplan.translate_proof(_rpc(rpc_fake), 10, "01111111")

    assert matched is True
    assert "sip:01111111@10.0.0.10" in message


def test_translate_proof_treats_a_zero_status_object_as_no_match(rpc_fake):
    rpc_fake.script("dialplan.translate", {"result": 0})

    matched, message = dialplan.translate_proof(_rpc(rpc_fake), 10, "01111111")

    assert matched is False
    assert "no dialplan rule" in message.lower()
