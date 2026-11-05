"""Regression tests for the second review round (v0.1.9)."""
from __future__ import annotations

import time
from dataclasses import replace

import pytest

from console import security
from console import settings as settings_module


def test_security_headers_present(auth_client):
    response = auth_client.get("/homes")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


def test_oversized_body_is_rejected(client):
    response = client.post(
        "/login",
        content=b"token=" + b"x" * (2 * 1024 * 1024),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 413


def test_token_session_is_rejected_in_sso_mode(seeded_db, auth_client):
    assert auth_client.get("/homes").status_code == 200
    settings_module.settings = replace(
        settings_module.settings, auth_mode="oidc", oidc_issuer="https://id.example",
        oidc_client_id="console", oidc_insecure=True,
    )
    response = auth_client.get("/homes")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_landline_ds_onnet_is_derived_not_posted(seeded_db, auth_client, app):
    class Worker:
        rpc = None
        calls = []

        def enqueue(self, table, rpc_name, params):
            self.calls.append((table, rpc_name, params))

        def flush(self, timeout=2.0):
            return True

        errors = {}

    app.state.worker = Worker()
    response = auth_client.post(
        "/tables/tenant_landline/new",
        data={
            "tprefix": "09999999",
            "tenantid": "T1",
            "target": "office",
            "ds_onnet": "31",  # forged: T1's real set is 11
            "ih_on": "1",
        },
    )
    assert response.status_code == 303, response.text
    import sqlite3

    conn = sqlite3.connect(str(seeded_db))
    try:
        value = conn.execute(
            "SELECT tvalue FROM tenant_landline WHERE tprefix='09999999'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert value.split(",")[2] == "11"


def test_dialplan_block_partition_is_delete_guarded(seeded_db, auth_client):
    import sqlite3

    conn = sqlite3.connect(str(seeded_db))
    try:
        row = conn.execute(
            "SELECT id FROM dialplan WHERE dpid=90 ORDER BY id LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    response = auth_client.post(f"/tables/dialplan/{row[0]}/delete")
    assert response.status_code == 409
    assert "tenant_registry" in response.text or "in use" in response.text.lower()


def test_corrupt_packed_row_does_not_break_the_list(seeded_db, auth_client):
    import sqlite3

    conn = sqlite3.connect(str(seeded_db))
    try:
        conn.execute(
            "INSERT INTO tenant_registry (tprefix, tvalue) VALUES (?, ?)",
            ("sip.corrupt.home.example.net", "T9,11"),
        )
        conn.commit()
    finally:
        conn.close()
    response = auth_client.get("/tables/tenant_registry")
    assert response.status_code == 200
    assert "corrupt row" in response.text.lower()
