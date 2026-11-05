"""T2 route crawl: full page vs htmx fragment safety (plan §7.20)."""
from __future__ import annotations

import html

import pytest

NAV_LABELS = [
    "Homes",
    "Live",
    "Routing data",
    "Landlines",
    "Route sets",
    "Number rules",
    "Trusted sources",
    "System",
    "Audit & backups",
]

EMPTY_STATE_STRINGS = ["No homes yet", "Onboard a home"]


def _looks_full_page(text: str) -> bool:
    lowered = text.lower()
    return "<!doctype" in lowered and "<html" in lowered


def _is_fragment(text: str) -> bool:
    lowered = text.lower()
    return not any(marker in lowered for marker in ("<!doctype", "<html", "<body"))


def test_login_page_is_full_and_accessible(client):
    response = client.get("/login")
    assert response.status_code == 200
    assert _looks_full_page(response.text)
    assert 'for="token"' in response.text
    assert 'type="password"' in response.text
    assert 'autocomplete="current-password"' in response.text


def test_homes_full_page_has_nav_empty_state_and_table(auth_client):
    response = auth_client.get("/homes")
    assert response.status_code == 200
    assert _looks_full_page(response.text)

    for label in NAV_LABELS:
        assert label in html.unescape(response.text), label
    assert 'aria-current="page"' in response.text
    for label in EMPTY_STATE_STRINGS:
        assert label in response.text, label

    # Data table shell is semantic, not div soup.
    assert "<thead>" in response.text
    assert "<tbody>" in response.text
    assert "Domain" in response.text
    assert "Landline" in response.text

    # Status strip placeholders (T6 wires the real values).
    assert "edge health" in response.text
    assert "not wired yet" in response.text
    assert "pending reloads" in response.text
    assert "read-only" in response.text

    # The CSRF value is exposed once on <body> for htmx; never inside swaps.
    assert "X-CSRF-Token" in response.text
    assert "<body" in response.text


def test_homes_with_hx_request_is_fragment_only(auth_client):
    response = auth_client.get("/homes", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert _is_fragment(response.text)

    # The fragment is the page content: no nav chrome, but the real content.
    for label in EMPTY_STATE_STRINGS:
        assert label in response.text, label
    assert "<table>" in response.text
    assert "Audit &amp; backups" not in response.text
    assert "X-CSRF-Token" not in response.text


def test_hx_header_false_still_returns_full_page(auth_client):
    response = auth_client.get("/homes", headers={"HX-Request": "false"})
    assert response.status_code == 200
    assert _looks_full_page(response.text)


@pytest.mark.parametrize("path", ["/login", "/homes"])
def test_unauthenticated_get_redirects_are_not_html(client, path):
    response = client.get(path)
    if path == "/homes":
        assert response.status_code == 303
        assert response.headers["location"] == "/login"
    else:
        assert response.status_code == 200
