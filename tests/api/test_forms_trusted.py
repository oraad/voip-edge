"""API tests for the trusted-source forms (T4.6, plan §5.2/§7.19).

These go through the generic descriptor router: no per-table route and no
template change.  They prove the two severities end-to-end — a public address
blocks with the override hint, the same address saves once the operator posts
``override_public_ip``, and every blocked save leaves the table untouched and
schedules no reload.
"""
from __future__ import annotations

import html
import sqlite3
from urllib.parse import parse_qs, urlparse

from tests.fixtures import seeddb

O_EXAMPLE = "sip.alpha.home.example.net"
A_EXAMPLE = "sip.beta.home.example.net"

VALID_TRUSTED = {
    "src_ip": "100.64.10.10",
    "proto": "any",
    "from_pattern": "",
    "ruri_pattern": "",
    "tag": O_EXAMPLE,
    "priority": "0",
}


class RecordingWorker:
    """Minimal ReloadWorker double: records the exact enqueue contract."""

    def __init__(self):
        self.calls: list[tuple[str, str, list]] = []

    def enqueue(self, table: str, rpc_name: str, params: list) -> None:
        self.calls.append((table, rpc_name, list(params)))


def _squash(text: str) -> str:
    return " ".join(text.split())


def _trusted_rows(path):
    conn = sqlite3.connect(path)
    try:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT id, src_ip, proto, tag, priority FROM trusted ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def _flash(response) -> str:
    location = response.headers["location"]
    return parse_qs(urlparse(location).query)["flash"][0]


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------
def test_post_src_ip_with_existing_tag_saves_and_enqueues_trusted_reload(
    seeded_db, auth_client, app
):
    worker = RecordingWorker()
    app.state.worker = worker

    response = auth_client.post("/tables/trusted/new", data=VALID_TRUSTED)

    assert response.status_code == 303, response.text
    assert response.headers["location"].startswith("/tables/trusted?")
    flash = _flash(response)
    assert "Saved" in flash
    assert "reload scheduled shortly" in flash

    rows = [row for row in _trusted_rows(seeded_db) if row["src_ip"] == "100.64.10.10"]
    assert len(rows) == 2  # the seeded row plus the one just created
    assert rows[-1]["tag"] == O_EXAMPLE
    assert rows[-1]["proto"] == "any"
    assert rows[-1]["priority"] == 0
    assert worker.calls == [("trusted", "permissions.trustedReload", [])]


def test_alias_shaped_address_saves_with_a_warning(seeded_db, auth_client, app):
    worker = RecordingWorker()
    app.state.worker = worker
    payload = dict(VALID_TRUSTED, src_ip="100.96.128.5")

    response = auth_client.post(
        "/tables/trusted/new", data=payload, headers={"HX-Request": "true"}
    )

    assert response.status_code == 200, response.text
    page = _squash(html.unescape(response.text))
    assert "Saved" in page
    assert "Use the wire-observed native IP, not the utility alias." in page
    assert any(row["src_ip"] == "100.96.128.5" for row in _trusted_rows(seeded_db))
    assert worker.calls == [("trusted", "permissions.trustedReload", [])]


# ---------------------------------------------------------------------------
# public address: error, then the explicit override
# ---------------------------------------------------------------------------
def test_public_address_blocks_with_the_override_hint(seeded_db, auth_client, app):
    worker = RecordingWorker()
    app.state.worker = worker
    payload = dict(VALID_TRUSTED, src_ip="8.8.8.8")

    response = auth_client.post("/tables/trusted/new", data=payload)

    assert response.status_code == 422
    page = _squash(html.unescape(response.text))
    assert "Use the site's wire-observed tunnel address" in page
    assert "onboarding step 2" in page
    assert "tick the override if this home is reached by another path" in page
    assert 'href="#field-src_ip"' in response.text
    assert "8.8.8.8" in page  # value preserved for the operator
    assert all(row["src_ip"] != "8.8.8.8" for row in _trusted_rows(seeded_db))
    assert worker.calls == []


