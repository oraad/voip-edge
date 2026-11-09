"""Static guards for the shared stylesheet."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _css() -> str:
    return (ROOT / "web" / "static" / "app.css").read_text(encoding="utf-8")


def test_app_css_braces_balance():
    css = _css()
    assert css.count("{") == css.count("}")


def test_stats_rule_keeps_its_selector():
    css = _css()
    assert ".stats {\n  display: grid;" in css


def test_danger_links_use_the_danger_color():
    assert "a.danger" in _css()


def test_console_shell_has_independent_scroll_regions():
    css = _css()
    assert "grid-template-rows: auto minmax(0, 1fr);" in css
    assert "height: 100vh;" in css
    assert "overflow-y: auto;" in css
