"""API tests for the onboarding wizard (T7, plan §5.5/§6/§7.1/§7.2).

The happy path proves the full four-step flow, the exact five-row batch, the
tagged backup and the four reload keys.  The no-trunk path proves four rows and
``ds_pstn=0``.  The fault-injection test proves rollback leaves zero wizard
rows.  The TLS test proves the mandatory ACME notice is driven by
``CONSOLE_TLS_LIVE``.  HTMX requests must always receive a fragment.
"""
from __future__ import annotations

import html
import re
import sqlite3
from dataclasses import replace
from pathlib import Path

from console import settings as settings_module
from console import wizard_ui
from console.rpc import UnixDatagramRPC
from tests.fixtures.fakerpc import FakeRPC

IDENTITY = {
    "domain": "sip.delta.home.example.net",
    "landline": "04444444",
    "ring_group": "home",
    "label": "Home example",
}

NETWORK = {
    "lan_ip": "10.0.6.20",
    "wire_ip": "100.64.10.20",
    "has_pstn": "1",
}


class RecordingWorker:
    """ReloadWorker double: records each enqueue and the flush call."""

    def __init__(self):
        self.calls: list[tuple[str, str, list]] = []
        self.flushes = 0
        self.errors: dict[str, BaseException] = {}

    def enqueue(self, table: str, rpc_name: str, params: list) -> None:
        self.calls.append((table, rpc_name, list(params)))

    def flush(self, timeout: float = 2.0) -> bool:
        self.flushes += 1
        return True


def _full_page(text: str) -> bool:
    lowered = text.lower()
    return "<!doctype" in lowered and "<html" in lowered


def _fragment_only(text: str) -> bool:
    lowered = text.lower()
    return not any(marker in lowered for marker in ("<!doctype", "<html", "<body"))


def _hidden(text: str) -> dict[str, str]:
    """Read the stateless draft carried by hidden inputs."""
    return {
        html.unescape(match.group(1)): html.unescape(match.group(2))
        for match in re.finditer(
            r'<input type="hidden" name="([^"]*)" value="([^"]*)"', text
        )
    }


def _text(page: str) -> str:
    """Rendered page text with tags collapsed to single spaces."""
    return " ".join(re.sub(r"<[^>]+>", " ", html.unescape(page)).split())


def _to_review(client, identity=None, network=None):
    """Follow step 1 and step 2; return the review response and apply payload."""
    identity = dict(IDENTITY if identity is None else identity)
    network = dict(NETWORK if network is None else network)

    first = client.post("/wizard/step1", data=identity)
    assert first.status_code == 200, first.text
    first_hidden = _hidden(first.text)

    second = client.post("/wizard/step2", data={**network, **first_hidden})
    assert second.status_code == 200, second.text
    review_hidden = _hidden(second.text)

    payload = {**identity, **network, **review_hidden}
    return second, payload


def _fetch(path, sql: str, params=()):
    conn = sqlite3.connect(path)
    try:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _backups(sidecar_dir: Path) -> list[Path]:
    directory = Path(sidecar_dir) / "backups"
    if not directory.is_dir():
        return []
    return sorted(
        path
        for path in directory.glob("edge-*.sqlite")
        if "wizard-apply" in path.name
    )


def _assert_four_reloads(worker: RecordingWorker):
    assert worker.calls == [
        ("tenantdom", "mtree.reload", ["tenantdom"]),
        ("landline", "mtree.reload", ["landline"]),
        ("dispatcher", "dispatcher.reload", []),
        ("trusted", "permissions.trustedReload", []),
    ]
    assert worker.flushes == 1


