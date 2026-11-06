"""Regression tests for the third review round (v0.1.10)."""
from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from conftest import login
from console import capabilities, descriptors, settings as settings_module, system_ui, wizard_ui
from console.app import create_app


def test_vendored_static_assets_are_served(client):
    for path in (
        "/static/pico.min.css",
        "/static/htmx.min.js",
        "/static/app.js",
    ):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.content


def test_chunked_body_is_capped_at_the_stream_level(client):
    def chunks():
        for _ in range(4):
            yield b"x" * 500_000

    response = client.post(
        "/login",
        content=chunks(),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 413


def test_corrupt_packed_row_edit_returns_404(seeded_db, auth_client):
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

    response = auth_client.get(
        "/tables/tenant_registry/sip.corrupt.home.example.net/edit"
    )
    assert response.status_code == 404
    assert "corrupt" in response.text.lower()


def test_corrupt_packed_row_is_skipped_on_homes(seeded_db, auth_client):
    import sqlite3

    conn = sqlite3.connect(str(seeded_db))
    try:
        conn.execute(
            "INSERT INTO tenant_registry (tprefix, tvalue) VALUES (?, ?)",
            ("sip.corrupt2.home.example.net", "T9,11"),
        )
        conn.commit()
    finally:
        conn.close()

    response = auth_client.get("/homes")
    assert response.status_code == 200
    assert "corrupt row" in response.text.lower()
    assert "sip.corrupt2.home.example.net" not in response.text


def test_readonly_ui_disables_writes(seeded_db, client, monkeypatch):
    monkeypatch.setattr(
        settings_module,
        "settings",
        replace(settings_module.settings, readonly=True, sweeper_interval=0),
    )
    login(client)

    form = client.get("/tables/tenant_registry/sip.alpha.home.example.net/edit")
    assert form.status_code == 200
    assert 'type="submit" disabled' in form.text

    listing = client.get("/tables/dispatcher")
    assert "+ New row" not in listing.text

    detail = client.get("/tables/tenant_registry/sip.alpha.home.example.net")
    assert ">Edit</a>" not in detail.text


def test_status_prefers_fresh_capability_cache(monkeypatch):
    app = SimpleNamespace(
        state=SimpleNamespace(capabilities={"reachable": True, "ok": True})
    )
    monkeypatch.setattr(
        capabilities,
        "cached",
        lambda **kwargs: {"reachable": False, "unknown": True, "ok": False},
    )
    status = system_ui.build_status(app)
    assert status["edge"] == "unreachable"


def test_lifespan_calls_worker_shutdown(monkeypatch):
    monkeypatch.setattr(
        settings_module,
        "settings",
        replace(settings_module.settings, sweeper_interval=0),
    )
    app = create_app()

    class Recorder:
        called = False

        def shutdown(self, *args, **kwargs):
            self.called = True

    recorder = Recorder()
    with TestClient(app):
        app.state.worker = recorder
    assert recorder.called is True


def test_sweeper_calls_resume_and_reissue_once_wired(monkeypatch, tmp_path):
    from console import lifecycle
    from console import sidecar as sidecar_module

    (tmp_path / "state").mkdir()
    monkeypatch.setattr(
        settings_module,
        "settings",
        replace(
            settings_module.settings,
            sweeper_interval=1,
            sidecar_dir=str(tmp_path / "state"),
            db_path=str(tmp_path / "edge.sqlite"),
        ),
    )
    calls: list[tuple[str, tuple, dict]] = []
    monkeypatch.setattr(
        lifecycle,
        "resume_pending",
        lambda *args, **kwargs: calls.append(("resume", args, kwargs)),
    )
    monkeypatch.setattr(
        sidecar_module,
        "reissue_pending_reloads",
        lambda *args, **kwargs: calls.append(("reissue", args, kwargs)),
    )

    app = create_app()
    with TestClient(app):
        deadline = time.time() + 3.0
        while time.time() < deadline and not any(
            call[0] == "reissue" for call in calls
        ):
            time.sleep(0.05)

    assert any(call[0] == "resume" for call in calls)
    assert any(call[0] == "reissue" for call in calls)
    resume = next(call for call in calls if call[0] == "resume")
    assert "backup_dir" in resume[2]
    import threading

    assert not any(
        thread.name == "console-sweeper" and thread.is_alive()
        for thread in threading.enumerate()
    )


def test_wizard_warning_ignores_unrelated_worker_errors():
    class Worker:
        errors = {"tenant_registry": RuntimeError("old failure")}
        flush_timeout = None

        def enqueue(self, *args, **kwargs):
            pass

        def flush(self, timeout=2.0):
            self.flush_timeout = timeout
            return True

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(worker=Worker())))
    keys, warnings = wizard_ui._enqueue_reloads(request, descriptors.load())
    assert keys
    assert warnings == []
    assert request.app.state.worker.flush_timeout == 30.0


def test_oidc_session_is_rejected_in_token_mode(client, monkeypatch):
    from console import security

    monkeypatch.setattr(
        settings_module,
        "settings",
        replace(settings_module.settings, auth_mode="token", sweeper_interval=0),
    )
    token = security.sign_session(
        {"sub": "idp#operator", "csrf": "csrf-value", "amr": "oidc"}
    )
    client.cookies.set(security.SESSION_COOKIE, token)
    client.cookies.set(security.CSRF_COOKIE, "csrf-value")
    response = client.get("/homes")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_readonly_empty_homes_hides_onboarding_cta(
    empty_db, client, monkeypatch
):
    monkeypatch.setattr(
        settings_module,
        "settings",
        replace(settings_module.settings, readonly=True, sweeper_interval=0),
    )
    login(client)
    response = client.get("/homes")
    assert response.status_code == 200
    assert 'href="/wizard"' not in response.text
    assert "read-only mode" in response.text.lower()


