"""T4.8 stale-edit tests: content-hash 409 banner and force resubmit.

The edit form carries a hidden ``row_hash`` computed from the row as loaded.
If another tab (or the CLI/Kamailio) changes the row before the POST, the
server recomputes the hash and returns a 409 banner instead of overwriting.
"Keep my changes" resubmits with ``force=1``; "Load latest" simply reopens the
edit form.
"""
from __future__ import annotations

import html
import re
import sqlite3

from tests.fixtures import seeddb

LANDLINE_KEY = "01111111"
LANDLINE_TENANT = "T1"


class RecordingWorker:
    def __init__(self):
        self.calls: list[tuple[str, str, list]] = []

    def enqueue(self, table: str, rpc_name: str, params: list) -> None:
        self.calls.append((table, rpc_name, list(params)))


def _squash(text: str) -> str:
    return " ".join(html.unescape(text).split())


def _row_hash(page: str) -> str:
    match = re.search(r'name="row_hash"\s+value="([0-9a-f]{64})"', page)
    assert match is not None, "edit form did not render a row_hash"
    return match.group(1)


def _set_target(path, target: str) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "UPDATE tenant_landline SET tvalue = ? WHERE tprefix = ?",
            (f"{LANDLINE_TENANT},{target},11,1", LANDLINE_KEY),
        )
        conn.commit()
    finally:
        conn.close()


def _current_target(path) -> str:
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT tvalue FROM tenant_landline WHERE tprefix = ?", (LANDLINE_KEY,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return row[0].split(",")[1]


def _payload(row_hash: str, target: str = "home") -> dict:
    return {
        "tprefix": LANDLINE_KEY,
        "tenantid": LANDLINE_TENANT,
        "target": target,
        "ds_onnet": "11",
        "ih_on": "1",
        "row_hash": row_hash,
    }


def test_edit_form_renders_hidden_row_hash(seeded_db, auth_client):
    response = auth_client.get(f"/tables/tenant_landline/{LANDLINE_KEY}/edit")
    assert response.status_code == 200
    hidden = re.search(r'<input type="hidden" name="row_hash" value="([0-9a-f]{64})"', response.text)
    assert hidden is not None


def test_stale_edit_returns_409_with_diff_and_preserved_values(
    seeded_db, auth_client, app
):
    worker = RecordingWorker()
    app.state.worker = worker

    loaded = auth_client.get(f"/tables/tenant_landline/{LANDLINE_KEY}/edit")
    old_hash = _row_hash(loaded.text)

    # Another writer changes the same row behind the operator's back.
    _set_target(seeded_db, "999")

    response = auth_client.post(
        f"/tables/tenant_landline/{LANDLINE_KEY}/edit",
        data=_payload(old_hash, target="home"),
    )

    assert response.status_code == 409, response.text
    page = _squash(response.text)
    assert 'id="conflict"' in response.text
    assert "changed" in page.lower()
    # Current and submitted values both survive on the banner.
    assert "999" in page
    assert "home" in page
    assert 'name="target" value="home"' in response.text
    # Small field diff: only the changed field is listed.
    assert "Ring group / extension" in page
    assert "submitted" in page.lower()
    assert "current" in page.lower()
    # Two explicit actions.
    assert "Keep my changes" in page
    assert "Load latest" in page
    assert f'href="/tables/tenant_landline/{LANDLINE_KEY}/edit"' in response.text
    # Keep-mine resubmits the preserved values with force=1.
    assert 'name="force" value="1"' in response.text

    # No write and no reload.
    assert _current_target(seeded_db) == "999"
    assert worker.calls == []


def test_force_one_saves_over_the_changed_row(seeded_db, auth_client, app):
    worker = RecordingWorker()
    app.state.worker = worker

    loaded = auth_client.get(f"/tables/tenant_landline/{LANDLINE_KEY}/edit")
    old_hash = _row_hash(loaded.text)
    _set_target(seeded_db, "999")

    payload = dict(_payload(old_hash, target="home"), force="1")
    response = auth_client.post(
        f"/tables/tenant_landline/{LANDLINE_KEY}/edit", data=payload
    )

    assert response.status_code == 303, response.text
    assert _current_target(seeded_db) == "home"
    assert worker.calls == [("tenant_landline", "mtree.reload", ["landline"])]


def test_unchanged_row_hash_does_not_block_a_normal_edit(seeded_db, auth_client, app):
    worker = RecordingWorker()
    app.state.worker = worker

    loaded = auth_client.get(f"/tables/tenant_landline/{LANDLINE_KEY}/edit")
    old_hash = _row_hash(loaded.text)

    response = auth_client.post(
        f"/tables/tenant_landline/{LANDLINE_KEY}/edit",
        data=_payload(old_hash, target="710"),
    )

    assert response.status_code == 303, response.text
    assert _current_target(seeded_db) == "710"
    assert worker.calls == [("tenant_landline", "mtree.reload", ["landline"])]


def test_stale_edit_fragment_returns_the_conflict_banner(seeded_db, auth_client, app):
    loaded = auth_client.get(f"/tables/tenant_landline/{LANDLINE_KEY}/edit")
    old_hash = _row_hash(loaded.text)
    _set_target(seeded_db, "999")

    response = auth_client.post(
        f"/tables/tenant_landline/{LANDLINE_KEY}/edit",
        data=_payload(old_hash),
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 409
    lowered = response.text.lower()
    assert "<!doctype" not in lowered and "<html" not in lowered
    assert 'id="conflict"' in response.text
