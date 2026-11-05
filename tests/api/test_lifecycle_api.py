"""Lifecycle API surface (T8 UI): park / restore through the real routes.

Uses the seeded DB, a recording worker (with a FakeRPC for the regcache purge)
and a tmp sidecar dir. The state machines themselves are unit-tested; this
proves the operator path is reachable and wired to them.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from console import settings as settings_module
from tests.fixtures.fakerpc import FakeRPC

DOMAIN = "sip.alpha.home.example.net"


class Worker:
    def __init__(self, rpc):
        self.rpc = rpc
        self.calls = []

    def enqueue(self, table, rpc_name, params):
        self.calls.append((table, rpc_name, params))

    def flush(self, timeout=2.0):
        return True

    @property
    def errors(self):
        return {}


@pytest.fixture()
def lifecycle_env(seeded_db, app, auth_client, tmp_path, monkeypatch):
    monkeypatch.setattr(
        settings_module,
        "settings",
        replace(
            settings_module.settings,
            db_path=str(seeded_db),
            sidecar_dir=str(tmp_path / "console"),
        ),
    )
    with FakeRPC(tmp_path / "rpc.sock") as fake:
        fake.script("htable.dump", {})
        app.state.worker = Worker(fake)
        yield app, auth_client, fake


def test_park_then_restore_through_the_routes(lifecycle_env):
    app, client, fake = lifecycle_env

    page = client.get("/homes")
    assert page.status_code == 200
    assert f"/homes/{DOMAIN}/park" in page.text

    response = client.post(f"/homes/{DOMAIN}/park")
    assert response.status_code == 303, response.text

    page = client.get("/homes")
    assert "Parked" in page.text
    assert f"/homes/{DOMAIN}/restore" in page.text
    assert any(call[0] == "tenantdom" for call in app.state.worker.calls)

    response = client.post(f"/homes/{DOMAIN}/restore")
    assert response.status_code == 303, response.text
    page = client.get("/homes")
    assert DOMAIN in page.text
    assert "Parked" not in page.text


def test_destroy_requires_the_typed_domain(lifecycle_env):
    app, client, fake = lifecycle_env
    response = client.post(f"/homes/{DOMAIN}/destroy", data={"confirm": "wrong"})
    assert response.status_code == 422
    assert client.get("/homes").status_code == 200