# ---------------------------------------------------------------------------
# happy path: 5 rows, one tagged backup, four reloads, green verify
# ---------------------------------------------------------------------------
def test_wizard_happy_path_writes_five_rows_backs_up_and_reloads(
    seeded_db, auth_client, app, tmp_path, monkeypatch
):
    sidecar_dir = tmp_path / "console-data"
    monkeypatch.setattr(
        settings_module,
        "settings",
        replace(
            settings_module.settings,
            db_path=str(seeded_db),
            sidecar_dir=str(sidecar_dir),
        ),
    )
    worker = RecordingWorker()
    app.state.worker = worker

    review, payload = _to_review(auth_client)

    assert review.status_code == 200
    page = " ".join(html.unescape(review.text).split())
    assert "About to be applied" in page
    assert "5 rows" in page
    assert IDENTITY["domain"] in page
    assert "sip.delta.home.example.net" in review.text

    applied = auth_client.post("/wizard/apply", data=payload)
    assert applied.status_code == 303, applied.text
    assert applied.headers["location"] == (
        "/wizard/verify/sip.delta.home.example.net"
    )

    registry = _fetch(
        seeded_db,
        "SELECT tvalue FROM tenant_registry WHERE tprefix = ?",
        (IDENTITY["domain"],),
    )
    assert len(registry) == 1
    assert registry[0]["tvalue"] == "T4,14,34,04444444,Home example,0,1+1"

    landline = _fetch(
        seeded_db,
        "SELECT tvalue FROM tenant_landline WHERE tprefix = ?",
        (IDENTITY["landline"],),
    )
    assert len(landline) == 1
    assert landline[0]["tvalue"] == "T4,home,14,1"

    dispatcher = _fetch(
        seeded_db,
        "SELECT setid, destination FROM dispatcher WHERE setid IN (14, 34) "
        "ORDER BY setid",
    )
    assert [row["setid"] for row in dispatcher] == [14, 34]
    assert all(
        row["destination"] == "sip:10.0.6.20:5060;transport=tcp"
        for row in dispatcher
    )

    trusted = _fetch(
        seeded_db,
        "SELECT src_ip, proto, tag FROM trusted WHERE tag = ?",
        (IDENTITY["domain"],),
    )
    assert len(trusted) == 1
    assert trusted[0]["src_ip"] == "100.64.10.20"
    assert trusted[0]["proto"] == "any"

    # Exactly one backup, with the required purpose tag.
    backups = _backups(sidecar_dir)
    assert len(backups) == 1
    assert "wizard-apply" in backups[0].name

    _assert_four_reloads(worker)

    verify = auth_client.get(
        f"/wizard/verify/{IDENTITY['domain']}"
    )
    assert verify.status_code == 200
    assert _full_page(verify.text)
    green = html.unescape(verify.text)
    assert "all coherence checks are green" in green
    assert "Home registered in the routing table" in green
    assert "PBX \u2192 edge" in green
    assert "Edge \u2192 PBX" in green
    assert "Place a call from a phone on the PBX" in green
    assert "Dial out through the landline trunk" in green
    assert "Landline points at the ring group" in green
    assert "Waiting for the first registration" in green


def test_step2_shows_pbx_edge_trunk_note(seeded_db, auth_client, monkeypatch):
    from dataclasses import replace as _replace

    monkeypatch.setattr(
        settings_module,
        "settings",
        _replace(
            settings_module.settings,
            db_path=str(seeded_db),
            edge_trunk_host="203.0.113.9",
            edge_trunk_port=5060,
        ),
    )
    page = auth_client.post("/wizard/step1", data=IDENTITY)
    text = " ".join(html.unescape(page.text).split())
    assert "Add the edge trunk on the PBX" in text
    assert "203.0.113.9:5060" in text
    assert "Do not register" in text
    assert "never the tenant's public SIP domain" in text
    assert "PBX LAN IP" in text
    assert "voip-stack" not in text.lower()


def test_step2_note_without_edge_host_is_generic(seeded_db, auth_client, monkeypatch):
    from dataclasses import replace as _replace

    monkeypatch.setattr(
        settings_module,
        "settings",
        _replace(settings_module.settings, db_path=str(seeded_db), edge_trunk_host=""),
    )
    page = auth_client.post("/wizard/step1", data=IDENTITY)
    text = " ".join(html.unescape(page.text).split())
    assert "the edge tunnel address from your deployment" in text
    assert "voip-stack" not in text.lower()


