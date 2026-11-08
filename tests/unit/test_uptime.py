"""Uptime formatting for Kamailio ``core.uptime`` replies."""
from __future__ import annotations

import pytest

from console.uptime import format_uptime, uptime_seconds


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "0s"),
        (23, "23s"),
        (63, "1m 3s"),
        (7682, "2h 8m 2s"),
        (7823, "2h 10m 23s"),
        (90_061, "1d 1h 1m"),
    ],
)
def test_format_uptime_seconds(value, expected):
    assert format_uptime(value) == expected


def test_format_uptime_unwraps_kamailio_dict():
    assert format_uptime({"uptime": 7823}) == "2h 10m 23s"
    assert format_uptime({"UP_TIME": "63"}) == "1m 3s"


def test_format_uptime_keeps_legacy_text():
    assert format_uptime("6 days, 3 hours") == "6 days, 3 hours"
    assert format_uptime(None) is None


def test_uptime_seconds_accepts_numeric_strings():
    assert uptime_seconds("7682") == 7682
    assert uptime_seconds("7682 s") == 7682
    assert uptime_seconds("not a number") is None
