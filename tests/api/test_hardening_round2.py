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


def test_empty_token_cannot_log_in(client, monkeypatch):
    from dataclasses import replace as _replace

    from console import security

    monkeypatch.setattr(
        settings_module, "settings", _replace(settings_module.settings, token="")
    )
    security.reset_login_rate_limit()
    response = client.post(
        "/login", data={"token": ""}, headers={"content-type": "application/x-www-form-urlencoded"}
    )
    assert response.status_code == 401


def test_stale_edit_without_row_hash_is_conflict(seeded_db, auth_client):
    import sqlite3

    domain = "sip.alpha.home.example.net"
    # change the row behind the operator's back, then POST without row_hash
    conn = sqlite3.connect(str(seeded_db))
    try:
        conn.execute(
            "UPDATE tenant_registry SET tvalue=? WHERE tprefix=?",
            ("T1,11,31,09999999,changed,0,1+1", domain),
        )
        conn.commit()
    finally:
        conn.close()
    response = auth_client.post(
        f"/tables/tenant_registry/{domain}/edit",
        data={
            "tprefix": domain,
            "tenantid": "T1",
            "ds_onnet": "11",
            "ds_pstn": "31",
            "cli": "01111111",
            "label": "unchanged",
            "dpid": "0",
            "flags": "1+1",
        },
    )
    assert response.status_code == 409


def test_dialplan_dpid_zero_row_is_deletable(seeded_db, auth_client):
    import sqlite3

    conn = sqlite3.connect(str(seeded_db))
    try:
        conn.execute(
            "INSERT INTO dialplan (dpid, pr, match_op, match_exp, attrs) VALUES (0, 1, 1, '^x$', 'none')"
        )
        conn.commit()
        row_id = conn.execute(
            "SELECT id FROM dialplan WHERE dpid=0 AND match_exp='^x$'"
        ).fetchone()[0]
    finally:
        conn.close()
    response = auth_client.post(f"/tables/dialplan/{row_id}/delete")
    assert response.status_code == 303
