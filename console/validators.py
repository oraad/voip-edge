"""validators.py — named validation rules for descriptor fields (plan §5.1/§5.2).

Contract
--------
A validator is a plain callable::

    fn(value, context) -> tuple[list[str], list[str]]

* ``value``   — the raw value taken from the submitted form (always the text
  the operator typed; type coercion happens later, in ``forms_ui``);
* ``context`` — a :class:`ValidationContext` carrying the descriptor, field,
  all submitted values, the live connection, the mode (``"create"`` /
  ``"edit"``), the original key and the active settings.

It returns ``(errors, warnings)``.  **Errors block the save (HTTP 422);
warnings are shown as a badge/hint and never block it.**  Returning
``([], [])`` means the value is acceptable.

The registry is intentionally a flat ``dict[str, Callable]`` so a descriptor
only names a rule (``validator: tenant_domain``) and never embeds code.
``tables.yaml`` is the single place that decides which rule a field uses and
which severity applies to it.

Severity
--------
The descriptor's ``severity`` is applied by :func:`validate_field`:

* ``severity: error`` (default) — errors stay errors;
* ``severity: warn`` — every error returned by the rule is downgraded to a
  warning.  Use this for advisory rules such as "UDP on the tunnel".

Phase-1 scope
-------------
T4.1 ships the small, generic rules listed in :data:`REGISTRY`, plus
:data:`PENDING_VALIDATORS`: names that already appear in ``tables.yaml`` but
belong to the T4.2–T4.8 families.  A pending name is **skipped** (it cannot
block a save) so the form framework works while those families land.  An
unknown name that is neither registered nor declared pending is an error — a
typo must never be silently ignored.
"""
from __future__ import annotations

import ipaddress
import re
import sqlite3
from dataclasses import dataclass, field as dataclass_field
from typing import Any, Callable, Mapping

from . import settings as settings_module

__all__ = [
    "REGISTRY",
    "PENDING_VALIDATORS",
    "ValidationContext",
    "validate",
    "validate_field",
    "is_known",
]

#: Validator signature: ``(value, context) -> (errors, warnings)``.
Validator = Callable[[Any, "ValidationContext"], tuple[list[str], list[str]]]


@dataclass
class ValidationContext:
    """Everything a rule may need, without giving it access to the router.

    All attributes are optional so unit tests can construct a bare context::

        validate("digits", "123", ValidationContext())

    ``values`` holds the *effective* values for this submission (stored values
    for disabled/locked fields, posted values elsewhere), so cross-field rules
    see what would actually be written.
    """

    descriptor: Any | None = None
    field: Any | None = None
    values: Mapping[str, Any] = dataclass_field(default_factory=dict)
    conn: sqlite3.Connection | None = None
    mode: str = "create"
    key: str | None = None
    settings: Any = dataclass_field(default_factory=lambda: settings_module.settings)

    # Convenience accessors used by family rules; never mutate the context.
    def get(self, name: str, default: Any = None) -> Any:
        return self.values.get(name, default)

    @property
    def table(self) -> str | None:
        return getattr(self.descriptor, "table", None)


# ---------------------------------------------------------------------------
# generic rules
# ---------------------------------------------------------------------------
def required(value: Any, context: ValidationContext) -> tuple[list[str], list[str]]:
    """Reject empty/whitespace-only values."""
    if value is None or str(value).strip() == "":
        return (["This field is required."], [])
    return ([], [])


def pack_separator_safe(
    value: Any, context: ValidationContext
) -> tuple[list[str], list[str]]:
    """Reject the packed-record separators and whitespace padding.

    Empty values are legal here (optional packed text); compose with
    :func:`required` when a value is mandatory.
    """
    text = "" if value is None else str(value)
    if text == "":
        return ([], [])
    errors: list[str] = []
    if "," in text:
        errors.append(
            "Remove the comma — “,” and “+” are packing separators."
        )
    elif "+" in text:
        errors.append(
            "Remove the plus sign — “,” and “+” are packing separators."
        )
    if text != text.strip():
        errors.append("Remove the leading or trailing spaces.")
    return (errors, [])