# ---------------------------------------------------------------------------
# no-PSTN variant: 4 rows, ds_pstn=0, PSTN set skipped
# ---------------------------------------------------------------------------
def test_wizard_no_trunk_writes_four_rows_and_skips_pstn_set(
    seeded_db, auth_client, app, tmp_path, monkeypatch
):
    sidecar_dir = tmp_path / "console-data"
    monkeypatch.setattr(
        settings_module,
        "settings",
        replace(
            settings_module.settings,
            db_path=str(seeded_db),
            sidecar_dir=str(sidecar_dir),
        ),
    )
    worker = RecordingWorker()
    app.state.worker = worker

    network = {"lan_ip": "10.0.6.21", "wire_ip": "100.64.10.21"}
    review, payload = _to_review(auth_client, network=network)
    page = " ".join(html.unescape(review.text).split())
    assert "4 rows" in page
    assert "no PSTN trunk" in page

    applied = auth_client.post("/wizard/apply", data=payload)
    assert applied.status_code == 303, applied.text

    registry = _fetch(
        seeded_db,
        "SELECT tvalue FROM tenant_registry WHERE tprefix = ?",
        (IDENTITY["domain"],),
    )
    assert registry[0]["tvalue"] == "T4,14,0,04444444,Home example,0,1+0"

    assert _fetch(
        seeded_db, "SELECT 1 FROM dispatcher WHERE setid = 34"
    ) == []
    assert len(_fetch(seeded_db, "SELECT 1 FROM dispatcher WHERE setid = 14")) == 1
    assert len(
        _fetch(
            seeded_db,
            "SELECT 1 FROM tenant_landline WHERE tprefix = ?",
            (IDENTITY["landline"],),
        )
    ) == 1
    assert len(
        _fetch(
            seeded_db,
            "SELECT 1 FROM trusted WHERE tag = ?",
            (IDENTITY["domain"],),
        )
    ) == 1
    assert len(_backups(sidecar_dir)) == 1
    _assert_four_reloads(worker)

    verify = auth_client.get(f"/wizard/verify/{IDENTITY['domain']}")
    verify_page = " ".join(html.unescape(verify.text).split())
    assert "all coherence checks are green" in verify_page
    assert "No PSTN trunk configured" in verify_page
    assert "Dial out through the landline trunk" not in verify_page


def test_live_verify_probes_dispatcher_and_trusted_table(
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
        app.state.rpc = UnixDatagramRPC(fake.sock_path, timeout_ms=500)
        page = auth_client.get(
            "/wizard/verify/sip.alpha.home.example.net/live"
        )
    text = " ".join(html.unescape(page.text).split())
    assert page.status_code == 200
    assert "Edge reachable" in text
    assert "set 11" in text
    assert "set 31" in text
    assert "100.64.10.10" in text
    assert "loaded" in text


def test_live_verify_reads_all_trusted_entries(
    seeded_db, auth_client, app, tmp_path
):
    with FakeRPC(tmp_path / "rpc.sock") as fake:
        fake.script("core.uptime", {"uptime": "1 hour"})
        fake.script(
            "dispatcher.list",
            {
                "NRSETS": 1,
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
                    }
                ],
            },
        )
        fake.raw(
            "permissions.trustedDump",
            (
                b'{"jsonrpc":"2.0","id":1,"result":{'
                b'"table":0,"item":{"ip":"100.64.10.10",'
                b'"tag":"sip.alpha.home.example.net"},'
                b'"table":1,"item":{"ip":"100.64.10.12",'
                b'"tag":"sip.beta.home.example.net"}}}'
            ),
        )
        app.state.rpc = UnixDatagramRPC(fake.sock_path, timeout_ms=500)
        page = auth_client.get(
            "/wizard/verify/sip.alpha.home.example.net/live"
        )
    text = _text(page.text)
    assert "OK — PBX → edge: trusted tunnel source" in text
    assert (
        "100.64.10.10 → sip.alpha.home.example.net · loaded" in text
    )


def test_live_verify_marks_unavailable_trusted_dump(
    seeded_db, auth_client, app, tmp_path
):
    with FakeRPC(tmp_path / "rpc.sock") as fake:
        fake.script("core.uptime", {"uptime": "1 hour"})
        fake.script("dispatcher.list", {"NRSETS": 0, "RECORDS": []})
        fake.fault("permissions.trustedDump", message="not supported")
        app.state.rpc = UnixDatagramRPC(fake.sock_path, timeout_ms=500)
        page = auth_client.get(
            "/wizard/verify/sip.alpha.home.example.net/live"
        )
    text = _text(page.text)
    assert "Unavailable — PBX → edge: trusted tunnel source" in text


