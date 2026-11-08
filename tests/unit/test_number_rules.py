"""Unit tests for the simplified number-rule pattern builders."""
from __future__ import annotations

import re

import pytest

from console import number_rules


def test_parse_entries_deduplicates_and_accepts_mixed_separators():
    assert number_rules.parse_entries("112, 911\n999 112") == [
        "112",
        "911",
        "999",
    ]


def test_parse_entries_rejects_non_digits():
    with pytest.raises(ValueError):
        number_rules.parse_entries("112\n9-1-1")


def test_exact_pattern_deduplicates_and_anchors():
    assert number_rules.exact_pattern(["112", "911", "112"]) == "^(112|911)$"
    assert number_rules.exact_pattern(["112"]) == "^112$"


def test_prefix_pattern_does_not_anchor_end():
    assert number_rules.prefix_pattern(["0900", "0901"]) == "^(0900|0901)"


@pytest.mark.parametrize(
    ("start", "end"),
    [("1234", "1245"), ("0900", "0909"), ("90000000", "90009999")],
)
def test_range_pattern_matches_only_inside_range(start, end):
    pattern = number_rules.range_pattern(start, end)
    assert re.fullmatch(pattern, start)
    assert re.fullmatch(pattern, end)
    assert not re.fullmatch(pattern, str(int(start) - 1).zfill(len(start)))
    assert not re.fullmatch(pattern, str(int(end) + 1).zfill(len(end)))


def test_range_pattern_covers_every_value_in_a_small_range():
    pattern = number_rules.range_pattern("0955", "0965")
    for number in range(955, 966):
        assert re.fullmatch(pattern, f"0{number}")


def test_range_pattern_rejects_bad_bounds():
    with pytest.raises(ValueError):
        number_rules.range_pattern("1245", "1234")
    with pytest.raises(ValueError):
        number_rules.range_pattern("12", "123")
    with pytest.raises(ValueError):
        number_rules.range_pattern("", "12")