def label_pack_safe(
    value: Any, context: ValidationContext
) -> tuple[list[str], list[str]]:
    """A display label: mandatory, and safe inside a packed value."""
    text = "" if value is None else str(value)
    if text.strip() == "":
        return (["Enter a display label."], [])
    return pack_separator_safe(text, context)


def enum01(value: Any, context: ValidationContext) -> tuple[list[str], list[str]]:
    """Accept only the two documented flag values, ``0`` and ``1``."""
    text = "" if value is None else str(value).strip()
    if text in {"0", "1"}:
        return ([], [])
    if text == "":
        return (["Choose 0 or 1."], [])
    return ([f"Use 0 or 1, not {text!r}."], [])


def digits(value: Any, context: ValidationContext) -> tuple[list[str], list[str]]:
    """Digits only, at least one; leading zeros are preserved."""
    text = "" if value is None else str(value).strip()
    if text == "":
        return (["Enter digits only — no spaces or separators."], [])
    if not text.isdigit():
        return (["Use digits only — no spaces, dots or separators."], [])
    return ([], [])


def ipv4(value: Any, context: ValidationContext) -> tuple[list[str], list[str]]:
    """A literal IPv4 address (IPv6 and host names are rejected)."""
    text = "" if value is None else str(value).strip()
    if text == "":
        return (["Enter an IP address."], [])
    try:
        ipaddress.IPv4Address(text)
    except ValueError:
        return ([f"Use a literal IPv4 address such as 10.0.0.10, not {text!r}."], [])
    return ([], [])


# mtree's shared char_list in the edge config (see plan §5.2 / scenario 17):
# digits, plus, ASCII letters, dot and dash.  Domains use the subset without
# the plus sign.
_MTREE_DOMAIN_CHARS = re.compile(r"^[A-Za-z0-9.-]+$")
_LABEL = re.compile(r"^[A-Za-z0-9-]+$")


def tenant_domain(value: Any, context: ValidationContext) -> tuple[list[str], list[str]]:
    """Home SIP domain: mtree-safe characters, sane labels, allowed suffix.

    The rule deliberately does not check uniqueness (that is a create/edit
    family rule with DB access) — it only proves the *shape* Kamailio's
    ``tenantdom`` tree can load.
    """
    text = "" if value is None else str(value).strip()
    if text == "":
        return (["Enter the home SIP domain."], [])
    if not _MTREE_DOMAIN_CHARS.fullmatch(text):
        return (
            [
                "Use only letters, digits, dots and dashes in the domain "
                "(the mtree character list does not allow anything else)."
            ],
            [],
        )

    errors: list[str] = []
    if text.startswith(".") or text.endswith("."):
        errors.append("The domain may not start or end with a dot.")
    if text.startswith("-") or text.endswith("-"):
        errors.append("The domain may not start or end with a dash.")
    if ".." in text:
        errors.append("The domain may not contain two dots in a row.")
    labels = text.split(".")
    if any(label == "" or len(label) > 63 for label in labels):
        errors.append("Each dot-separated label must be 1–63 characters.")
    if any(label.startswith("-") or label.endswith("-") for label in labels):
        errors.append("A dot-separated label may not start or end with a dash.")
    if len(text) > 253:
        errors.append("The domain is longer than 253 characters.")

    suffixes = tuple(
        str(suffix).strip().lstrip(".")
        for suffix in (getattr(context.settings, "allowed_suffixes", ()) or ())
        if str(suffix).strip()
    )
    if suffixes:
        lowered = text.lower()
        exact = next(
            (suffix for suffix in suffixes if lowered == suffix.lower()), None
        )
        matched = next(
            (suffix for suffix in suffixes if lowered.endswith("." + suffix.lower())),
            None,
        )
        if exact is not None:
            errors.append(
                f"The domain must be a subdomain of {exact}, not the suffix itself."
            )
        elif matched is None:
            allowed = ", ".join(suffixes)
            errors.append(f"The domain must end with one of: {allowed}.")
    return (errors, [])


# sip:user@host:port;transport=tcp — the skeleton for dispatcher destinations.
_SIP_PARAM = re.compile(r"^[A-Za-z0-9_.-]+(?:=[A-Za-z0-9_.:+-]*)?$")
_SIP_USER = re.compile(r"^[A-Za-z0-9_.+~-]+$")


