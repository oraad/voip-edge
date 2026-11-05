"""API tests for the Homes landing page (T3)."""
from __future__ import annotations

import html


def _full_page(text: str) -> bool:
    lowered = text.lower()
    return "<!doctype" in lowered and "<html" in lowered


def _fragment_only(text: str) -> bool:
    lowered = text.lower()
    return not any(marker in lowered for marker in ("<!doctype", "<html", "<body"))


def test_homes_lists_registry_rows_with_placeholder_coherence(seeded_db, auth_client):
    response = auth_client.get("/homes")
    assert response.status_code == 200
    page = html.unescape(response.text)

    for domain in (
        "sip.alpha.home.example.net",
        "sip.beta.home.example.net",
        "sip.gamma.home.example.net",
    ):
        assert domain in page
    for landline in ("01111111", "02222222", "03333333"):
        assert landline in page

    # Route-set ids come from the unpacked registry row.
    assert "11 on-net" in page
    assert "31 PSTN" in page

    # Coherence is a placeholder until the live check lands (T6).
    assert "Coherence" in response.text
    assert "—" in page
    # Presence in tenant_registry is the status signal.
    assert "Active" in page
    assert page.count("Active") >= 3

    # The list is a real semantic table.
    assert "<thead>" in response.text
    assert "<tbody>" in response.text
    assert "Domain" in response.text
    assert "Landline" in response.text
    assert "No homes yet" not in page


def test_homes_empty_database_shows_onboard_cta(empty_db, auth_client):
    response = auth_client.get("/homes")
    assert response.status_code == 200
    page = html.unescape(response.text)
    assert "No homes yet" in page
    assert "+ Onboard a home" in page
    # Keep the table shell so the page is still a real table (and crawl-safe).
    assert "<thead>" in response.text
    assert "<tbody>" in response.text
    assert "Active" not in page


def test_homes_requires_login(client):
    response = client.get("/homes")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_homes_full_page_and_fragment_modes(seeded_db, auth_client):
    full = auth_client.get("/homes")
    assert full.status_code == 200
    assert _full_page(full.text)

    fragment = auth_client.get("/homes", headers={"HX-Request": "true"})
    assert fragment.status_code == 200
    assert _fragment_only(fragment.text)
    assert "sip.alpha.home.example.net" in fragment.text
    assert "X-CSRF-Token" not in fragment.text
    assert "Audit &amp; backups" not in fragment.text
