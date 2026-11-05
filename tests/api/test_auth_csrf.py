"""T2 auth/session/CSRF contract tests (plan §2, §10)."""
from __future__ import annotations

from dataclasses import replace

import pytest

from conftest import CSRF_HEADER, login
from console import settings as settings_module
from console.security import sign_session, verify_session
from console.settings import settings


def test_health_is_public_and_always_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize("path", ["/", "/homes"])
def test_unauthenticated_pages_redirect_to_login(client, path):
    response = client.get(path)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_wrong_token_returns_401_with_error_summary(client):
    response = client.post("/login", data={"token": "not-the-token"})
    assert response.status_code == 401
    assert "Invalid token" in response.text
    assert 'tabindex="-1"' in response.text
    assert 'aria-invalid="true"' in response.text
    assert client.cookies.get("session") is None
    assert client.cookies.get("csrf") is None


def test_correct_token_sets_session_and_csrf_cookies(client):
    response = client.post("/login", data={"token": settings.token})
    assert response.status_code == 303
    assert response.headers["location"] == "/"

    raw = response.headers.get_list("set-cookie")
    session_cookie = next(c for c in raw if c.startswith("session="))
    csrf_cookie = next(c for c in raw if c.startswith("csrf="))
    assert "httponly" in session_cookie.lower()
    assert "httponly" not in csrf_cookie.lower()
    assert "samesite=lax" in session_cookie.lower()
    assert "samesite=lax" in csrf_cookie.lower()

    csrf = client.cookies.get("csrf")
    assert csrf
    payload = verify_session(client.cookies.get("session"))
    assert payload is not None
    assert payload["csrf"] == csrf
    assert payload["sub"] == "token"
    assert payload["exp"] > 0


def test_authenticated_index_redirects_to_homes(auth_client):
    response = auth_client.get("/")
    assert response.status_code == 303
    assert response.headers["location"] == "/homes"


def test_logout_requires_csrf_then_clears_cookies(client):
    login(client)

    # Same client, header deliberately dropped: double-submit must reject it.
    client.headers.pop(CSRF_HEADER, None)
    rejected = client.post("/logout")
    assert rejected.status_code == 403
    assert client.cookies.get("session"), "CSRF rejection must not clear the session"

    csrf = client.cookies.get("csrf")
    cleared = client.post("/logout", headers={CSRF_HEADER: csrf})
    assert cleared.status_code == 303
    assert cleared.headers["location"] == "/login"
    assert client.cookies.get("session") in (None, "")
    assert client.cookies.get("csrf") in (None, "")


def test_login_rate_limit_blocks_sixth_bad_attempt(client, monkeypatch):
    # Pin the documented policy (5/min/IP) regardless of the ambient env.
    monkeypatch.setattr(
        settings_module,
        "settings",
        replace(settings_module.settings, login_rate_per_min=5),
    )
    statuses = [
        client.post("/login", data={"token": f"bad-{i}"}).status_code for i in range(6)
    ]
    assert statuses[:5] == [401] * 5
    assert statuses[5] == 429
    # Even the correct token is refused while the window is exhausted.
    assert client.post("/login", data={"token": settings.token}).status_code == 429


def test_secure_cookie_flag_follows_setting(client, monkeypatch):
    monkeypatch.setattr(
        settings_module,
        "settings",
        replace(settings_module.settings, secure_cookie=True),
    )
    response = client.post("/login", data={"token": settings_module.settings.token})
    assert response.status_code == 303
    raw = response.headers.get_list("set-cookie")
    auth_cookies = [c for c in raw if c.startswith(("session=", "csrf="))]
    assert len(auth_cookies) == 2
    assert all("secure" in cookie.lower() for cookie in auth_cookies)


def test_session_tokens_round_trip_and_reject_tampering():
    token = sign_session({"sub": "token", "csrf": "abc123"})
    payload = verify_session(token)
    assert payload is not None
    assert payload["csrf"] == "abc123"

    assert verify_session(token + "x") is None
    assert verify_session("garbage") is None
    assert verify_session(sign_session({"sub": "token", "csrf": "a", "exp": 1})) is None
