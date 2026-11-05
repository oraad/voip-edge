"""API tests for the generic read-only table browser (T3)."""
from __future__ import annotations

import html
from pathlib import Path

import pytest

from console import tables_ui
from tests.fixtures import seeddb

# Each table's identifying values as they appear in the seeded fixture.
TABLE_SAMPLES = {
    "tenant_registry": [
        "sip.alpha.home.example.net",
        "sip.beta.home.example.net",
        "sip.gamma.home.example.net",
        "01111111",
        "T1",
    ],
    "tenant_landline": ["01111111", "02222222", "03333333", "T1", "home"],
    "dispatcher": [
        "sip:10.0.0.10:5060;transport=tcp",
        "sip:10.0.99.12:5060;transport=tcp",
        "alpha voip-stack",
    ],
    "dialplan": ["^(112|911|999)$", "^0900[0-9]{6}$", "emergency Example"],
    "trusted": [
        "100.64.10.10",
        "100.64.10.12",
        "100.64.10.14",
        "sip.alpha.home.example.net",
    ],
}


def _full_page(text: str) -> bool:
    lowered = text.lower()
    return "<!doctype" in lowered and "<html" in lowered


def _fragment_only(text: str) -> bool:
    lowered = text.lower()
    return not any(marker in lowered for marker in ("<!doctype", "<html", "<body"))


# ---------------------------------------------------------------------------
# list rendering: every descriptor table, zero per-table code
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("table", list(TABLE_SAMPLES))
def test_list_renders_seeded_rows_with_key_columns(seeded_db, auth_client, table):
    response = auth_client.get(f"/tables/{table}")
    assert response.status_code == 200
    page = html.unescape(response.text)
    for sample in TABLE_SAMPLES[table]:
        assert sample in page, sample
    assert "<thead>" in response.text
    assert "<tbody>" in response.text


def test_router_contains_no_table_specific_code():
    source = Path(tables_ui.__file__).read_text(encoding="utf-8")
    for table in seeddb.TABLE_NAMES:
        assert table not in source
        assert f'"{table}"' not in source


def test_unknown_table_is_404(seeded_db, auth_client):
    response = auth_client.get("/tables/not_a_table")
    assert response.status_code == 404
    assert "No table named" in response.text
    assert _full_page(response.text)


def test_unknown_key_is_404(seeded_db, auth_client):
    response = auth_client.get("/tables/tenant_registry/sip.nope.example")
    assert response.status_code == 404

    response = auth_client.get("/tables/dispatcher/99999")
    assert response.status_code == 404


def test_detail_lists_descriptor_fields_and_help(seeded_db, auth_client):
    domain = "sip.alpha.home.example.net"
    response = auth_client.get(f"/tables/tenant_registry/{domain}")
    assert response.status_code == 200
    page = html.unescape(response.text)
    assert domain in page
    assert "01111111" in page
    # Labels + help come from tables.yaml, not from route code.
    assert "Tprefix" in page
    assert "Home SIP domain" in page
    assert "create-only" in page
    assert "Read-only" in page
    # No edit form yet (T4 owns writes).
    assert "<form" not in response.text


# ---------------------------------------------------------------------------
# ?q= filter
# ---------------------------------------------------------------------------
def test_query_filter_is_case_insensitive(seeded_db, auth_client):
    response = auth_client.get("/tables/tenant_registry", params={"q": "ALPHA"})
    assert response.status_code == 200
    page = html.unescape(response.text)
    assert "sip.alpha.home.example.net" in page
    assert "sip.beta.home.example.net" not in page


def test_query_filter_searches_declared_list_fields(seeded_db, auth_client):
    # tenantid is a displayed field but not part of the domain/key.
    response = auth_client.get("/tables/tenant_registry", params={"q": "t2"})
    assert response.status_code == 200
    page = html.unescape(response.text)
    assert "sip.beta.home.example.net" in page
    assert "sip.alpha.home.example.net" not in page


def test_filtered_empty_state_has_clear_filter(seeded_db, auth_client):
    response = auth_client.get("/tables/tenant_registry", params={"q": "zzz-no-match"})
    assert response.status_code == 200
    assert "No results match" in response.text
    assert "zzz-no-match" in response.text
    assert "Clear filter" in response.text
    assert 'href="/tables/tenant_registry"' in response.text
    assert "No rows in" not in response.text


# ---------------------------------------------------------------------------
# empty database (first use)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("table", list(TABLE_SAMPLES))
def test_empty_database_shows_first_use_cta(empty_db, auth_client, table):
    response = auth_client.get(f"/tables/{table}")
    assert response.status_code == 200
    assert "No rows in" in response.text
    assert "+ Add the first row" in response.text
    assert "Clear filter" not in response.text


def test_empty_database_with_filter_still_shows_first_use(empty_db, auth_client):
    response = auth_client.get("/tables/tenant_registry", params={"q": "anything"})
    assert response.status_code == 200
    assert "No rows in" in response.text
    assert "No results match" not in response.text


# ---------------------------------------------------------------------------
# auth + fragment safety
# ---------------------------------------------------------------------------
def test_tables_require_login(client):
    for path in ("/tables/tenant_registry", "/tables/dispatcher/1"):
        response = client.get(path)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"


@pytest.mark.parametrize(
    "path",
    ["/tables/tenant_registry", "/tables/tenant_registry/sip.alpha.home.example.net"],
)
def test_full_page_and_fragment_modes(seeded_db, auth_client, path):
    full = auth_client.get(path)
    assert full.status_code == 200
    assert _full_page(full.text)

    fragment = auth_client.get(path, headers={"HX-Request": "true"})
    assert fragment.status_code == 200
    assert _fragment_only(fragment.text)
    assert "X-CSRF-Token" not in fragment.text

    # Content is the same in both modes.
    assert "sip.alpha.home.example.net" in html.unescape(fragment.text)
