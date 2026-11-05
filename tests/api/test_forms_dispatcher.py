"""API tests for the dispatcher create form (T4.5, plan §5.2/§6).

These exercise the *same* generic router as T4.1; the dispatcher family only
adds named rules, so the assertions focus on the save/block split and on the
warning text the operator actually sees.
"""
from __future__ import annotations

import html
import sqlite3
from urllib.parse import parse_qs, urlparse

from console.rules import dispatcher as rules

BASE = {
    "setid": "14",
    "destination": "sip:10.0.0.10:5060;transport=tcp",
    "flags": "0",
    "priority": "0",
    "attrs": "",
    "description": "b-example voip-stack",
}


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


def _row(path, setid: int):
    conn = sqlite3.connect(path)
    try:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            'SELECT * FROM "dispatcher" WHERE setid = ?', (setid,)
        ).fetchone()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# happy paths
# ---------------------------------------------------------------------------
def test_ipv4_tcp_destination_saves_and_enqueues_one_reload(
    seeded_db, auth_client, app
):
    worker = RecordingWorker()
    app.state.worker = worker

    response = auth_client.post("/tables/dispatcher/new", data=BASE)

    assert response.status_code == 303, response.text
    assert _flash(response).startswith("Saved")

    row = _row(seeded_db, 14)
    assert row is not None
    assert row["destination"] == "sip:10.0.0.10:5060;transport=tcp"
    assert worker.calls == [("dispatcher", "dispatcher.reload", [])]


def test_ipv4_udp_destination_saves_with_the_mtu_warning(
    seeded_db, auth_client, app
):
    worker = RecordingWorker()
    app.state.worker = worker

    response = auth_client.post(
        "/tables/dispatcher/new",
        data=dict(BASE, destination="sip:10.0.0.10:5060;transport=udp"),
    )

    assert response.status_code == 303, response.text
    flash = _flash(response)
    assert "Saved" in flash
    assert "Warning" in flash
    assert (
        "OPTIONS pings stay green while INVITEs fragment and die in the tunnel — "
        "use ;transport=tcp for tunnel destinations"
    ) in flash
    assert "REGISTER" not in flash
    assert _row(seeded_db, 14) is not None
    assert worker.calls == [("dispatcher", "dispatcher.reload", [])]


def test_missing_transport_saves_with_the_omitted_transport_warning(
    seeded_db, auth_client, app
):
    app.state.worker = RecordingWorker()

    response = auth_client.post(
        "/tables/dispatcher/new",
        data=dict(BASE, destination="sip:10.0.0.10:5060"),
    )

    assert response.status_code == 303, response.text
    flash = _flash(response)
    assert (
        "transport omitted — treated as tcp; set ;transport=tcp explicitly"
        in flash
    )
    assert rules.TRANSPORT_OMITTED_WARNING in flash

    row = _row(seeded_db, 14)
    assert row is not None
    assert row["destination"] == "sip:10.0.0.10:5060"


# ---------------------------------------------------------------------------
# blocking paths
# ---------------------------------------------------------------------------
def test_fqdn_destination_is_422_with_the_lan_ip_hint(
    seeded_db, auth_client, app
):
    worker = RecordingWorker()
    app.state.worker = worker

    response = auth_client.post(
        "/tables/dispatcher/new",
        data=dict(BASE, destination="sip:voip-stack.lan:5060;transport=tcp"),
    )

    assert response.status_code == 422, response.text
    page = _squash(response.text)
    assert "use the voip-stack host's LAN IP" in page
    assert "not an FQDN" in page
    assert 'aria-invalid="true"' in response.text
    assert 'href="#field-destination"' in response.text

    assert _row(seeded_db, 14) is None
    assert worker.calls == []


def test_udp_does_not_block_and_bad_transport_does(
    seeded_db, auth_client, app
):
    app.state.worker = RecordingWorker()

    blocked = auth_client.post(
        "/tables/dispatcher/new",
        data=dict(BASE, destination="sip:10.0.0.10:5060;transport=tls"),
    )
    assert blocked.status_code == 422, blocked.text
    assert "transport must be tcp or udp" in _squash(blocked.text)
    assert _row(seeded_db, 14) is None


def test_negative_flags_are_blocked(seeded_db, auth_client, app):
    app.state.worker = RecordingWorker()

    response = auth_client.post(
        "/tables/dispatcher/new", data=dict(BASE, setid="14", flags="-1")
    )

    assert response.status_code == 422, response.text
    assert "Use 0 or a larger whole number" in _squash(response.text)
    assert _row(seeded_db, 14) is None


def test_unusual_flags_warn_and_save(seeded_db, auth_client, app):
    app.state.worker = RecordingWorker()

    response = auth_client.post(
        "/tables/dispatcher/new", data=dict(BASE, setid="14", flags="32")
    )

    assert response.status_code == 303, response.text
    assert "unusual" in _flash(response)
    assert _row(seeded_db, 14) is not None


# ---------------------------------------------------------------------------
# warning must also be visible in the htmx toast fragment
# ---------------------------------------------------------------------------
def test_udp_warning_is_visible_in_the_hx_toast(seeded_db, auth_client, app):
    app.state.worker = RecordingWorker()

    response = auth_client.post(
        "/tables/dispatcher/new",
        data=dict(BASE, destination="sip:10.0.0.10:5060;transport=udp"),
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200, response.text
    assert "<!doctype" not in response.text.lower()
    page = _squash(response.text)
    assert "Saved" in page
    assert "OPTIONS pings stay green" in page
    assert 'role="status"' in response.text
