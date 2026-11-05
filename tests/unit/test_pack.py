"""Golden vectors for the packed mtree columns (plan §5.1/§5.2, task T0).

The literal strings here come from the real seed (kamailio-edge/kamailio/db/seed.sql)
so a regression in separator handling is visible without a database.
"""
from __future__ import annotations

import pytest

from console.pack import pack_fields, pack_flags, parse_flags, unpack_fields

GOLDEN_TVALUE = "T1,11,31,01111111,01111111,10,1+1+90"
GOLDEN_FIELDS = ["T1", "11", "31", "01111111", "01111111", "10", "1+1+90"]
# pack_fields is the free-text codec: the flags field is structured ('+' is its
# own separator) and is serialized by descriptors.pack_row, not through
# pack_fields.  So the comma-joined golden prefix excludes the last segment.
GOLDEN_PREFIX = "T1,11,31,01111111,01111111,10"
GOLDEN_FREE_TEXT = GOLDEN_FIELDS[:-1]

# Absent block_dpid is legal: "0 or omitted = nothing blocked".
GOLDEN_TVALUE_NO_BLOCK = "T1,11,31,01111111,01111111,10,1+1"
GOLDEN_FIELDS_NO_BLOCK = ["T1", "11", "31", "01111111", "01111111", "10", "1+1"]


def test_pack_fields_golden_vector() -> None:
    assert pack_fields(GOLDEN_FREE_TEXT) == GOLDEN_PREFIX


def test_unpack_fields_golden_vector() -> None:
    assert unpack_fields(GOLDEN_TVALUE, expected=7) == GOLDEN_FIELDS


def test_golden_vector_round_trip() -> None:
    fields = unpack_fields(GOLDEN_TVALUE, expected=7)
    assert pack_fields(fields[:-1]) == GOLDEN_PREFIX
    assert parse_flags(fields[-1]) == (True, True, 90)


def test_unpack_fields_absent_block_round_trip() -> None:
    fields = unpack_fields(GOLDEN_TVALUE_NO_BLOCK, expected=7)
    assert fields == GOLDEN_FIELDS_NO_BLOCK
    assert pack_fields(fields[:-1]) == GOLDEN_PREFIX
    assert parse_flags(fields[-1]) == (True, True, None)


def test_unpack_fields_count_mismatch_is_an_error() -> None:
    with pytest.raises(ValueError):
        unpack_fields(GOLDEN_TVALUE, expected=6)


def test_pack_fields_rejects_comma_in_value() -> None:
    with pytest.raises(ValueError):
        pack_fields(["T1", "a,b"])


def test_pack_fields_rejects_plus_in_value() -> None:
    with pytest.raises(ValueError):
        pack_fields(["T1", "1+1"])


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ((True, True, 90), "1+1+90"),
        ((True, True), "1+1"),
        ((True, False, 90), "1+0+90"),
        ((False, True), "0+1"),
        ((False, False), "0+0"),
        ((True, True, 0), "1+1+0"),
    ],
)
def test_pack_flags(args: tuple, expected: str) -> None:
    assert pack_flags(*args) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1+1+90", (True, True, 90)),
        ("1+1", (True, True, None)),
        ("1+0+90", (True, False, 90)),
        ("0+0", (False, False, None)),
        ("0+1", (False, True, None)),
        ("1+1+0", (True, True, None)),
        ("", (False, False, None)),
        (" 1+1+90 ", (True, True, 90)),
    ],
)
def test_parse_flags(raw: str, expected: tuple) -> None:
    assert parse_flags(raw) == expected


@pytest.mark.parametrize(
    "args",
    [(True, True, 90), (True, True, None), (False, True, 0), (False, False, None)],
)
def test_flags_round_trip(args: tuple) -> None:
    raw = pack_flags(*args)
    ih_on, pstn_on, block_dpid = parse_flags(raw)
    assert (ih_on, pstn_on, block_dpid) == (
        bool(args[0]),
        bool(args[1]),
        args[2] if len(args) > 2 and args[2] else None,
    )


@pytest.mark.parametrize("raw", ["1+1+90+99", "2+1", "1+1+x", "1+-1"])
def test_parse_flags_rejects_malformed(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_flags(raw)