def test_live_verify_reports_inactive_dispatcher(
    seeded_db, auth_client, app, tmp_path
):
    with FakeRPC(tmp_path / "rpc.sock") as fake:
        fake.script("core.uptime", {"uptime": "1 hour"})
        fake.script(
            "dispatcher.list",
            {
                "NRSETS": 1,
                "RECORDS": [
                    {
                        "SET": {
                            "ID": 11,
                            "TARGETS": [
                                {
                                    "DEST": {
                                        "URI": "sip:10.0.0.10:5060;transport=tcp",
                                        "FLAGS": "IP",
                                        "PRIORITY": 0,
                                    }
                                }
                            ],
                        }
                    }
                ],
            },
        )
        fake.script(
            "permissions.trustedDump",
            {"table": 0, "item": {
                "ip": "100.64.10.10",
                "tag": "sip.alpha.home.example.net",
            }},
        )
        app.state.rpc = UnixDatagramRPC(fake.sock_path, timeout_ms=500)
        page = auth_client.get(
            "/wizard/verify/sip.alpha.home.example.net/live"
        )
    text = _text(page.text)
    assert "probe not answered" in text
    assert "OK — Edge → PBX" not in text


def test_live_verify_reports_unreachable_edge(seeded_db, auth_client, app):
    app.state.rpc = None
    app.state.get_rpc = lambda: None
    page = auth_client.get(
        "/wizard/verify/sip.alpha.home.example.net/live"
    )
    assert page.status_code == 200
    assert "Edge unreachable" in page.text


# ---------------------------------------------------------------------------
# fault injection: any insert failure rolls the whole batch back
# ---------------------------------------------------------------------------
def test_wizard_failure_rolls_back_every_row_and_enqueues_nothing(
    seeded_db, auth_client, app, tmp_path, monkeypatch
):
    sidecar_dir = tmp_path / "console-data"
    monkeypatch.setattr(
        settings_module,
        "settings",
        replace(
            settings_module.settings,
            db_path=str(seeded_db),
            sidecar_dir=str(sidecar_dir),
        ),
    )
    worker = RecordingWorker()
    app.state.worker = worker

    _review, payload = _to_review(auth_client)

    original_insert = wizard_ui._insert_row
    calls = {"n": 0}

    def exploding_insert(conn, table, row):
        calls["n"] += 1
        if calls["n"] == 2:  # first row landed, second insert fails
            raise RuntimeError("injected insert failure")
        return original_insert(conn, table, row)

    monkeypatch.setattr(wizard_ui, "_insert_row", exploding_insert)

    failed = auth_client.post("/wizard/apply", data=payload)
    assert failed.status_code == 422, failed.text
    page = " ".join(html.unescape(failed.text).split())
    assert "rolled back" in page
    assert "no rows were written" in page
    assert "injected insert failure" in page
    assert 'class="errsummary"' in failed.text

    # Zero wizard rows remain, and nothing was scheduled.
    assert _fetch(
        seeded_db,
        "SELECT 1 FROM tenant_registry WHERE tprefix = ?",
        (IDENTITY["domain"],),
    ) == []
    assert _fetch(
        seeded_db,
        "SELECT 1 FROM tenant_landline WHERE tprefix = ?",
        (IDENTITY["landline"],),
    ) == []
    assert _fetch(seeded_db, "SELECT 1 FROM dispatcher WHERE setid IN (14, 34)") == []
    assert _fetch(
        seeded_db,
        "SELECT 1 FROM trusted WHERE tag = ?",
        (IDENTITY["domain"],),
    ) == []
    assert worker.calls == []
    assert worker.flushes == 0


# ---------------------------------------------------------------------------
# TLS-SAN notice follows CONSOLE_TLS_LIVE
# ---------------------------------------------------------------------------
def test_wizard_review_shows_tls_notice_when_tls_is_live(
    seeded_db, auth_client, app, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        settings_module,
        "settings",
        replace(
            settings_module.settings,
            db_path=str(seeded_db),
            sidecar_dir=str(tmp_path / "console-data"),
            tls_live=True,
        ),
    )
    review, _payload = _to_review(auth_client)

    assert "TLS is live" in review.text
    assert "ACME_DOMAINS" in review.text
    assert "ACME_FORCE_ISSUE" in review.text
    assert "README onboarding step 4" in review.text
    assert IDENTITY["domain"] in review.text


def test_wizard_review_omits_tls_notice_when_tls_is_not_live(
    seeded_db, auth_client, app, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        settings_module,
        "settings",
        replace(
            settings_module.settings,
            db_path=str(seeded_db),
            sidecar_dir=str(tmp_path / "console-data"),
            tls_live=False,
        ),
    )
    review, _payload = _to_review(auth_client)

    assert "ACME_DOMAINS" not in review.text
    assert "README onboarding step 4" not in review.text


