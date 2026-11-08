"""API tests for the per-home checks page and its live probe."""
from __future__ import annotations

import html
import re

from console.rpc import UnixDatagramRPC
from tests.fixtures.fakerpc import FakeRPC


def _text(page: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html.unescape(page)).split())


def test_home_detail_shows_checklist_and_manual_calls(seeded_db, auth_client):
    page = auth_client.get("/homes/sip.alpha.home.example.net")
    text = _text(page.text)
    assert page.status_code == 200
    assert "Coherence checks" in text
    assert "Edge → PBX" in text
    assert "PBX → edge" in text
    assert "Place a call" in text
    assert "Run live checks" in text
    assert 'hx-get="/homes/sip.alpha.home.example.net/live"' in page.text


def test_home_detail_404_for_unknown_home(seeded_db, auth_client):
    response = auth_client.get("/homes/sip.missing.example.net")
    assert response.status_code == 404


def test_home_detail_requires_login(client):
    response = client.get("/homes/sip.alpha.home.example.net")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_home_live_filters_phones_to_this_domain(
    seeded_db, auth_client, app, tmp_path
):
    with FakeRPC(tmp_path / "rpc.sock") as fake:
        fake.script("core.uptime", {"uptime": "1 hour"})
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
                                        "URI": "sip:10.0.0.10:5060;transport=tcp",
                                        "FLAGS": "AP",
                                        "PRIORITY": 0,
                                    }
                                }
                            ],
                        }
                    },
                    {
                        "SET": {
                            "ID": 31,
                            "TARGETS": [
                                {
                                    "DEST": {
                                        "URI": "sip:10.0.0.10:5060;transport=tcp",
                                        "FLAGS": "AP",
                                        "PRIORITY": 0,
                                    }
                                }
                            ],
                        }
                    },
                ],
            },
        )
        fake.script(
            "permissions.trustedDump",
            {
                "table": 33,
                "item": {
                    "ip": "100.64.10.10",
                    "tag": "sip.alpha.home.example.net",
                },
            },
        )
        fake.script(
            "htable.dump",
            [
                {
                    "entry": 0,
                    "size": 2,
                    "slot": [
                        {
                            "item": {
                                "name": "mobile1@sip.alpha.home.example.net",
                                "value": "sip:198.51.100.20:2416;transport=tls",
                            }
                        },
                        {
                            "item": {
                                "name": "garden@sip.beta.home.example.net",
                                "value": "sip:198.51.100.21:43774;transport=tcp",
                            }
                        },
                    ],
                }
            ],
        )
        app.state.rpc = UnixDatagramRPC(fake.sock_path, timeout_ms=500)
        page = auth_client.get("/homes/sip.alpha.home.example.net/live")
    text = _text(page.text)
    assert page.status_code == 200
    assert "Edge reachable" in text
    assert "mobile1@sip.alpha.home.example.net" in text
    assert "garden@sip.beta.home.example.net" not in text
    assert "tls" in text


def test_home_live_reports_unreachable_edge(seeded_db, auth_client, app):
    app.state.rpc = None
    app.state.get_rpc = lambda: None
    page = auth_client.get("/homes/sip.alpha.home.example.net/live")
    assert page.status_code == 200
    assert "Edge unreachable" in page.text
