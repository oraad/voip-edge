"""dialplan.py — T4.7 validation family for the standard ``dialplan`` table.

Plan §5.2 (dialplan row) and the V-DP-* rows in ``docs/validation-matrix.md``
lock this deployment to **match-only** rules:

* ``match_exp`` is pre-checked by the shared
  :func:`console.validators.regex_compiles` rule (Python ``re`` error, PCRE-only
  syntax is an "approximate check" warning).  It is *not* re-implemented here;
  ``tables.yaml`` already names it for the ``match_exp`` field.
* ``match_op`` must be ``1`` (regex match);
* ``match_len`` must be ``0`` (the expression alone selects the destination);
* ``subst_exp``/``repl_exp`` are the locked identity rewrite ``^(.*)$`` / ``\\1``
  — the rule matches, it never rewrites;
* ``attrs`` is free text: only NUL and line breaks cannot survive the column/UI
  round trip, so only those are rejected;
* ``dpid`` is a non-negative integer and warns when no home references the
  partition.

The rewrite is "match-only by design".  Kamailio's dialplan can rewrite the
R-URI; this console deliberately does not expose that, so the identity pair is
enforced server-side even for a forged POST of the hidden/locked inputs.

``translate_proof`` is the post-reload behavioural proof helper (plan §3.3,
§5.2): it calls ``dialplan.translate`` with plain JSON-RPC values — an integer
partition and a plain sample string, never the kamctl ``s:`` type prefix — and
returns ``(matched, message)`` for the toast.  It is **not** a field validator
and is therefore deliberately absent from :data:`RULES`.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any

from .. import db, descriptors
from ..errors import ConsoleError
from ..pack import parse_flags
from ..validators import ValidationContext, Validator

__all__ = [
    "RULES",
    "PENDING",
    "IDENTITY_ERROR",
    "PARTITION_UNREFERENCED_WARNING",
    "REPL_IDENTITY",
    "SUBST_IDENTITY",
    "TRANSLATE_METHOD",
    "referenced_dpids",
    "translate_proof",
]

#: The locked identity rewrite (plan §5.2: "match-only by design").
SUBST_IDENTITY = r"^(.*)$"
REPL_IDENTITY = "\\1"

#: Locked invariant text (the em dash is intentional).
IDENTITY_ERROR = (
    "Identity rewrite by design — matching only: subst_exp must be ^(.*)$ "
    "and repl_exp must be \\1."
)

#: Non-blocking partition badge text; mirrors the dispatcher family wording.
PARTITION_UNREFERENCED_WARNING = "not referenced by any home yet"

#: The JSON-RPC method used for the behavioural proof.
TRANSLATE_METHOD = "dialplan.translate"

_INTEGER = re.compile(r"^-?\d+$")

#: Scripted/real "no match" string results from the RPC layer.
_UNMATCHED_STRINGS = frozenset(
    {"", "-1", "0", "false", "no match", "no-match", "nomatch", "not found"}
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _integer(value: Any) -> int | None:
    """Coerce an integer field, rejecting bools and non-integral text."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = _text(value).strip()
    if not _INTEGER.fullmatch(text):
        return None
    try:
        return int(text)
    except ValueError:  # pragma: no cover - the regex already guarantees this
        return None


def _registry_values(
    row: Any, descriptor: Any
) -> dict[str, Any]:
    """Unpack one ``tenant_registry`` row, with a documented fallback.

    The normal path uses the descriptor so the packed record layout lives in
    exactly one place.  A corrupted/hand-edited row or a missing tables.yaml
    falls back to the locked positions from plan §5.2.
    """
    if descriptor is not None:
        try:
            return descriptors.unpack_row(descriptor, row)
        except (ValueError, KeyError):
            pass

    parts = _text(row.get("tvalue")).split(",")
    keys = ("tenantid", "ds_onnet", "ds_pstn", "cli", "label", "dpid", "flags")
    values: dict[str, Any] = dict(zip(keys, parts))
    values["tprefix"] = row.get("tprefix")
    return values


