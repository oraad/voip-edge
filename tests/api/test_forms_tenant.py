"""API tests for the generic create/edit forms (T4.1, plan §5.1/§5.2/§6).

The tenant family proves the packed happy path, the error re-render and key
immutability; the dispatcher family proves the *same* router works for a
columns table and for the warnings-save path.  No test reaches into per-table
branches because the router has none.
"""
from __future__ import annotations

import html
import re
import sqlite3
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from console import forms_ui
from tests.fixtures import seeddb

VALID_TENANT = {
    "tprefix": "sip.delta.home.example.net",
    "tenantid": "T4",
    "ds_onnet": "11",
    "ds_pstn": "31",
    "cli": "05555555",
    "label": "05555555",
    "dpid": "10",
    "flags": "1+1+90",
}

VALID_DISPATCHER = {
    "setid": "14",
    "destination": "sip:10.0.0.10:5060;transport=udp",
    "flags": "0",
    "priority": "0",
    "attrs": "transport=udp",
    "description": "beta voip-stack",
}

TENANT_FIELDS = [
    "tprefix",
    "tenantid",
    "ds_onnet",
    "ds_pstn",
    "cli",
    "label",
    "dpid",
    "flags",
]


class RecordingWorker:
    """Minimal ReloadWorker double: records the exact enqueue contract."""

    def __init__(self):
        self.calls: list[tuple[str, str, list]] = []
        self.errors: dict[str, BaseException] = {}

    def enqueue(self, table: str, rpc_name: str, params: list) -> None:
        self.calls.append((table, rpc_name, list(params)))


def _full_page(text: str) -> bool:
    lowered = text.lower()
    return "<!doctype" in lowered and "<html" in lowered


def _fragment_only(text: str) -> bool:
    lowered = text.lower()
    return not any(marker in lowered for marker in ("<!doctype", "<html", "<body"))


def _squash(text: str) -> str:
    return " ".join(text.split())


def _row(path, table: str, key: str):
    conn = sqlite3.connect(path)
    try:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            f'SELECT * FROM "{table}" WHERE tprefix = ?', (key,)
        ).fetchone()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# rendering: every descriptor field, never the auto id
# ---------------------------------------------------------------------------
def test_new_tenant_form_renders_all_descriptor_fields_with_help(seeded_db, auth_client):
    response = auth_client.get("/tables/tenant_registry/new")
    assert response.status_code == 200
    assert _full_page(response.text)
    page = html.unescape(response.text)

    for name in TENANT_FIELDS:
        assert f'id="field-{name}"' in response.text, name
        assert f'name="{name}"' in response.text, name

    # Help text comes from tables.yaml, not from the router.
    assert "Home SIP domain" in page
    assert "no commas or plus signs" in page
    assert "stored as 1+1+90" in page
    # Create mode does not disable the packed key.
    assert 'id="field-tprefix"' in response.text
    assert not re.search(r'id="field-tprefix"[^>]*\bdisabled\b', response.text)


def test_new_dispatcher_form_renders_columns_and_not_id(seeded_db, auth_client):
    response = auth_client.get("/tables/dispatcher/new")
    assert response.status_code == 200
    page = html.unescape(response.text)

    for name in ("setid", "destination", "flags", "priority", "attrs", "description"):
        assert f'name="{name}"' in response.text, name
    assert 'name="id"' not in response.text
    assert "sip:<literal IPv4>:<port>;transport=tcp" in page


def test_new_form_fragment_is_fragment_only(seeded_db, auth_client):
    response = auth_client.get(
        "/tables/tenant_registry/new", headers={"HX-Request": "true"}
    )
    assert response.status_code == 200
    assert _fragment_only(response.text)
    assert 'id="form-shell"' in response.text
    assert 'name="flags"' in response.text


# ---------------------------------------------------------------------------
# happy path: packed write + exactly one reload intent
# ---------------------------------------------------------------------------
def test_post_tenant_happy_path_writes_row_and_enqueues_one_reload(
    seeded_db, auth_client, app
):
    worker = RecordingWorker()
    app.state.worker = worker

    response = auth_client.post("/tables/tenant_registry/new", data=VALID_TENANT)

    assert response.status_code == 303, response.text
    location = response.headers["location"]
    assert location.startswith("/tables/tenant_registry?")
    flash = parse_qs(urlparse(location).query)["flash"][0]
    assert "Saved" in flash
    assert "reload scheduled shortly" in flash

    row = _row(seeded_db, "tenant_registry", VALID_TENANT["tprefix"])
    assert row is not None
    assert row["tvalue"] == "T4,11,31,05555555,05555555,10,1+1+90"

    assert worker.calls == [
        ("tenant_registry", "mtree.reload", ["tenantdom"])
    ]


