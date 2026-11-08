"""API tests for the simplified Number rules page."""
from __future__ import annotations

import html
import re


def _text(page: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html.unescape(page)).split())


def test_number_rules_has_three_tabs_and_advanced_link(seeded_db, auth_client):
    page = auth_client.get("/number-rules")
    text = _text(page.text)
    assert page.status_code == 200
    assert "Emergency" in text
    assert "Blocked" in text
    assert "Custom" in text
    assert 'href="/tables/dialplan"' in page.text
    assert 'href="/number-rules?tab=blocked"' in page.text


def test_number_rules_labels_referenced_partitions(seeded_db, auth_client):
    page = auth_client.get("/number-rules")
    text = _text(page.text)
    assert "Partition 10" in text
    assert "Emergency" in text
    assert "sip.alpha.home.example.net" in text
    assert "Partition 90" in text
    assert "Blocked" in text


def test_number_rules_requires_login(client):
    response = client.get("/number-rules")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