def test_table_list_with_missing_edge_db_is_first_use(client, monkeypatch, tmp_path):
    monkeypatch.setattr(
        settings_module,
        "settings",
        replace(
            settings_module.settings,
            db_path=str(tmp_path / "does-not-exist.sqlite"),
            sweeper_interval=0,
        ),
    )
    login(client)
    response = client.get("/tables/dispatcher")
    assert response.status_code == 200
    assert "No rows in" in response.text


def _rendered_inputs(text: str) -> dict[str, str]:
    import html
    import re

    values: dict[str, str] = {}
    for tag in re.findall(r"<input\b[^>]*>", text):
        name = re.search(r'name="([^"]*)"', tag)
        value = re.search(r'value="([^"]*)"', tag)
        if name is not None:
            values[html.unescape(name.group(1))] = html.unescape(
                value.group(1) if value is not None else ""
            )
    return values


def test_dialplan_create_form_round_trips_locked_defaults(seeded_db, auth_client):
    page = auth_client.get("/tables/dialplan/new")
    assert page.status_code == 200
    data = _rendered_inputs(page.text)
    assert data["match_op"] == "1"
    assert data["match_len"] == "0"
    data.update(dpid="10", match_exp="^999123$", attrs="from the rendered form")
    response = auth_client.post("/tables/dialplan/new", data=data)
    assert response.status_code == 303, response.text


def test_trusted_create_form_round_trips_locked_defaults(seeded_db, auth_client):
    page = auth_client.get("/tables/trusted/new")
    assert page.status_code == 200
    data = _rendered_inputs(page.text)
    assert data["proto"] == "any"
    data.update(src_ip="100.64.10.99", tag="sip.alpha.home.example.net")
    response = auth_client.post("/tables/trusted/new", data=data)
    assert response.status_code == 303, response.text


def test_dispatcher_create_form_round_trips_numeric_defaults(seeded_db, auth_client):
    page = auth_client.get("/tables/dispatcher/new")
    assert page.status_code == 200
    data = _rendered_inputs(page.text)
    assert data["flags"] == "0"
    assert data["priority"] == "0"
    data.update(
        setid="77",
        destination="sip:10.9.9.9:5060;transport=tcp",
        description="from the rendered form",
    )
    response = auth_client.post("/tables/dispatcher/new", data=data)
    assert response.status_code == 303, response.text


def test_schema_mismatch_refuses_writes_and_shows_banner(seeded_db, auth_client, app):
    app.state.descriptor_problems = ["dispatcher: version 3 != expected 4"]
    response = auth_client.post(
        "/tables/dispatcher/new",
        data={
            "setid": "77",
            "destination": "sip:10.9.9.9:5060;transport=tcp",
            "flags": "0",
            "priority": "0",
            "description": "x",
        },
    )
    assert response.status_code == 503
    assert "schema mismatch" in response.text
    page = auth_client.get("/homes")
    assert page.status_code == 200
    assert "schema/version mismatch" in page.text


def test_audit_page_shows_failed_intent_last_error(
    seeded_db, client, monkeypatch, tmp_path
):
    from console.sidecar import Sidecar

    state = tmp_path / "console"
    state.mkdir()
    monkeypatch.setattr(
        settings_module,
        "settings",
        replace(
            settings_module.settings,
            sidecar_dir=str(state),
            sweeper_interval=0,
        ),
    )
    sidecar = Sidecar(state / "console.sqlite")
    intent = sidecar.create_intent("park", "sip.alpha.home.example.net")
    sidecar.fail_intent(intent, "refusing to widen the park/destroy scope")

    login(client)
    response = client.get("/system/audit")
    assert response.status_code == 200
    assert "Lifecycle errors" in response.text
    assert "refusing to widen" in response.text


def test_destroy_confirm_does_not_create_a_missing_edge_db(
    client, monkeypatch, tmp_path
):
    db_path = tmp_path / "absent.sqlite"
    monkeypatch.setattr(
        settings_module,
        "settings",
        replace(
            settings_module.settings,
            db_path=str(db_path),
            sidecar_dir=str(tmp_path / "console"),
            sweeper_interval=0,
        ),
    )
    login(client)
    response = client.get("/homes/sip.alpha.home.example.net/destroy")
    assert response.status_code == 200
    assert not db_path.exists()


def test_sweeper_skips_mutations_in_readonly_mode(monkeypatch, tmp_path):
    from console import lifecycle

    (tmp_path / "state").mkdir()
    monkeypatch.setattr(
        settings_module,
        "settings",
        replace(
            settings_module.settings,
            readonly=True,
            sweeper_interval=1,
            sidecar_dir=str(tmp_path / "state"),
            db_path=str(tmp_path / "edge.sqlite"),
        ),
    )
    calls: list = []
    monkeypatch.setattr(
        lifecycle, "resume_pending", lambda *args, **kwargs: calls.append(args)
    )
    app = create_app()
    with TestClient(app):
        time.sleep(1.3)
    assert calls == []


def test_startup_db_lock_is_deferred_not_a_schema_mismatch(seeded_db, monkeypatch):
    import sqlite3

    from console import db as db_module

    real_connect = db_module.connect
    first = {"called": False}

    def flaky_connect(path):
        if not first["called"]:
            first["called"] = True
            raise sqlite3.OperationalError("database is locked")
        return real_connect(path)

    monkeypatch.setattr(db_module, "connect", flaky_connect)
    app = create_app()
    with TestClient(app):
        assert app.state.descriptor_problems == []