def test_public_address_with_override_saves_with_a_warning(
    seeded_db, auth_client, app
):
    worker = RecordingWorker()
    app.state.worker = worker
    payload = dict(
        VALID_TRUSTED, src_ip="8.8.8.8", override_public_ip="1"
    )

    response = auth_client.post(
        "/tables/trusted/new", data=payload, headers={"HX-Request": "true"}
    )

    assert response.status_code == 200, response.text
    page = _squash(html.unescape(response.text))
    assert "Saved" in page
    assert "Warning" in page
    assert "documented generic non-tunnel path" in page
    assert "not a private/CGNAT address — continuing anyway" in page
    assert any(row["src_ip"] == "8.8.8.8" for row in _trusted_rows(seeded_db))
    assert worker.calls == [("trusted", "permissions.trustedReload", [])]


# ---------------------------------------------------------------------------
# duplicate source across homes
# ---------------------------------------------------------------------------
def test_duplicate_src_ip_for_another_tag_blocks_naming_the_existing_tag(
    seeded_db, auth_client, app
):
    worker = RecordingWorker()
    app.state.worker = worker
    payload = dict(VALID_TRUSTED, src_ip="100.64.10.12", tag=O_EXAMPLE)

    response = auth_client.post("/tables/trusted/new", data=payload)

    assert response.status_code == 422
    page = _squash(html.unescape(response.text))
    assert "already trusted for" in page
    assert A_EXAMPLE in page
    assert all(
        not (row["src_ip"] == "100.64.10.12" and row["tag"] == O_EXAMPLE)
        for row in _trusted_rows(seeded_db)
    )
    assert worker.calls == []


# ---------------------------------------------------------------------------
# tag must be an existing home
# ---------------------------------------------------------------------------
def test_unknown_tag_blocks_and_names_the_available_count(
    seeded_db, auth_client, app
):
    worker = RecordingWorker()
    app.state.worker = worker
    payload = dict(
        VALID_TRUSTED,
        src_ip="100.64.10.11",
        tag="sip.unknown.home.example.net",
    )

    response = auth_client.post("/tables/trusted/new", data=payload)

    assert response.status_code == 422
    page = _squash(html.unescape(response.text))
    assert "Choose an existing home domain (3 available)" in page
    assert "sip.unknown.home.example.net" in page
    assert all(
        row["src_ip"] != "100.64.10.11" for row in _trusted_rows(seeded_db)
    )
    assert worker.calls == []


# ---------------------------------------------------------------------------
# descriptor invariants through the form
# ---------------------------------------------------------------------------
def test_proto_other_than_any_blocks(seeded_db, auth_client, app):
    app.state.worker = RecordingWorker()
    payload = dict(VALID_TRUSTED, src_ip="100.64.10.11", proto="udp")

    response = auth_client.post("/tables/trusted/new", data=payload)

    assert response.status_code == 422
    page = _squash(html.unescape(response.text))
    assert 'proto is locked to "any"' in page
    assert all(
        row["src_ip"] != "100.64.10.11" for row in _trusted_rows(seeded_db)
    )


def test_negative_priority_blocks(seeded_db, auth_client, app):
    app.state.worker = RecordingWorker()
    payload = dict(VALID_TRUSTED, src_ip="100.64.10.11", priority="-2")

    response = auth_client.post("/tables/trusted/new", data=payload)

    assert response.status_code == 422
    assert "Priority cannot be negative." in _squash(html.unescape(response.text))
    assert all(
        row["src_ip"] != "100.64.10.11" for row in _trusted_rows(seeded_db)
    )


# ---------------------------------------------------------------------------
# generic-first guard
# ---------------------------------------------------------------------------
def test_trusted_uses_the_same_descriptor_fields(seeded_db):
    from console import descriptors

    descriptor = descriptors.load().tables["trusted"]
    names = {field.name for field in descriptor.fields}
    assert {"src_ip", "proto", "from_pattern", "ruri_pattern", "tag", "priority"} <= names
    # No override column exists in the trusted table; the override is an
    # explicit acknowledgement, not stored routing data.
    assert seeddb.TABLE_NAMES == (
        "tenant_registry",
        "tenant_landline",
        "dispatcher",
        "dialplan",
        "trusted",
    )
