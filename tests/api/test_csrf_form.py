"""Plain browser form posts must pass CSRF without htmx (hidden field path).

The console has to stay usable with JavaScript disabled: the middleware accepts
the double-submit token from either the X-CSRF-Token header (htmx) or the
hidden `csrf_token` form field, and replays the request body for the route.
"""
from __future__ import annotations

from urllib.parse import urlencode


def test_plain_form_post_with_hidden_csrf_succeeds(seeded_db, auth_client):
    # auth_client has the csrf cookie; drop the header to emulate a plain form.
    csrf = auth_client.cookies.get("csrf")
    auth_client.headers.pop("X-CSRF-Token", None)

    data = {
        "csrf_token": csrf,
        "tprefix": "sip.plain.home.example.net",
        "tenantid": "T9",
        "ds_onnet": "11",
        "ds_pstn": "31",
        "cli": "09999999",
        "label": "Plain",
        "dpid": "10",
        "flags": "1+1+90",
    }
    r = auth_client.post(
        "/tables/tenant_registry/new",
        content=urlencode(data),
        headers={"content-type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert r.status_code in (303, 200), r.text[:2000]

    import sqlite3

    conn = sqlite3.connect(str(seeded_db))
    try:
        row = conn.execute(
            "SELECT tvalue FROM tenant_registry WHERE tprefix=?",
            ("sip.plain.home.example.net",),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None


def test_plain_form_post_with_wrong_hidden_csrf_is_403(seeded_db, auth_client):
    auth_client.headers.pop("X-CSRF-Token", None)
    r = auth_client.post(
        "/tables/tenant_registry/new",
        content=urlencode({"csrf_token": "wrong", "tprefix": "x"}),
        headers={"content-type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert r.status_code == 403
