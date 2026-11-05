"""Shared pytest fixtures for the VoIP Edge Console API tests.

T2 owns this file; T3/T4 may append to it but must keep these names stable.

Helper API
----------
``login(client, token=None) -> str``
    POST ``/login`` with ``settings.token`` (or *token*), assert the 303,
    copy the ``csrf`` cookie into ``client.headers['X-CSRF-Token']`` and return
    the CSRF value.  The TestClient does not follow redirects, so the caller
    can assert ``303``/``Location`` itself.

Fixtures
--------
``app``          fresh application instance (``create_app()``)
``client``       ``TestClient(app)`` with ``follow_redirects=False``
``auth_client``  ``client`` already logged in via ``login()``

The autouse fixture resets the process-local login throttle for every test so
rate-limit tests never leak state into unrelated cases.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from console import security
from console import settings as settings_module
from console.app import create_app

CSRF_HEADER = security.CSRF_HEADER
CSRF_COOKIE = security.CSRF_COOKIE
SESSION_COOKIE = security.SESSION_COOKIE


def login(client: TestClient, token: str | None = None) -> str:
    """Log *client* in and arm its CSRF header; return the CSRF value."""
    supplied = settings_module.settings.token if token is None else token
    response = client.post("/login", data={"token": supplied})
    assert response.status_code == 303, response.text
    csrf = client.cookies.get(CSRF_COOKIE)
    assert csrf, "login did not set a csrf cookie"
    client.headers[CSRF_HEADER] = csrf
    return csrf


@pytest.fixture()
def app(monkeypatch):
    from dataclasses import replace as _replace

    monkeypatch.setattr(
        settings_module,
        "settings",
        _replace(settings_module.settings, sweeper_interval=0),
    )
    return create_app()


@pytest.fixture()
def client(app):
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client


@pytest.fixture()
def auth_client(client):
    """An authenticated client with the CSRF header already set."""
    login(client)
    return client


@pytest.fixture(autouse=True)
def _reset_login_throttle():
    security.reset_login_rate_limit()
    yield
    security.reset_login_rate_limit()


# ---------------------------------------------------------------------------
# T3 (read-only browser) fixtures — appended, existing fixtures unchanged
# ---------------------------------------------------------------------------
@pytest.fixture()
def seeded_db(tmp_path, monkeypatch):
    """A WAL edge database with the three realistic homes, wired to settings."""
    from dataclasses import replace

    from tests.fixtures import seeddb

    path = seeddb.seed(tmp_path / "edge.sqlite")
    monkeypatch.setattr(
        settings_module,
        "settings",
        replace(settings_module.settings, db_path=str(path)),
    )
    return path


@pytest.fixture()
def empty_db(tmp_path, monkeypatch):
    """A schema-only WAL edge database (first-use empty state)."""
    from dataclasses import replace

    from tests.fixtures import seeddb

    path = seeddb.empty(tmp_path / "empty.sqlite")
    monkeypatch.setattr(
        settings_module,
        "settings",
        replace(settings_module.settings, db_path=str(path)),
    )
    return path
