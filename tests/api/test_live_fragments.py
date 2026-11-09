"""T6 API tests — /live full page vs /live/partial fragment, auth and RPC-down.

The production composition root wires the router with
``app.include_router(build_live_router(lambda: app.state.rpc))`` (see
console/live.py).  These tests do the same wiring on the conftest ``app``
fixture, then reuse the shared ``auth_client`` fixture.
"""
from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from console import settings as settings_module
from console.live import build_live_router
from console.rpc import UnixDatagramRPC
from tests.fixtures.fakerpc import FakeRPC


@pytest.fixture()
def fake(tmp_path):
    with FakeRPC(tmp_path / "kamailio_rpc.sock") as server:
        yield server


@pytest.fixture()
def live_app(app):
    app.include_router(build_live_router(lambda: app.state.rpc))
    return app


@pytest.fixture()
def client(live_app):
    # Override conftest's client so ``auth_client`` logs into the app that has
    # the live router mounted.
    with TestClient(live_app, follow_redirects=False) as test_client:
        yield test_client


def _script_healthy_edge(fake):
    fake.script("core.uptime", {"uptime": "6 days, 3 hours"})
    fake.script(
        "htable.dump",
        [
            {
                "entry": 0,
                "size": 2,
                "slot": [
                    {
                        "item": {
                            "name": "garden@sip.beta.home.example.net",
                            "value": "sip:198.51.100.21:43774;transport=tcp",
                        }
                    },
                    {
                        "item": {
                            "name": "mobile1@sip.alpha.home.example.net",
                            "value": "sip:198.51.100.20:2416;transport=tls",
                        }
                    },
                ],
            }
        ],
    )
    fake.script(
        "dispatcher.list",
        {
            "NRSETS": 2,
            "RECORDS": [
                {
                    "SET": {
                        "ID": 11,
                        "TARGETS": [
                            {
                                "DEST": {
                                    "URI": "sip:10.0.0.10:5060",
                                    "FLAGS": "AP",
                                    "PRIORITY": 0,
                                }
                            }
                        ],
                    }
                },
                {
                    "SET": {
                        "ID": 12,
                        "TARGETS": [
                            {
                                "DEST": {
                                    "URI": "sip:10.0.7.10:5060",
                                    "FLAGS": "TP",
                                    "PRIORITY": 0,
                                }
                            }
                        ],
                    }
                },
            ],
        },
    )


def test_live_full_page_has_doctype_nav_polling_and_rows(
    auth_client, live_app, fake, monkeypatch
):
    live_app.state.rpc = fake
    _script_healthy_edge(fake)
    monkeypatch.setattr(
        settings_module,
        "settings",
        replace(settings_module.settings, poll_ms=5000),
    )

    response = auth_client.get("/live")
    assert response.status_code == 200
    text = response.text

    assert "<!doctype" in text.lower()
    assert "<html" in text.lower()
    assert 'aria-current="page"' in text  # Live nav item
    assert 'hx-get="/live/partial"' in text
    assert 'hx-trigger="every 5000ms"' in text
    assert 'hx-swap="innerHTML"' in text

    # Health and both live panels.
    assert "Edge control" in text
    assert "Online" in text
    assert "6 days, 3 hours" in text
    assert "Registered phones" in text
    assert "phones-table" in text
    assert "phone-flow" in text
    assert "Route set probes" in text

    # Rows from the scripted fake replies.
    assert "garden@sip.beta.home.example.net" in text
    assert "198.51.100.21:43774" in text
    assert "TLS" in text
    assert "sip:10.0.0.10:5060" in text
    assert "Active · probed" in text
    assert "Trying · last probe: no answer" in text


def test_live_partial_is_a_fragment_without_shell(auth_client, live_app, fake):
    live_app.state.rpc = fake
    _script_healthy_edge(fake)

    response = auth_client.get("/live/partial")
    assert response.status_code == 200
    lowered = response.text.lower()

    assert "<!doctype" not in lowered
    assert "<html" not in lowered
    assert "<body" not in lowered
    # The polling wrapper belongs to the full page, not to the swapped inner
    # fragment (otherwise the first swap would destroy the poller).
    assert "hx-get=\"/live/partial\"" not in response.text
    assert "Registered phones" in response.text
    assert "garden@sip.beta.home.example.net" in response.text
    assert "HX-Stop-Polling" not in response.headers


@pytest.mark.parametrize("path", ["/live", "/live/partial"])
def test_live_routes_require_a_session(client, path):
    response = client.get(path)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_live_partial_when_edge_is_down_is_200_banner_and_stops_polling(
    auth_client, live_app, tmp_path
):
    # No FakeRPC at this path: the datagram client fails exactly like a down
    # Kamailio.
    live_app.state.rpc = UnixDatagramRPC(
        str(tmp_path / "missing-kamailio.sock"), timeout_ms=50
    )

    response = auth_client.get("/live/partial")
    assert response.status_code == 200
    assert "Edge control offline" in response.text
    assert response.headers.get("HX-Stop-Polling") == "true"


def test_live_full_page_when_edge_is_down_is_200_with_banner(
    auth_client, live_app, tmp_path
):
    live_app.state.rpc = UnixDatagramRPC(
        str(tmp_path / "missing-kamailio.sock"), timeout_ms=50
    )

    response = auth_client.get("/live")
    assert response.status_code == 200
    assert "<!doctype" in response.text.lower()
    assert "Edge control offline" in response.text
    assert "HX-Stop-Polling" not in response.headers


def test_live_when_rpc_is_not_configured_still_renders_the_banner(
    auth_client, live_app
):
    # The composition root may not have attached ``app.state.rpc`` yet.
    if hasattr(live_app.state, "rpc"):
        delattr(live_app.state, "rpc")

    response = auth_client.get("/live/partial")
    assert response.status_code == 200
    assert "Edge control offline" in response.text
    assert response.headers.get("HX-Stop-Polling") == "true"
