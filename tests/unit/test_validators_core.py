"""Unit tests for console.validators — the named validation registry (T4.1).

The contract under test is exactly the one documented in ``validators.py``:
``fn(value, context) -> (errors, warnings)``; errors block, warnings do not.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from console.descriptors import Field, load
from console.validators import (
    PENDING_VALIDATORS,
    REGISTRY,
    ValidationContext,
    is_known,
    validate,
    validate_field,
)

REPO = Path(__file__).resolve().parents[2]
TABLES_YAML = REPO / "console" / "tables.yaml"


def run(name: str, value, context: ValidationContext | None = None):
    return validate(name, value, context or ValidationContext())


# ---------------------------------------------------------------------------
# registry contract
# ---------------------------------------------------------------------------
def test_registry_exposes_phase_one_rules():
    required = {
        "required",
        "pack_separator_safe",
        "label_pack_safe",
        "tenant_domain",
        "enum01",
        "digits",
        "ipv4",
        "sip_uri",
        "regex_compiles",
        "attrs_udp_warning",
    }
    assert required <= set(REGISTRY)
    assert all(callable(rule) for rule in REGISTRY.values())


def test_every_rule_has_the_documented_two_argument_signature():
    for name, rule in REGISTRY.items():
        parameters = list(inspect.signature(rule).parameters.values())
        positional = [
            p
            for p in parameters
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        ]
        assert len(positional) == 2, name
        assert all(p.default is p.empty for p in positional), name


def test_validate_returns_lists_and_unknown_names_raise():
    errors, warnings = run("digits", "12a")
    assert isinstance(errors, list) and isinstance(warnings, list)
    assert len(errors) == 1 and warnings == []
    with pytest.raises(KeyError):
        validate("no_such_rule", "x", ValidationContext())


def test_every_yaml_validator_is_registered_or_declared_pending():
    names = {
        field.validator
        for descriptor in load(TABLES_YAML).tables.values()
        for field in descriptor.fields
        if field.validator
    }
    unknown = names - set(REGISTRY) - set(PENDING_VALIDATORS)
    assert unknown == set(), f"tables.yaml names unknown validators: {sorted(unknown)}"
    # A rule that landed should not still be advertised as pending.
    assert set(REGISTRY) & set(PENDING_VALIDATORS) == set()


def test_is_known_distinguishes_pending_from_implemented():
    assert is_known("digits")
    # Families land in parallel; pick whichever name is still pending rather
    # than hard-coding one family's name (which would fail as soon as it lands).
    pending_names = sorted(PENDING_VALIDATORS)
    if pending_names:
        assert not is_known(pending_names[0])


def test_pending_rule_is_skipped_but_a_typo_blocks():
    pending_names = sorted(PENDING_VALIDATORS)
    if pending_names:
        pending = Field(name="tenantid", validator=pending_names[0])
        assert validate_field(pending, "anything", ValidationContext()) == ([], [])

    typo = Field(name="tenantid", validator="tenantid_unqie")
    errors, warnings = validate_field(typo, "T1", ValidationContext())
    assert errors and "Unsupported validator" in errors[0]
    assert warnings == []


def test_severity_warn_downgrades_a_rule_error_to_a_warning():
    field = Field(name="attrs", validator="digits", severity="warn")
    errors, warnings = validate_field(field, "not-digits", ValidationContext())
    assert errors == []
    assert warnings and "digits" in warnings[0].lower()


def test_no_validator_means_no_messages():
    field = Field(name="description", validator=None)
    assert validate_field(field, "", ValidationContext()) == ([], [])


# ---------------------------------------------------------------------------
# generic rules
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value", ["", "   ", None])
def test_required_rejects_empty(value):
    errors, warnings = run("required", value)
    assert errors and warnings == []


@pytest.mark.parametrize("value", ["x", 0, "0"])
def test_required_accepts_non_empty(value):
    assert run("required", value) == ([], [])


@pytest.mark.parametrize(
    "value",
    ["Delta, Example City", "A+R", " padded ", "x,", "x+"],
)
def test_pack_separator_safe_rejects_separators_and_padding(value):
    errors, _ = run("pack_separator_safe", value)
    assert errors


@pytest.mark.parametrize("value", ["", "Delta", "01111111", "home"])
def test_pack_separator_safe_accepts_plain_text(value):
    assert run("pack_separator_safe", value) == ([], [])


def test_label_pack_safe_requires_a_label_and_names_the_separator():
    errors, _ = run("label_pack_safe", "")
    assert errors and "display label" in errors[0].lower()

    errors, _ = run("label_pack_safe", "Delta, Example City")
    assert errors
    assert "comma" in errors[0].lower()
    assert "packing separator" in errors[0]

    assert run("label_pack_safe", "Delta") == ([], [])


# ---------------------------------------------------------------------------
# field-shape rules
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value", ["0", "1", 0, 1])
def test_enum01_accepts_zero_and_one(value):
    assert run("enum01", value) == ([], [])


@pytest.mark.parametrize("value", ["", "2", "true", "-1"])
def test_enum01_rejects_everything_else(value):
    errors, _ = run("enum01", value)
    assert errors


@pytest.mark.parametrize("value", ["0", "0077", "01111111"])
def test_digits_accepts_digit_strings(value):
    assert run("digits", value) == ([], [])


@pytest.mark.parametrize("value", ["", "12a", "+999", "12 34", "12.3"])
def test_digits_rejects_non_digits(value):
    errors, _ = run("digits", value)
    assert errors


@pytest.mark.parametrize("value", ["10.0.0.10", "100.64.10.14", "0.0.0.0"])
def test_ipv4_accepts_literal_addresses(value):
    assert run("ipv4", value) == ([], [])


@pytest.mark.parametrize("value", ["", "example.com", "999.1.1.1", "::1", "10.0.0"])
def test_ipv4_rejects_everything_else(value):
    errors, _ = run("ipv4", value)
    assert errors


# ---------------------------------------------------------------------------
# tenant domain (suffix + mtree charset)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value",
    [
        "sip.alpha.home.example.net",
        "sip.beta.home.example.net",
    ],
)
def test_tenant_domain_accepts_allowed_suffixes(value):
    assert run("tenant_domain", value) == ([], [])


def test_tenant_domain_rejects_uppercase():
    errors, _ = run("tenant_domain", "SIP.UPPER.Home.Example.Net")
    assert errors and "lower-case" in errors[0]


def test_tenant_domain_rejects_a_suffix_outside_the_allow_list():
    errors, _ = run("tenant_domain", "sip.beta.example.com")
    assert errors
    assert "must end with" in errors[0]
    assert "home.example.net" in errors[0]


def test_tenant_domain_rejects_the_suffix_itself():
    errors, _ = run("tenant_domain", "home.example.net")
    assert errors and "subdomain" in errors[0]


@pytest.mark.parametrize(
    "value",
    ["sip_bad.home.example.net", "sip/bad.home.example.net", "sip bad.home.example.net"],
)
def test_tenant_domain_rejects_unsafe_characters(value):
    errors, _ = run("tenant_domain", value)
    assert errors
    assert "letters, digits" in errors[0]


@pytest.mark.parametrize(
    "value",
    [
        ".sip.home.example.net",
        "sip-.home.example.net",
        "sip..home.example.net",
    ],
)
def test_tenant_domain_rejects_malformed_labels(value):
    errors, _ = run("tenant_domain", value)
    assert errors


def test_tenant_domain_empty_is_required_text():
    errors, _ = run("tenant_domain", "")
    assert errors and "SIP domain" in errors[0]


# ---------------------------------------------------------------------------
# SIP URI skeleton (literal IPv4; FQDN rejected per plan §5.2)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value",
    [
        "sip:10.0.0.10:5060;transport=tcp",
        "sip:10.0.0.10:5060;transport=udp",
        "sip:10.0.0.10",
        "sip:alice@10.0.0.10:5060;transport=tcp",
        "sip:10.0.0.10:5060;transport=tls;lr",
    ],
)
def test_sip_uri_accepts_literal_ipv4_uris(value):
    assert run("sip_uri", value) == ([], [])


@pytest.mark.parametrize(
    "value",
    [
        "sip:edge.example.com:5060;transport=tcp",
        "sip:voip-stack.lan:5060",
        "sip:[::1]:5060",
    ],
)
def test_sip_uri_rejects_fqdn_and_ipv6_hosts(value):
    errors, _ = run("sip_uri", value)
    assert errors
    assert "literal IPv4" in errors[0]


@pytest.mark.parametrize(
    "value",
    ["", "10.0.0.10:5060", "sip:", "sip:10.0.0.10:notaport", "sip:10.0.0.10:70000"],
)
def test_sip_uri_rejects_malformed_values(value):
    errors, _ = run("sip_uri", value)
    assert errors


# ---------------------------------------------------------------------------
# regex pre-check + PCRE warning
# ---------------------------------------------------------------------------
def test_regex_compiles_accepts_a_python_regex():
    assert run("regex_compiles", "^(999|998)$") == ([], [])


def test_regex_compiles_reports_a_syntax_error():
    errors, warnings = run("regex_compiles", "(")
    assert errors and "does not compile" in errors[0]
    assert warnings == []


def test_regex_compiles_warns_on_pcre_only_syntax():
    # \R compiles in Python (as a literal R) but means "any newline" in PCRE.
    errors, warnings = run("regex_compiles", r"^a\Rb$")
    assert errors == []
    assert warnings and "PCRE-only" in warnings[0]
    assert "approximate" in warnings[0]


def test_regex_compiles_requires_a_pattern():
    errors, _ = run("regex_compiles", "")
    assert errors


# ---------------------------------------------------------------------------
# advisory dispatcher rule used by the warnings-save API test
# ---------------------------------------------------------------------------
def test_attrs_udp_warning_only_fires_for_udp_transport():
    errors, warnings = run("attrs_udp_warning", "transport=udp")
    assert errors == []
    assert warnings and "OPTIONS pings stay green" in warnings[0]

    assert run("attrs_udp_warning", "transport=tcp") == ([], [])
    assert run("attrs_udp_warning", "") == ([], [])
