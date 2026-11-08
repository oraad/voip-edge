"""API tests for the simplified Number rules page."""
from __future__ import annotations

import html
import re
import sqlite3
from dataclasses import replace
from urllib.parse import unquote

from console import number_rules, settings as settings_module


def _text(page: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html.unescape(page)).split())


def test_number_rules_has_three_tabs_and_advanced_link(seeded_db, auth_client):
    page = auth_client.get("/number-rules")
    text = _text(page.text)
    assert page.status_code == 200
    assert "Emergency" in text
    assert "Blocked" in text
    assert "Custom" in text
    assert 'href="/tables/dialplan"' in page.text
    assert 'href="/number-rules?tab=blocked"' in page.text


def test_number_rules_labels_referenced_partitions(seeded_db, auth_client):
    page = auth_client.get("/number-rules")
    text = _text(page.text)
    assert "Partition 10" in text
    assert "Emergency" in text
    assert "sip.alpha.home.example.net" in text
    assert "Partition 90" in text
    assert "Blocked" in text


def test_number_rules_requires_login(client):
    response = client.get("/number-rules")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


class RecordingWorker:
    def __init__(self):
        self.calls = []

    def enqueue(self, table, rpc_name, params):
        self.calls.append((table, rpc_name, list(params)))


def _rows(path, pattern):
    conn = sqlite3.connect(path)
    rows = conn.execute(
        "SELECT dpid, pr, match_exp, attrs FROM dialplan WHERE match_exp = ?",
        (pattern,),
    ).fetchall()
    conn.close()
    return rows


def test_add_emergency_numbers_writes_one_row(seeded_db, auth_client, app):
    worker = RecordingWorker()
    app.state.worker = worker
    response = auth_client.post(
        "/number-rules/emergency",
        data={"dpid": "10", "numbers": "112\n911", "description": "Emergency"},
    )
    assert response.status_code == 303, response.text
    assert _rows(seeded_db, "^(112|911)$") == [
        (10, 1, "^(112|911)$", "Emergency")
    ]
    assert ("dialplan", "dialplan.reload", []) in worker.calls


def test_add_blocked_prefix_writes_unanchored_end(seeded_db, auth_client, app):
    app.state.worker = RecordingWorker()
    response = auth_client.post(
        "/number-rules/blocked",
        data={"dpid": "90", "mode": "prefix", "numbers": "0900\n0901"},
    )
    assert response.status_code == 303, response.text
    assert _rows(seeded_db, "^(0900|0901)")[0][0] == 90


def test_add_blocked_range_uses_the_range_pattern(seeded_db, auth_client, app):
    app.state.worker = RecordingWorker()
    response = auth_client.post(
        "/number-rules/blocked",
        data={
            "dpid": "90",
            "mode": "range",
            "range_start": "1234",
            "range_end": "1245",
        },
    )
    assert response.status_code == 303, response.text
    rows = _rows(seeded_db, number_rules.range_pattern("1234", "1245"))
    assert len(rows) == 1


def test_duplicate_rule_is_rejected(seeded_db, auth_client, app):
    app.state.worker = RecordingWorker()
    data = {"dpid": "10", "numbers": "112"}
    assert auth_client.post("/number-rules/emergency", data=data).status_code == 303
    response = auth_client.post("/number-rules/emergency", data=data)
    assert response.status_code == 422
    assert len(_rows(seeded_db, "^112$")) == 1


def test_custom_unreferenced_partition_warns_but_saves(seeded_db, auth_client, app):
    app.state.worker = RecordingWorker()
    response = auth_client.post(
        "/number-rules/custom",
        data={"dpid": "55", "priority": "1", "pattern": "^777$"},
    )
    assert response.status_code == 303
    assert "warn=" in response.headers["location"]
    assert len(_rows(seeded_db, "^777$")) == 1


def test_readonly_blocks_number_rule_writes(
    seeded_db, auth_client, app, monkeypatch
):
    monkeypatch.setattr(
        settings_module,
        "settings",
        replace(settings_module.settings, readonly=True),
    )
    response = auth_client.post(
        "/number-rules/emergency", data={"dpid": "10", "numbers": "112"}
    )
    assert response.status_code == 403
    assert _rows(seeded_db, "^112$") == []


def test_preview_returns_generated_pattern(auth_client):
    response = auth_client.get(
        "/number-rules/preview",
        params={"kind": "emergency", "numbers": "112, 911"},
    )
    assert response.status_code == 200
    assert "^(112|911)$" in response.text


def test_emergency_form_can_create_the_first_partition(empty_db, auth_client, app):
    app.state.worker = RecordingWorker()

    page = auth_client.get("/number-rules")
    assert 'name="dpid" type="number"' in page.text
    assert 'value="10"' in page.text

    response = auth_client.post(
        "/number-rules/emergency",
        data={"dpid": "10", "numbers": "112"},
    )
    assert response.status_code == 303, response.text
    assert "not assigned as an emergency partition" in unquote(
        response.headers["location"]
    )
    assert len(_rows(empty_db, "^112$")) == 1