def sip_uri(value: Any, context: ValidationContext) -> tuple[list[str], list[str]]:
    """Validate a plain ``sip:`` URI and require a **literal IPv4** host.

    Plan §5.2 locks dispatcher destinations to a literal IPv4 host; an FQDN is
    rejected with the LAN-IP hint.  Transport policy (mandatory ``transport=``,
    UDP MTU warning) belongs to the dispatcher family rule so this skeleton
    stays useful for any SIP URI field.
    """
    text = "" if value is None else str(value).strip()
    if text == "":
        return (["Enter a SIP URI such as sip:10.0.0.10:5060;transport=tcp."], [])
    if any(character.isspace() for character in text):
        return (["The SIP URI may not contain spaces."], [])
    if not text.lower().startswith("sip:"):
        return (["Start the URI with sip: (for example sip:10.0.0.10:5060)."], [])

    rest = text[4:]
    if "@" in rest:
        user, rest = rest.split("@", 1)
        if not user or not _SIP_USER.fullmatch(user):
            return (["The user part of the SIP URI contains invalid characters."], [])

    host_port, *raw_params = rest.split(";")
    if not host_port:
        return (["The SIP URI has no host."], [])

    host = host_port
    port_text: str | None = None
    if host_port.startswith("["):
        return (
            [
                "Use a literal IPv4 host; IPv6 literals are not supported on "
                "the edge tunnel."
            ],
            [],
        )
    if host_port.count(":") > 1:
        return (["Use a literal IPv4 host; IPv6 literals are not supported."], [])
    if ":" in host_port:
        host, port_text = host_port.rsplit(":", 1)

    try:
        ipaddress.IPv4Address(host)
    except ValueError:
        return (
            [
                "The destination host must be a literal IPv4 address "
                f"(got {host!r}); use the voip-stack host's LAN IP, not an FQDN."
            ],
            [],
        )

    errors: list[str] = []
    if port_text is not None:
        if not port_text.isdigit():
            errors.append("The SIP URI port must be a whole number.")
        else:
            port = int(port_text)
            if not 1 <= port <= 65535:
                errors.append("The SIP URI port must be between 1 and 65535.")

    for raw_param in raw_params:
        if not _SIP_PARAM.fullmatch(raw_param):
            errors.append(f"Invalid SIP URI parameter {raw_param!r}.")
            continue
        name, _, param_value = raw_param.partition("=")
        if name.lower() == "transport" and param_value.lower() not in {
            "tcp",
            "udp",
            "tls",
            "sctp",
        }:
            errors.append(
                "transport must be one of tcp, udp, tls or sctp."
            )
    return (errors, [])


# PCRE-only constructs that Python's re either rejects or interprets
# differently.  A successful Python compile is therefore only approximate.
_PCRE_MARKERS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\(\?<[A-Za-z_]"), "named groups (?<name>…)"),
    (re.compile(r"\\[pP]\{"), r"Unicode properties \p{…}"),
    (re.compile(r"\(\?>"), "atomic groups (?>…)"),
    (re.compile(r"\(\?\|"), "branch-reset groups (?|…)"),
    (re.compile(r"\\K"), r"\K"),
    (re.compile(r"\\[RHh]"), r"PCRE escapes \R / \h / \H"),
    (
        re.compile(r"(?:[*+?]|\{\d+(?:,\d*)?\})\+"),
        "possessive quantifiers (*+, ++, ?+, {n,m}+)",
    ),
)


