"""T9 API tests — audit & backups page, capability banner, status strip.

All fixtures use temporary sidecar/backup directories; no test touches the
live edge.  Pages must be authenticated and must render even when the sidecar
or RPC is unavailable.
"""
from __future__ import annotations

import os
import re
from dataclasses import replace
from pathlib import Path

import pytest

from console import capabilities, settings as settings_module
from console.sidecar import Sidecar


@pytest.fixture(autouse=True)
def _fresh_capabilities():
    capabilities.clear_cache()
    yield
    capabilities.clear_cache()


@pytest.fixture()
def system_env(tmp_path, monkeypatch):
    sidecar_dir = tmp_path / "console-data"
    sidecar_dir.mkdir()
    sidecar = Sidecar(sidecar_dir / "console.sqlite")
    monkeypatch.setattr(
        settings_module,
        "settings",
        replace(settings_module.settings, sidecar_dir=str(sidecar_dir)),
    )
    return {"dir": sidecar_dir, "sidecar": sidecar, "backups": sidecar_dir / "backups"}


def _seed_audit(sidecar: Sidecar) -> None:
    sidecar.audit("park", "beta.home.example.net", "SCHEDULED", "waiting for window")
    sidecar.audit("park", "beta.home.example.net", "RELOADED", "reload applied")
    sidecar.audit("park", "beta.home.example.net", "DONE", "purge verified")
    sidecar.audit("restore", "gamma.home.example.net", "FAILED", "collision")


def _seed_backups(directory: Path) -> None:
    directory.mkdir()
    tags = [
        ("20261105T090000000000Z", "destroy:legacy-x", 10 * 1024),
        ("20261105T100000000000Z", "restore:alpha", 20 * 1024),
        ("20261105T110000000000Z", "wizard-apply:beta", 30 * 1024),
        ("20261105T120000000000Z", "park:alpha", 40 * 1024),
        ("20261105T130000000000Z", "destroy:gamma", 50 * 1024),
        ("20261105T140000000000Z", "park:beta", 72 * 1024),
    ]
    base_ns = 1_000_000_000_000_000_000
    for index, (stamp, tag, size) in enumerate(tags):
        path = directory / f"edge-{stamp}-{tag}.sqlite"
        path.write_bytes(b"x" * size)
        moment = base_ns + index * 1_000_000_000
        os.utime(path, ns=(moment, moment))


def test_system_pages_require_authentication(client):
    for path in ("/system/audit", "/system/backups"):
        response = client.get(path)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"


def test_audit_page_renders_newest_rows_and_outcome_badges(auth_client, system_env):
    _seed_audit(system_env["sidecar"])

    response = auth_client.get("/system/audit")
    assert response.status_code == 200
    text = response.text

    assert "<!doctype" in text.lower()
    assert "Audit &amp; backups" in text
    assert "Audit" in text and "Backups" in text

    # Newest first: the failed restore was inserted last and must render first.
    assert text.index("gamma") < text.index("beta.home.example.net")

    # All four outcome tokens exist as badges somewhere on the page.
    for token in ("scheduled", "applied", "verified", "failed"):
        assert f'class="badge b-' in text
        assert token in text

    # The raw sidecar fields are displayed.
    assert "SCHEDULED" in text and "RELOADED" in text and "DONE" in text
    assert "purge verified" in text
    assert "collision" in text


def test_backups_table_has_purpose_size_and_retention(auth_client, system_env):
    _seed_backups(system_env["backups"])

    response = auth_client.get("/system/audit")
    text = response.text

    assert "before park · beta" in text
    assert "before destroy · legacy-x" in text
    assert "wizard apply · beta" in text
    assert "72 KB" in text
    assert "1 of 5 kept" in text
    assert "6 of 5 — beyond rotation" in text
    # Newest backup (park:beta) is the first purpose row.
    assert text.index("before park · beta") < text.index("before destroy · gamma")


def test_backups_anchor_route_renders_same_tables(auth_client, system_env):
    _seed_audit(system_env["sidecar"])
    _seed_backups(system_env["backups"])

    response = auth_client.get("/system/backups")
    assert response.status_code == 200
    assert 'id="backups"' in response.text
    assert 'id="audit"' in response.text
    assert "before park · beta" in response.text
    assert "purge verified" in response.text


def test_restore_note_and_cli_edits_note_are_explicit(auth_client, system_env):
    response = auth_client.get("/system/audit")
    text = response.text

    assert "stop the stack, replace the file (never hot-swap)" in text
    assert "never safe while the stack is running" in text
    assert "Direct CLI edits of the database do not appear here" in text
    assert "re-reading a table is immediate" in text


def test_audit_page_tolerates_an_absent_sidecar(auth_client, tmp_path, monkeypatch):
    missing = tmp_path / "no-sidecar-here"
    monkeypatch.setattr(
        settings_module,
        "settings",
        replace(settings_module.settings, sidecar_dir=str(missing)),
    )

    response = auth_client.get("/system/audit")
    assert response.status_code == 200
    assert "sidecar unavailable" in response.text
    assert "No audit rows recorded yet" in response.text
    assert "No backups have been taken yet" in response.text


def test_capability_banner_reports_htable_delete_and_refusal(
    auth_client, system_env, monkeypatch
):
    monkeypatch.setattr(
        settings_module,
        "settings",
        replace(settings_module.settings, enforce_capabilities=True),
    )
    monkeypatch.setattr(
        capabilities,
        "cached",
        lambda **kwargs: {
            "core.uptime": True,
            "htable.dump": True,
            "htable.delete": False,
            "mtree.reload": True,
            "dispatcher.reload": True,
            "dialplan.reload": True,
            "permissions.trustedReload": True,
            "jsonrpcs": True,
            "reachable": True,
            "unknown": False,
            "ok": False,
            "aggregate": False,
        },
    )

    response = auth_client.get("/system/audit")
    assert response.status_code == 200
    assert "htable.delete unavailable — park and destroy are refused" in response.text


def test_capability_banner_when_edge_is_unknown(auth_client, system_env, monkeypatch):
    monkeypatch.setattr(capabilities, "cached", lambda **kwargs: None)

    response = auth_client.get("/system/audit")
    assert response.status_code == 200
    assert "capabilities unknown (edge unreachable)" in response.text


def test_status_strip_shows_pending_reload_count(auth_client, system_env):
    sidecar = system_env["sidecar"]
    sidecar.record_pending_reload("tenantdom")
    sidecar.record_pending_reload("dispatcher")

    response = auth_client.get("/homes")
    assert response.status_code == 200
    assert "sidecar unavailable" not in response.text
    assert re.search(
        r"pending reloads:\s*<strong>\s*2\s*</strong>", response.text
    ), response.text


def test_status_strip_degrades_cleanly_when_sidecar_is_unavailable(
    auth_client, tmp_path, monkeypatch
):
    missing = tmp_path / "absent-sidecar"
    monkeypatch.setattr(
        settings_module,
        "settings",
        replace(settings_module.settings, sidecar_dir=str(missing)),
    )

    response = auth_client.get("/homes")
    assert response.status_code == 200
    assert "sidecar unavailable" in response.text
    assert re.search(r"pending reloads:\s*<strong>\s*unknown\s*</strong>", response.text)
    # No invented zero count while the sidecar cannot be read.
    assert not re.search(r"pending reloads:\s*<strong>\s*0\s*</strong>", response.text)


def test_legacy_audit_nav_redirects(auth_client):
    response = auth_client.get("/audit")
    assert response.status_code == 303
    assert response.headers["location"] == "/system/audit"
