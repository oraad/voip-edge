"""API tests for T5 row delete + dependency guards (plan §5.2, §7.8, §8 T5)."""
from __future__ import annotations

import html
import sqlite3
from urllib.parse import parse_qs, urlparse

from conftest import CSRF_HEADER, login

FREE_SETID = 14
FREE_DESTINATION = "sip:10.0.0.10:5060;transport=tcp"


class RecordingWorker:
    """Minimal ReloadWorker double: records the exact enqueue contract."""

    def __init__(self):
        self.calls: list[tuple[str, str, list]] = []

    def enqueue(self, table: str, rpc_name: str, params: list) -> None:
        self.calls.append((table, rpc_name, list(params)))


def _squash(text: str) -> str:
    return " ".join(html.unescape(text).split())


def _flash(response) -> str:
    location = response.headers["location"]
    return parse_qs(urlparse(location).query)["flash"][0]


def _fetch(path, table: str, key):
    conn = sqlite3.connect(path)
    try:
        conn.row_factory = sqlite3.Row
        column = "id" if table not in ("tenant_registry", "tenant_landline") else "tprefix"
        return conn.execute(
            f'SELECT * FROM "{table}" WHERE "{column}" = ?', (key,)
        ).fetchone()
    finally:
        conn.close()


def _free_dispatcher(path) -> int:
    conn = sqlite3.connect(path)
    try:
        cursor = conn.execute(
            "INSERT INTO dispatcher "
            "(setid, destination, flags, priority, attrs, description) "
            "VALUES (?, ?, 0, 0, '', 'free route set')",
            (FREE_SETID, FREE_DESTINATION),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def _dialplan_id(path, dpid: int) -> int:
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            'SELECT id FROM "dialplan" WHERE dpid = ?', (dpid,)
        ).fetchone()
        assert row is not None
        return int(row[0])
    finally:
        conn.close()


def _full_page(text: str) -> bool:
    lowered = text.lower()
    return "<!doctype" in lowered and "<html" in lowered


def _fragment(text: str) -> bool:
    lowered = text.lower()
    return not any(marker in lowered for marker in ("<!doctype", "<html", "<body"))


# ---------------------------------------------------------------------------
# confirmation page vs blocked page
# ---------------------------------------------------------------------------
def test_free_row_gets_a_named_delete_confirmation(seeded_db, auth_client):
    row_id = _free_dispatcher(seeded_db)

    response = auth_client.get(f"/tables/dispatcher/{row_id}/delete")

    assert response.status_code == 200, response.text
    page = _squash(response.text)
    assert "free route set" in page
    assert str(FREE_SETID) in page
    assert "Delete" in page
    assert 'method="post"' in response.text


def test_in_use_set_is_blocked_on_get_and_post_and_names_the_tenant(
    seeded_db, auth_client, app
):
    worker = RecordingWorker()
    app.state.worker = worker

    get_blocked = auth_client.get("/tables/dispatcher/1/delete")
    assert get_blocked.status_code == 200, get_blocked.text
    page = _squash(get_blocked.text)
    assert "sip.alpha.home.example.net" in page
    assert "in use" in page
    assert "/delete" not in get_blocked.text  # only the global sign-out form

    # POST re-checks inside the write transaction; it must not delete anything.
    post_blocked = auth_client.post("/tables/dispatcher/1/delete")
    assert post_blocked.status_code == 409, post_blocked.text
    assert "sip.alpha.home.example.net" in _squash(post_blocked.text)

    assert _fetch(seeded_db, "dispatcher", 1) is not None
    assert worker.calls == []


def test_generic_cross_ref_blocks_deleting_a_partition_in_use(
    seeded_db, auth_client, app
):
    """tenant_registry.dpid -> dialplan.dpid, discovered from cross_refs."""
    app.state.worker = RecordingWorker()
    dialplan_id = _dialplan_id(seeded_db, 10)

    blocked = auth_client.post(f"/tables/dialplan/{dialplan_id}/delete")

    assert blocked.status_code == 409, blocked.text
    assert "sip.alpha.home.example.net" in _squash(blocked.text)
    assert _fetch(seeded_db, "dialplan", dialplan_id) is not None


# ---------------------------------------------------------------------------
# successful delete
# ---------------------------------------------------------------------------
def test_free_dispatcher_row_is_deleted_and_reload_enqueued_once(
    seeded_db, auth_client, app
):
    worker = RecordingWorker()
    app.state.worker = worker
    row_id = _free_dispatcher(seeded_db)

    response = auth_client.post(f"/tables/dispatcher/{row_id}/delete")

    assert response.status_code == 303, response.text
    assert _flash(response) == (
        f"Deleted {row_id} — reload scheduled shortly"
    )
    assert _fetch(seeded_db, "dispatcher", row_id) is None
    assert worker.calls == [("dispatcher", "dispatcher.reload", [])]


def test_hx_delete_returns_a_fragment_without_a_doctype(
    seeded_db, auth_client, app
):
    app.state.worker = RecordingWorker()
    row_id = _free_dispatcher(seeded_db)

    response = auth_client.post(
        f"/tables/dispatcher/{row_id}/delete",
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200, response.text
    assert _fragment(response.text)
    page = _squash(response.text)
    assert "Deleted" in page
    assert "reload scheduled shortly" in page
    assert 'role="status"' in response.text
    assert _fetch(seeded_db, "dispatcher", row_id) is None


# ---------------------------------------------------------------------------
# policy + auth
# ---------------------------------------------------------------------------
def test_lifecycle_tables_have_no_row_delete_route(
    seeded_db, auth_client, app
):
    app.state.worker = RecordingWorker()
    domain = "sip.alpha.home.example.net"

    for method in ("get", "post"):
        response = getattr(auth_client, method)(
            f"/tables/tenant_registry/{domain}/delete"
        )
        assert response.status_code == 404, response.text
        page = _squash(response.text)
        assert "Homes are managed through Park / Destroy" in page
        assert 'href="/homes"' in response.text

    response = auth_client.get("/tables/tenant_landline/01111111/delete")
    assert response.status_code == 404
    assert "Homes are managed through Park / Destroy" in _squash(response.text)


def test_delete_post_without_csrf_is_403(seeded_db, client):
    login(client)
    row_id = _free_dispatcher(seeded_db)
    client.headers.pop(CSRF_HEADER, None)

    response = client.post(f"/tables/dispatcher/{row_id}/delete")

    assert response.status_code == 403
    assert _fetch(seeded_db, "dispatcher", row_id) is not None


def test_unknown_row_and_table_are_404(seeded_db, auth_client):
    response = auth_client.get("/tables/dispatcher/99999/delete")
    assert response.status_code == 404

    response = auth_client.get("/tables/not_a_table/1/delete")
    assert response.status_code == 404
    assert "No table named" in response.text


def test_delete_confirmation_full_page_and_fragment_modes(seeded_db, auth_client):
    row_id = _free_dispatcher(seeded_db)

    full = auth_client.get(f"/tables/dispatcher/{row_id}/delete")
    assert _full_page(full.text)

    fragment = auth_client.get(
        f"/tables/dispatcher/{row_id}/delete",
        headers={"HX-Request": "true"},
    )
    assert _fragment(fragment.text)
    assert "free route set" in html.unescape(fragment.text)