# ---------------------------------------------------------------------------
# validation errors: 422 summary, links, preserved values, no writes
# ---------------------------------------------------------------------------
def test_wizard_step1_validation_error_renders_summary_and_preserves_values(
    seeded_db, auth_client, app
):
    worker = RecordingWorker()
    app.state.worker = worker

    bad = dict(IDENTITY, domain="sip.beta.example.com")
    response = auth_client.post("/wizard/step1", data=bad)

    assert response.status_code == 422
    page = " ".join(html.unescape(response.text).split())
    assert "need" in page and "fixing before this step can continue" in page
    assert 'href="#field-domain"' in response.text
    assert "The domain must end with" in page
    assert f'value="{IDENTITY["landline"]}"' in response.text
    assert _fetch(
        seeded_db,
        "SELECT 1 FROM tenant_registry WHERE tprefix = ?",
        (bad["domain"],),
    ) == []
    assert worker.calls == []


# ---------------------------------------------------------------------------
# fragments (HTMX) vs full pages
# ---------------------------------------------------------------------------
def test_wizard_every_step_is_a_fragment_for_htmx(
    seeded_db, auth_client, app, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        settings_module,
        "settings",
        replace(
            settings_module.settings,
            db_path=str(seeded_db),
            sidecar_dir=str(tmp_path / "console-data"),
        ),
    )
    app.state.worker = RecordingWorker()

    start = auth_client.get("/wizard", headers={"HX-Request": "true"})
    assert start.status_code == 200
    assert _fragment_only(start.text)
    assert 'id="wizard-shell"' in start.text

    first = auth_client.post(
        "/wizard/step1",
        data=IDENTITY,
        headers={"HX-Request": "true"},
    )
    assert first.status_code == 200
    assert _fragment_only(first.text)
    assert 'id="field-lan_ip"' in first.text

    second = auth_client.post(
        "/wizard/step2",
        data={**NETWORK, **_hidden(first.text)},
        headers={"HX-Request": "true"},
    )
    assert second.status_code == 200
    assert _fragment_only(second.text)
    assert "About to be applied" in second.text

    applied = auth_client.post(
        "/wizard/apply",
        data={**IDENTITY, **NETWORK, **_hidden(second.text)},
        headers={"HX-Request": "true"},
    )
    assert applied.status_code == 200
    assert _fragment_only(applied.text)
    assert "all coherence checks are green" in html.unescape(applied.text)


# ---------------------------------------------------------------------------
# auth + CSRF
# ---------------------------------------------------------------------------
def test_wizard_requires_login(client):
    for path in (
        "/wizard",
        "/wizard/verify/sip.alpha.home.example.net",
    ):
        response = client.get(path)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"


def test_wizard_post_without_csrf_header_is_403(seeded_db, auth_client):
    auth_client.headers.pop("X-CSRF-Token", None)
    response = auth_client.post("/wizard/step1", data=IDENTITY)
    assert response.status_code == 403
    assert _fetch(
        seeded_db,
        "SELECT 1 FROM tenant_registry WHERE tprefix = ?",
        (IDENTITY["domain"],),
    ) == []


def test_wizard_rejects_cross_home_landline_prefix(seeded_db, auth_client):
    payload = dict(IDENTITY, landline="0111")
    response = auth_client.post("/wizard/step1", data=payload)
    assert response.status_code == 422
    assert "shadow" in response.text.lower()


# ---------------------------------------------------------------------------
# domain input modes
# ---------------------------------------------------------------------------
def test_wizard_get_offers_domain_modes(seeded_db, auth_client):
    response = auth_client.get("/wizard")
    assert response.status_code == 200
    assert 'name="domain_mode"' in response.text
    assert 'value="suffix"' in response.text
    assert 'value="custom"' in response.text
    assert "home.example.net" in response.text


def test_wizard_accepts_subdomain_and_base_domain(seeded_db, auth_client):
    payload = {
        "domain_mode": "suffix",
        "domain_subdomain": "sip.echo",
        "domain_base": "home.example.net",
        "landline": "06666666",
        "ring_group": "home",
        "label": "Home echo",
    }
    response = auth_client.post("/wizard/step1", data=payload)
    assert response.status_code == 200, response.text
    assert 'id="field-lan_ip"' in response.text
    assert "sip.echo.home.example.net" in response.text
