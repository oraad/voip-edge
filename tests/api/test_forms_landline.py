"""API/form tests for the T4.4 landline prefix interruption flow.

A cross-home overlap returns a dedicated 422 interruption page that names both
rows and never writes.  A same-home overlap is allowed only with the explicit
``allow_same_tenant_shadow`` acknowledgement, which the interruption page
offers as "Save anyway (same home)".
"""
from __future__ import annotations

import html
import sqlite3
from urllib.parse import parse_qs, urlparse

from tests.fixtures import seeddb

BASE = {
    "tprefix": "26419999",
    "tenantid": "T1",
    "target": "700",
    "ds_onnet": "11",
    "ih_on": "1",
}

EXISTING_FULL = "01111111"
EXISTING_TENANT = "T1"


class RecordingWorker:
    def __init__(self):
        self.calls: list[tuple[str, str, list]] = []

    def enqueue(self, table: str, rpc_name: str, params: list) -> None:
        self.calls.append((table, rpc_name, list(params)))


def _squash(text: str) -> str:
    return " ".join(html.unescape(text).split())


def _row(path, key: str):
    conn = sqlite3.connect(path)
    try:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM tenant_landline WHERE tprefix = ?", (key,)
        ).fetchone()
    finally:
        conn.close()


def test_new_landline_form_renders_all_descriptor_fields(seeded_db, auth_client):
    response = auth_client.get("/tables/tenant_landline/new")
    assert response.status_code == 200
    page = html.unescape(response.text)
    for name in ("tprefix", "tenantid", "target", "ds_onnet", "ih_on"):
        assert f'id="field-{name}"' in response.text, name
    assert "longer prefixes win" in page
    assert "Taken from the owning home" in page


def test_landline_create_happy_path_writes_row_and_enqueues_one_reload(
    seeded_db, auth_client, app
):
    worker = RecordingWorker()
    app.state.worker = worker

    response = auth_client.post("/tables/tenant_landline/new", data=BASE)

    assert response.status_code == 303, response.text
    location = response.headers["location"]
    assert location.startswith("/tables/tenant_landline?")
    flash = parse_qs(urlparse(location).query)["flash"][0]
    assert "Saved" in flash
    assert "reload scheduled shortly" in flash

    row = _row(seeded_db, BASE["tprefix"])
    assert row is not None
    assert row["tvalue"] == "T1,700,11,1"
    assert worker.calls == [("tenant_landline", "mtree.reload", ["landline"])]


def test_cross_tenant_shadow_returns_interruption_page_naming_both_rows(
    seeded_db, auth_client, app
):
    worker = RecordingWorker()
    app.state.worker = worker
    payload = dict(BASE, tprefix="2641", tenantid="T2", ds_onnet="12")

    response = auth_client.post("/tables/tenant_landline/new", data=payload)

    assert response.status_code == 422, response.text
    page = _squash(response.text)
    assert 'id="interruption"' in response.text
    assert EXISTING_FULL in page
    assert "2641" in page
    assert EXISTING_TENANT in page
    assert "T2" in page
    assert "longest matching prefix wins" in page.lower()
    assert "cross-home" in page.lower() or "different home" in page.lower()
    # The same-home escape hatch is deliberately absent for a cross-home clash.
    assert "Save anyway (same home)" not in page
    assert 'name="allow_same_tenant_shadow"' not in response.text

    assert _row(seeded_db, payload["tprefix"]) is None
    assert worker.calls == []


def test_same_tenant_shadow_is_blocked_without_the_checkbox(
    seeded_db, auth_client, app
):
    worker = RecordingWorker()
    app.state.worker = worker
    payload = dict(BASE, tprefix="2641")

    response = auth_client.post("/tables/tenant_landline/new", data=payload)

    assert response.status_code == 422, response.text
    page = _squash(response.text)
    assert 'id="interruption"' in response.text
    assert "Save anyway (same home)" in page
    assert 'name="allow_same_tenant_shadow"' in response.text
    assert "allow_same_tenant_shadow" in page
    assert _row(seeded_db, payload["tprefix"]) is None
    assert worker.calls == []


def test_same_tenant_shadow_saves_with_the_checkbox(seeded_db, auth_client, app):
    worker = RecordingWorker()
    app.state.worker = worker
    payload = dict(BASE, tprefix="2641", allow_same_tenant_shadow="1")

    response = auth_client.post("/tables/tenant_landline/new", data=payload)

    assert response.status_code == 303, response.text
    row = _row(seeded_db, payload["tprefix"])
    assert row is not None
    assert row["tvalue"] == "T1,700,11,1"
    assert worker.calls == [("tenant_landline", "mtree.reload", ["landline"])]


def test_same_tenant_longer_number_also_needs_the_checkbox(
    seeded_db, auth_client, app
):
    worker = RecordingWorker()
    app.state.worker = worker
    payload = dict(BASE, tprefix=EXISTING_FULL + "9")

    blocked = auth_client.post("/tables/tenant_landline/new", data=payload)
    assert blocked.status_code == 422
    assert _row(seeded_db, payload["tprefix"]) is None

    allowed = auth_client.post(
        "/tables/tenant_landline/new",
        data=dict(payload, allow_same_tenant_shadow="1"),
    )
    assert allowed.status_code == 303, allowed.text
    assert _row(seeded_db, payload["tprefix"]) is not None
    assert worker.calls == [("tenant_landline", "mtree.reload", ["landline"])]


# ---------------------------------------------------------------------------
# flash tone rendering on the generic list page
# ---------------------------------------------------------------------------
def test_list_page_renders_flash_tones_as_status_toasts(seeded_db, auth_client):
    for tone in ("scheduled", "applied", "verified", "failed"):
        response = auth_client.get(
            "/tables/tenant_landline",
            params={"flash": f"Saved {BASE['tprefix']} — {tone}", "tone": tone},
        )
        assert response.status_code == 200
        page = _squash(response.text)
        assert 'role="status"' in response.text
        assert f"Saved {BASE['tprefix']}" in page
        assert f'data-tone="{tone}"' in response.text


def test_list_page_without_flash_has_no_toast(seeded_db, auth_client):
    response = auth_client.get("/tables/tenant_landline")
    assert response.status_code == 200
    assert 'class="toast' not in response.text
