"""API/form tests for the tenant_registry rule family (T4.2/T4.3).

These exercise the same descriptor-driven form router with the family rules
active: blocking errors re-render 422 with imperative copy and preserved
values, while the convention nudge saves and shows its warning.
"""
from __future__ import annotations

import html
import sqlite3

from tests.fixtures import seeddb

FIELDS = [
    "tprefix",
    "tenantid",
    "ds_onnet",
    "ds_pstn",
    "cli",
    "label",
    "dpid",
    "flags",
]

BASE = {
    "tprefix": "sip.d-example.home.example.net",
    "tenantid": "T5",
    "ds_onnet": "13",
    "ds_pstn": "33",
    "cli": "07730000",
    "label": "07730000",
    "dpid": "10",
    "flags": "1+1+90",
}


class RecordingWorker:
    def __init__(self):
        self.calls: list[tuple[str, str, list]] = []

    def enqueue(self, table: str, rpc_name: str, params: list) -> None:
        self.calls.append((table, rpc_name, list(params)))


def _squash(text: str) -> str:
    return " ".join(html.unescape(text).split())


def _row(path, key: str):
    conn = sqlite3.connect(path)
    try:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM tenant_registry WHERE tprefix = ?", (key,)
        ).fetchone()
    finally:
        conn.close()


def _post(auth_client, payload, *, hx: bool = False):
    headers = {"HX-Request": "true"} if hx else None
    return auth_client.post(
        "/tables/tenant_registry/new", data=payload, headers=headers
    )


# ---------------------------------------------------------------------------
# happy path stays green with the new rules active
# ---------------------------------------------------------------------------
def test_happy_path_with_rules_active_writes_and_enqueues(seeded_db, auth_client, app):
    worker = RecordingWorker()
    app.state.worker = worker

    response = _post(auth_client, BASE)

    assert response.status_code == 303, response.text
    row = _row(seeded_db, BASE["tprefix"])
    assert row is not None
    assert row["tvalue"] == "T5,13,33,07730000,07730000,10,1+1+90"
    assert worker.calls == [("tenant_registry", "mtree.reload", ["tenantdom"])]


# ---------------------------------------------------------------------------
# T4.3 cross-refs: missing set / dpid / block partition
# ---------------------------------------------------------------------------
def test_missing_onnet_set_is_422_with_imperative_summary_and_values(
    seeded_db, auth_client, app
):
    worker = RecordingWorker()
    app.state.worker = worker
    payload = dict(BASE, ds_onnet="99")

    response = _post(auth_client, payload)

    assert response.status_code == 422
    page = _squash(response.text)
    assert "Choose an existing on-net route set, or create set 99" in page
    assert "Route sets first" in page
    assert 'href="#field-ds_onnet"' in response.text
    # Every value survives the re-render, not just the offending field.
    for name in ("tprefix", "tenantid", "cli", "label", "flags"):
        assert f'value="{payload[name]}"' in response.text, name
    assert 'value="99"' in response.text
    assert _row(seeded_db, BASE["tprefix"]) is None
    assert worker.calls == []


def test_missing_dpid_is_422_with_imperative_summary(seeded_db, auth_client, app):
    worker = RecordingWorker()
    app.state.worker = worker
    payload = dict(BASE, tenantid="T6", cli="07730001", label="07730001", dpid="99")

    response = _post(auth_client, payload)

    assert response.status_code == 422
    page = _squash(response.text)
    assert "Choose an existing dialplan partition, or create partition 99" in page
    assert "Number rules first" in page
    assert 'href="#field-dpid"' in response.text
    assert 'value="99"' in response.text
    assert _row(seeded_db, payload["tprefix"]) is None
    assert worker.calls == []


def test_missing_block_dpid_is_422_naming_the_partition(seeded_db, auth_client, app):
    worker = RecordingWorker()
    app.state.worker = worker
    payload = dict(
        BASE, tenantid="T7", cli="07730002", label="07730002", flags="1+1+91"
    )

    response = _post(auth_client, payload)

    assert response.status_code == 422
    page = _squash(response.text)
    assert "Blocked-destination partition 91 does not exist" in page
    assert "Number rules first" in page
    assert 'href="#field-flags"' in response.text
    assert 'value="1+1+91"' in response.text
    assert _row(seeded_db, payload["tprefix"]) is None
    assert worker.calls == []


# ---------------------------------------------------------------------------
# T4.2 uniqueness / CLI shadowing
# ---------------------------------------------------------------------------
def test_duplicate_tenantid_is_422_naming_the_existing_home(seeded_db, auth_client, app):
    worker = RecordingWorker()
    app.state.worker = worker
    payload = dict(
        BASE, tenantid="T1", cli="07730003", label="07730003"
    )

    response = _post(auth_client, payload)

    assert response.status_code == 422
    page = _squash(response.text)
    assert "Tenant id T1 is already used by sip.alpha.home.example.net" in page
    assert 'href="#field-tenantid"' in response.text
    assert _row(seeded_db, payload["tprefix"]) is None
    assert worker.calls == []


def test_cli_collision_with_another_home_is_422(seeded_db, auth_client, app):
    worker = RecordingWorker()
    app.state.worker = worker
    payload = dict(BASE, tenantid="T8", cli="02222222", label="02222222")

    response = _post(auth_client, payload)

    assert response.status_code == 422
    page = _squash(response.text)
    assert "already the landline of T2" in page
    assert 'href="#field-cli"' in response.text
    assert 'value="02222222"' in response.text
    assert _row(seeded_db, payload["tprefix"]) is None
    assert worker.calls == []


# ---------------------------------------------------------------------------
# convention nudge: warning saves, row written
# ---------------------------------------------------------------------------
def test_convention_warning_saves_and_shows_in_the_toast(seeded_db, auth_client, app):
    worker = RecordingWorker()
    app.state.worker = worker
    # PSTN set 31 means N=1, so on-net 12 is outside 10+N (expected 11).
    payload = dict(
        BASE,
        tprefix="sip.e-example.home.example.net",
        tenantid="T8",
        ds_onnet="12",
        ds_pstn="31",
        cli="07740000",
        label="07740000",
    )

    response = _post(auth_client, payload, hx=True)

    assert response.status_code == 200, response.text
    page = _squash(response.text)
    assert "Saved" in page
    assert "Warning" in page
    assert "10+N" in page
    assert "expected set 11" in page

    row = _row(seeded_db, payload["tprefix"])
    assert row is not None
    assert row["tvalue"] == "T8,12,31,07740000,07740000,10,1+1+90"
    assert worker.calls == [("tenant_registry", "mtree.reload", ["tenantdom"])]


def test_missing_set_blocks_and_shows_no_toast_warning(seeded_db, auth_client, app):
    worker = RecordingWorker()
    app.state.worker = worker
    payload = dict(
        BASE,
        tprefix="sip.f-example.home.example.net",
        tenantid="T9",
        ds_onnet="999",
        cli="07750000",
        label="07750000",
    )

    response = _post(auth_client, payload, hx=True)

    assert response.status_code == 422
    assert "reload scheduled shortly" not in _squash(response.text)
    assert _row(seeded_db, payload["tprefix"]) is None
    assert worker.calls == []


def test_tenant_family_does_not_add_table_code_to_the_forms_router():
    from pathlib import Path

    from console import forms_ui

    source = Path(forms_ui.__file__).read_text(encoding="utf-8")
    for table in seeddb.TABLE_NAMES:
        assert table not in source, table