def referenced_dpids(conn: sqlite3.Connection | None) -> set[int]:
    """Partitions referenced by any home: its ``dpid`` and ``block_dpid``.

    Both are real references: the registry's ``dpid`` field is the home's
    emergency partition, while the optional third flags segment names the
    blocked-destination partition (``1+1+90``).  A missing/unreadable registry
    yields an empty set, which only affects the advisory badge.
    """
    if conn is None:
        return set()

    try:
        rows = db.read_rows(conn, "tenant_registry")
    except (sqlite3.Error, ValueError):
        return set()

    try:
        descriptor = descriptors.load().tables.get("tenant_registry")
    except (ValueError, OSError):
        descriptor = None

    referenced: set[int] = set()
    for row in rows:
        values = _registry_values(row, descriptor)
        dpid = _integer(values.get("dpid"))
        if dpid is not None:
            referenced.add(dpid)
        try:
            _ih_on, _pstn_on, block_dpid = parse_flags(
                _text(values.get("flags"))
            )
        except ValueError:
            block_dpid = None
        if block_dpid is not None:
            referenced.add(block_dpid)
    return referenced


# ---------------------------------------------------------------------------
# dialplan rules
# ---------------------------------------------------------------------------
def dpid_int(value: Any, context: ValidationContext) -> tuple[list[str], list[str]]:
    """A non-negative integer partition id; warn when no home uses it."""
    number = _integer(value)
    if number is None:
        return (["Enter a whole number for the partition id (0 or greater)."], [])
    if number < 0:
        return (["The partition id must be 0 or greater."], [])

    if context.conn is not None and number not in referenced_dpids(context.conn):
        return (
            [],
            [f"Partition {number} is {PARTITION_UNREFERENCED_WARNING}."],
        )
    return ([], [])


def pr_int(value: Any, context: ValidationContext) -> tuple[list[str], list[str]]:
    """Rule priority: a whole number from 1 to 100."""
    number = _integer(value)
    if number is None or not 1 <= number <= 100:
        return (["Priority must be a whole number between 1 and 100."], [])
    return ([], [])


def match_op_locked(
    value: Any, context: ValidationContext
) -> tuple[list[str], list[str]]:
    """``match_op`` is locked to ``1`` — this console only configures regex match."""
    if _text(value).strip() == "1":
        return ([], [])
    return (
        ["match_op must be 1 — regex matching only in this console."],
        [],
    )


def match_len_zero(
    value: Any, context: ValidationContext
) -> tuple[list[str], list[str]]:
    """``match_len`` is locked to ``0``: the expression alone selects a rule."""
    if _text(value).strip() == "0":
        return ([], [])
    return (
        ["match_len must be 0 — this deployment matches on the regex only."],
        [],
    )


def identity_locked(
    value: Any, context: ValidationContext
) -> tuple[list[str], list[str]]:
    """Enforce the locked identity rewrite pair.

    ``subst_exp`` and ``repl_exp`` are separate descriptor fields that share
    this rule, so the pair is checked from ``context.values``.  When either
    field is not part of the submission (a bare/unit context) the rule is
    silent.  When the context names the field under validation, only that
    field's mismatch is reported; the generic form path reports the invariant
    once per offending field.
    """
    values = context.values or {}
    if "subst_exp" not in values or "repl_exp" not in values:
        return ([], [])

    subst = _text(values.get("subst_exp"))
    repl = _text(values.get("repl_exp"))
    if subst == SUBST_IDENTITY and repl == REPL_IDENTITY:
        return ([], [])

    target = getattr(context.field, "name", None)
    if target not in {"subst_exp", "repl_exp"}:
        current = _text(value)
        if current == subst and current != repl:
            target = "subst_exp"
        elif current == repl and current != subst:
            target = "repl_exp"
        else:
            target = None

    if target == "subst_exp" and subst == SUBST_IDENTITY:
        return ([], [])
    if target == "repl_exp" and repl == REPL_IDENTITY:
        return ([], [])

    return ([IDENTITY_ERROR], [])


def attrs_free_text(
    value: Any, context: ValidationContext
) -> tuple[list[str], list[str]]:
    """``attrs`` is a plain free-text column; reject only NUL and line breaks."""
    text = _text(value)
    errors: list[str] = []
    if "\x00" in text:
        errors.append("Remove the NUL character — this field is stored as text.")
    if "\r" in text or "\n" in text:
        errors.append("Use a single line — line breaks are not stored in this field.")
    return (errors, [])


