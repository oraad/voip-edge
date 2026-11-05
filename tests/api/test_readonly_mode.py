"""T9 API tests — CONSOLE_READONLY is a single cross-cutting gate.

The middleware refuses every unsafe method before the route/dependency runs.
GETs (browsing, live, audit/backups) are unaffected; login/logout remain
reachable so an operator can enter and leave a read-only console.
"""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from conftest import CSRF_HEADER, login
from console import settings as settings_module


@pytest.fixture()
def readonly_settings(monkeypatch):
    monkeypatch.setattr(
        settings_module,
        "settings",
        replace(settings_module.settings, readonly=True),
    )
    return settings_module.settings


def test_readonly_post_is_refused_with_banner_and_no_write(readonly_settings, client):
    login(client)  # /login is exempt so the session can exist

    response = client.post(
        "/tables/tenant_registry/new",
        data={"tprefix": "new.home.example.net", "tenantid": "X1"},
        headers={CSRF_HEADER: client.cookies.get("csrf")},
    )

    assert response.status_code == 403
    assert "console is in read-only mode" in response.text
    assert "writes are refused" in response.text


def test_readonly_gate_runs_before_csrf_and_route_dependencies(readonly_settings, client):
    # No session and no CSRF header: readonly still answers with its banner,
    # proving the gate is the single outer check.
    response = client.post("/wizard/apply", data={})
    assert response.status_code == 403
    assert "console is in read-only mode" in response.text


@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH"])
def test_readonly_refuses_every_unsafe_method(readonly_settings, client, method):
    login(client)
    response = client.request(
        method,
        "/tables/dispatcher/1/edit",
        headers={CSRF_HEADER: client.cookies.get("csrf")},
    )
    assert response.status_code == 403
    assert "console is in read-only mode" in response.text


@pytest.mark.parametrize("path", ["/homes", "/live", "/system/audit", "/system/backups"])
def test_readonly_gets_are_unaffected_and_show_global_banner(
    readonly_settings, client, path
):
    login(client)
    response = client.get(path)
    assert response.status_code == 200
    assert "console is in read-only mode" in response.text
    assert "read-only: <strong>on</strong>" in response.text


def test_readonly_login_and_logout_remain_reachable(readonly_settings, client):
    login(client)
    response = client.post(
        "/logout", headers={CSRF_HEADER: client.cookies.get("csrf")}
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_readonly_refusal_is_logged_as_structured_json(readonly_settings, client, capsys):
    login(client)
    capsys.readouterr()
    response = client.post(
        "/tables/tenant_registry/new",
        data={},
        headers={CSRF_HEADER: client.cookies.get("csrf")},
    )
    assert response.status_code == 403

    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    records = [json.loads(line) for line in lines]
    refusals = [record for record in records if record["event"] == "readonly.refused"]
    assert refusals
    assert refusals[-1]["fields"]["method"] == "POST"
    assert refusals[-1]["fields"]["path"] == "/tables/tenant_registry/new"


@pytest.mark.parametrize("readonly", [True, False])
def test_login_rate_limit_still_blocks_sixth_attempt(monkeypatch, client, readonly):
    """T2's 5/min/IP policy is unchanged by the READONLY middleware."""
    monkeypatch.setattr(
        settings_module,
        "settings",
        replace(
            settings_module.settings,
            readonly=readonly,
            login_rate_per_min=5,
        ),
    )
    statuses = [
        client.post("/login", data={"token": f"bad-{i}"}).status_code
        for i in range(6)
    ]
    assert statuses[:5] == [401] * 5
    assert statuses[5] == 429
    assert client.post(
        "/login", data={"token": settings_module.settings.token}
    ).status_code == 429


def test_auth_failure_is_logged_without_the_supplied_or_configured_token(
    client, capsys
):
    capsys.readouterr()
    response = client.post("/login", data={"token": "guess-token-123"})
    assert response.status_code == 401

    out = capsys.readouterr().out
    records = [json.loads(line) for line in out.splitlines() if line.strip()]
    failures = [record for record in records if record["event"] == "auth.failure"]
    assert failures
    assert failures[-1]["fields"]["reason"] == "invalid_token"
    assert "guess-token-123" not in out
    assert settings_module.settings.token not in out