def regex_compiles(
    value: Any, context: ValidationContext
) -> tuple[list[str], list[str]]:
    """The pattern must compile in Python; PCRE-only syntax is a warning.

    Kamailio uses PCRE while the console can only pre-check with ``re``.  A
    pattern that compiles but uses PCRE-only constructs gets a severity
    warning ("approximate check", plan §5.2/§7.7).
    """
    text = "" if value is None else str(value)
    if text == "":
        return (["Enter a regular expression."], [])

    markers = [label for pattern, label in _PCRE_MARKERS if pattern.search(text)]
    warning = ""
    if markers:
        warning = (
            "This uses PCRE-only syntax ("
            + ", ".join(markers)
            + "); Kamailio evaluates it with PCRE, so the Python check is "
            "approximate — verify with a live reload."
        )

    try:
        re.compile(text)
    except re.error as exc:
        if warning:
            # Python's re cannot even parse this construct.  The plan makes
            # PCRE-only syntax a warning rather than a block (§5.2), so the
            # operator sees the parity caveat and Kamailio makes the decision.
            return ([], [warning])
        return ([f"This pattern does not compile: {exc}."], [])

    return ([], [warning] if warning else [])


def attrs_udp_warning(
    value: Any, context: ValidationContext
) -> tuple[list[str], list[str]]:
    """Plan §5.2 / scenario 18: UDP on the tunnel is a warning, not a block.

    The console never infers REGISTER transport from this value.
    """
    text = "" if value is None else str(value).lower()
    if "transport=udp" in text or "transport = udp" in text:
        return (
            [],
            [
                "UDP on the tunnel: OPTIONS pings stay green while INVITEs "
                "fragment and die in the tunnel."
            ],
        )
    return ([], [])


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------
REGISTRY: dict[str, Validator] = {
    "required": required,
    "pack_separator_safe": pack_separator_safe,
    "label_pack_safe": label_pack_safe,
    "enum01": enum01,
    "digits": digits,
    "ipv4": ipv4,
    "tenant_domain": tenant_domain,
    "sip_uri": sip_uri,
    "regex_compiles": regex_compiles,
    # Advisory rule shipped early because the warnings-save path is part of
    # the T4.1 pattern (plan §5.2 dispatcher row).
    "attrs_udp_warning": attrs_udp_warning,
}

#: Names declared by the family tables but owned by T4.2–T4.8.  They are
#: skipped until their implementation lands; ``tests/unit/test_validators_core``
#: asserts that every name in ``tables.yaml`` is either registered or listed
#: here, so a typo still fails loudly.
PENDING_VALIDATORS: frozenset[str] = frozenset(
    {
        # tenant_registry family (T4.2/T4.3)
        "tenantid_unique",
        "set_exists_onnet",
        "pstn_set_exists_or_zero",
        "cli_unique",
        "dpid_exists",
        "flags_valid",
        # tenant_landline family (T4.4)
        "landline_digits",
        "tenant_exists",
        "target_identifier",
        # dispatcher family (T4.5)
        "setid_int",
        "destination_ipv4_required",
        "dispatcher_flags",
        "priority_int",
        # dialplan family (T4.7)
        "dpid_int",
        "pr_int",
        "match_op_locked",
        "match_len_zero",
        "identity_locked",
        "attrs_free_text",
        # trusted family (T4.6)
        "trusted_src_ip",
        "proto_any_locked",
        "tag_options",
    }
)


def is_known(name: str) -> bool:
    """True when *name* is implemented (not merely pending)."""
    return name in REGISTRY


def validate(
    name: str, value: Any, context: ValidationContext
) -> tuple[list[str], list[str]]:
    """Run a registered rule by name.  Unknown names raise ``KeyError``."""
    rule = REGISTRY[name]
    errors, warnings = rule(value, context)
    return (list(errors), list(warnings))


def validate_field(
    field: Any, value: Any, context: ValidationContext
) -> tuple[list[str], list[str]]:
    """Run the field's named rule and apply the descriptor's severity.

    ``field.validator`` may be ``None`` (no rule), a registered rule, or a
    name in :data:`PENDING_VALIDATORS` (skipped until its family lands).
    Any other name is a configuration error and is surfaced as a blocking
    error rather than silently dropping a rule.
    """
    name = getattr(field, "validator", None)
    if not name:
        return ([], [])
    if name in PENDING_VALIDATORS and name not in REGISTRY:
        return ([], [])
    if name not in REGISTRY:
        return ([f"Unsupported validator {name!r} (console configuration error)."], [])

    errors, warnings = validate(name, value, context)
    if getattr(field, "severity", "error") == "warn":
        warnings = [*warnings, *errors]
        errors = []
    return (errors, warnings)