def test_post_tenant_happy_path_hx_returns_toast(seeded_db, auth_client, app):
    app.state.worker = RecordingWorker()
    response = auth_client.post(
        "/tables/tenant_registry/new",
        data=VALID_TENANT,
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    assert _fragment_only(response.text)
    text = _squash(html.unescape(response.text))
    assert "Saved" in text
    assert "reload scheduled shortly" in text
    assert "verified" not in text.lower()
    assert 'role="status"' in response.text


# ---------------------------------------------------------------------------
# validation error path: 422, summary, inline error, values preserved
# ---------------------------------------------------------------------------
def test_invalid_label_comma_rerenders_422_with_summary_and_values(
    seeded_db, auth_client, app
):
    worker = RecordingWorker()
    app.state.worker = worker
    payload = dict(VALID_TENANT, label="Delta, Example City")

    response = auth_client.post("/tables/tenant_registry/new", data=payload)

    assert response.status_code == 422
    page = _squash(html.unescape(response.text))

    # Focused summary with a link to the offending field.
    assert 'class="errsummary"' in response.text
    assert 'tabindex="-1"' in response.text
    assert "1 thing needs fixing before this can save" in page
    assert 'href="#field-label"' in response.text
    assert "packing separator" in page

    # Inline, accessible field error.
    assert 'aria-invalid="true"' in response.text
    assert 'aria-describedby="help-label err-label-1"' in response.text
    assert 'id="err-label-1"' in response.text
    assert "Remove the comma" in page

    # Values preserved on the whole row, not just the bad field.
    assert 'value="Delta, Example City"' in response.text
    assert 'value="sip.delta.home.example.net"' in response.text
    assert 'value="05555555"' in response.text

    # Nothing was written and no reload was scheduled.
    assert _row(seeded_db, "tenant_registry", VALID_TENANT["tprefix"]) is None
    assert worker.calls == []


def test_missing_cross_ref_is_an_inline_error(seeded_db, auth_client, app):
    app.state.worker = RecordingWorker()
    payload = dict(VALID_TENANT, ds_onnet="999")
    response = auth_client.post("/tables/tenant_registry/new", data=payload)
    assert response.status_code == 422
    page = _squash(html.unescape(response.text))
    assert "Choose an existing" in page
    assert "999" in page
    assert 'href="#field-ds_onnet"' in response.text


# ---------------------------------------------------------------------------
# warnings do not block a save (plan §5.2/§6 severity model)
# ---------------------------------------------------------------------------
def test_warning_saves_and_is_shown_in_the_toast(seeded_db, auth_client, app):
    worker = RecordingWorker()
    app.state.worker = worker

    response = auth_client.post(
        "/tables/dispatcher/new",
        data=VALID_DISPATCHER,
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200, response.text
    assert _fragment_only(response.text)
    page = _squash(html.unescape(response.text))
    assert "Saved" in page
    assert "Warning" in page
    assert "OPTIONS pings stay green" in page
    assert "verified" not in page.lower()

    conn = sqlite3.connect(seeded_db)
    try:
        row = conn.execute(
            'SELECT * FROM "dispatcher" WHERE setid = 14'
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert worker.calls == [("dispatcher", "dispatcher.reload", [])]


# ---------------------------------------------------------------------------
# key immutability on edit (create-only)
# ---------------------------------------------------------------------------
def test_edit_page_disables_the_packed_key_with_a_create_only_hint(
    seeded_db, auth_client
):
    domain = "sip.alpha.home.example.net"
    response = auth_client.get(f"/tables/tenant_registry/{domain}/edit")
    assert response.status_code == 200

    match = re.search(r'<input id="field-tprefix"[^>]*>', response.text)
    assert match is not None
    assert "disabled" in match.group(0)
    assert f'value="{domain}"' in match.group(0)
    assert "Create-only" in response.text
    assert "cannot be changed" in response.text


def test_edit_post_cannot_rename_a_packed_key(seeded_db, auth_client, app):
    worker = RecordingWorker()
    app.state.worker = worker
    domain = "sip.alpha.home.example.net"
    payload = dict(VALID_TENANT, tprefix="sip.renamed.home.example.net")

    response = auth_client.post(
        f"/tables/tenant_registry/{domain}/edit", data=payload
    )
    assert response.status_code == 303, response.text

    conn = sqlite3.connect(seeded_db)
    try:
        renamed = conn.execute(
            'SELECT 1 FROM "tenant_registry" WHERE tprefix = ?',
            ("sip.renamed.home.example.net",),
        ).fetchone()
        original = conn.execute(
            'SELECT tvalue FROM "tenant_registry" WHERE tprefix = ?', (domain,)
        ).fetchone()
    finally:
        conn.close()
    assert renamed is None
    assert original is not None
    assert worker.calls == [("tenant_registry", "mtree.reload", ["tenantdom"])]


# ---------------------------------------------------------------------------
# auth + CSRF
# ---------------------------------------------------------------------------
def test_forms_require_login(client):
    for path in (
        "/tables/tenant_registry/new",
        "/tables/tenant_registry/sip.alpha.home.example.net/edit",
    ):
        response = client.get(path)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"


def test_post_without_csrf_header_is_403(seeded_db, auth_client):
    auth_client.headers.pop("X-CSRF-Token", None)
    response = auth_client.post("/tables/tenant_registry/new", data=VALID_TENANT)
    assert response.status_code == 403
    assert _row(seeded_db, "tenant_registry", VALID_TENANT["tprefix"]) is None


# ---------------------------------------------------------------------------
# generic-first guard
# ---------------------------------------------------------------------------
def test_forms_router_contains_no_table_specific_code():
    source = Path(forms_ui.__file__).read_text(encoding="utf-8")
    for table in seeddb.TABLE_NAMES:
        assert table not in source, table