# ---------------------------------------------------------------------------
# post-reload behavioural proof (not a field validator)
# ---------------------------------------------------------------------------
def _interpret_translate_result(result: Any) -> tuple[bool, str]:
    """Normalise the RPC result into ``(matched, translated_output)``.

    Kamailio's reply shape is not promised by a typed RPC, so the common forms
    are accepted: a translated string, an integer status (positive = success),
    a dict wrapping the output/status, or a one-item list.  ``None``, an empty
    string, ``-1``/``0`` and explicit no-match strings mean "did not match".
    """
    if result is None:
        return (False, "")
    if isinstance(result, bool):
        return (result, "")
    if isinstance(result, str):
        text = result.strip()
        if text.lower() in _UNMATCHED_STRINGS:
            return (False, "")
        return (True, result)
    if isinstance(result, (int, float)):
        if result > 0:
            return (True, str(result))
        return (False, "")
    if isinstance(result, dict):
        matched: bool | None = None
        for key in ("matched", "match", "found", "ok", "success"):
            if key in result:
                matched = bool(result[key])
                break

        output = ""
        for key in ("output", "result", "translated", "new_uri", "uri", "target", "value"):
            candidate = result.get(key)
            if candidate is None or candidate == "":
                continue
            if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
                if candidate <= 0:
                    continue
            output = str(candidate)
            break

        if matched is None:
            matched = output != ""
        return (matched, output)
    if isinstance(result, (list, tuple)):
        for item in reversed(result):
            matched, output = _interpret_translate_result(item)
            if matched:
                return (True, output)
        return (False, "")
    return (bool(result), _text(result))


def translate_proof(
    rpc: Any, dpid: int, sample: str
) -> tuple[bool, str]:
    """Prove that *sample* is translated by partition *dpid* after a reload.

    Parameters are plain JSON-RPC values: ``[int(dpid), str(sample)]``.  The
    kamctl-only ``s:`` prefix is never added, so the edge receives exactly the
    dialed sample.  Transport problems and JSON-RPC faults are returned as a
    ``(False, message)`` pair (message includes Kamailio's verbatim text) so a
    proof failure can be shown in a toast without failing the saved reload.
    """
    try:
        number = int(dpid)
        sample_text = str(sample)
        result = rpc.call(TRANSLATE_METHOD, [number, sample_text])
    except (ConsoleError, TypeError, ValueError) as exc:
        return (False, f"Translation proof failed: {exc}")

    matched, output = _interpret_translate_result(result)
    if matched:
        if output:
            return (
                True,
                f"Matched in partition {number}: {sample_text!r} → {output!r}.",
            )
        return (True, f"Matched in partition {number}: {sample_text!r}.")
    return (
        False,
        f"No dialplan rule in partition {number} matched {sample_text!r}.",
    )


#: Names this module implements; the package merge removes them from
#: :data:`console.validators.PENDING_VALIDATORS`.
RULES: dict[str, Validator] = {
    "dpid_int": dpid_int,
    "pr_int": pr_int,
    "match_op_locked": match_op_locked,
    "match_len_zero": match_len_zero,
    "identity_locked": identity_locked,
    "attrs_free_text": attrs_free_text,
}

PENDING: frozenset[str] = frozenset(RULES)


def _dialplan_references(conn, key) -> list[dict]:
    """Homes whose emergency or block partition still points at *key*.

    ``block_dpid`` lives inside the packed registry ``flags`` field, so the
    generic descriptor cross-ref scan cannot see it.
    """
    from .. import db as db_module, descriptors as descriptors_module
    from ..pack import parse_flags

    try:
        row_id = int(str(key))
    except (TypeError, ValueError):
        return []
    # The delete key is the dialplan row id; references point at its dpid.
    try:
        found_row = conn.execute(
            "SELECT dpid FROM dialplan WHERE id = ?", (row_id,)
        ).fetchone()
        if found_row is None:
            return []
        number = int(found_row[0])
        if number == 0:
            return []
    except Exception:
        return []
    try:
        rows = db_module.read_rows(conn, "tenant_registry")
        registry = descriptors_module.load().tables.get("tenant_registry")
    except Exception:
        return []
    if registry is None:
        return []
    found: list[dict] = []
    for raw in rows:
        try:
            values = descriptors_module.unpack_row(registry, raw)
            _ih, _pstn, block = parse_flags(str(values.get("flags") or ""))
            dpid = int(str(values.get("dpid") or "0"))
        except (KeyError, ValueError):
            continue
        if (dpid and dpid == number) or (block is not None and block == number):
            found.append(
                {
                    "table": "tenant_registry",
                    "key": values.get("tprefix"),
                    "label": str(values.get("tprefix")),
                    "field": "flags" if block == number else "dpid",
                    "value": str(number),
                }
            )
    return found


REFERENCES = {"dialplan": _dialplan_references}
