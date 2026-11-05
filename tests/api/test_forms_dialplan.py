"""API tests for the dialplan create form (T4.7, plan §5.2/§6).

The dialplan family adds only named validators, so these tests exercise the
same generic router as T4.1 and assert the operator-visible split:

* a valid match-only row saves and enqueues exactly one reload;
* a bad regex is a pre-write 422 naming the compile error;
* tampering with the locked identity rewrite is a 422 naming the invariant;
* an unreferenced partition saves with a warning (warnings never block).
"""
from __future__ import annotations

import html
import sqlite3
from urllib.parse import parse_qs, urlparse

BASE = {
    "dpid": "10",
    "pr": "2",
    "match_op": "1",
    "match_exp": "^112$",
    "match_len": "0",
    "subst_exp": "^(.*)$",
    "repl_exp": "\\1",
    "attrs": "emergency test",
}


class RecordingWorker:
    """Minimal ReloadWorker double: records the exact enqueue contract."""

    def __init__(self):
        self.calls: list[tuple[str, str, list]] = []

    def enqueue(self, table: str, rpc_name: str, params: list) -> None:
        self.calls.append((table, rpc_name, list(params)))


def _squash(text: str) -> str:
    return " ".join(html.unescape(text).split())


def _flash(response) -> str:
    location = response.headers["location"]
    return parse_qs(urlparse(location).query)["flash"][0]


def _row(path, match_exp: str):
    conn = sqlite3.connect(path)
    try:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            'SELECT * FROM "dialplan" WHERE match_exp = ?', (match_exp,)
        ).fetchone()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------
def test_valid_match_only_row_saves_and_enqueues_one_reload(
    seeded_db, auth_client, app
):
    worker = RecordingWorker()
    app.state.worker = worker

    response = auth_client.post("/tables/dialplan/new", data=BASE)

    assert response.status_code == 303, response.text
    flash = _flash(response)
    assert flash.startswith("Saved")
    assert "reload scheduled shortly" in flash
    assert "Warning" not in flash

    row = _row(seeded_db, "^112$")
    assert row is not None
    assert row["dpid"] == 10
    assert row["pr"] == 2
    assert row["match_op"] == 1
    assert row["match_len"] == 0
    assert row["subst_exp"] == "^(.*)$"
    assert row["repl_exp"] == "\\1"
    assert row["attrs"] == "emergency test"

    assert worker.calls == [("dialplan", "dialplan.reload", [])]


# ---------------------------------------------------------------------------
# pre-write regex error
# ---------------------------------------------------------------------------
def test_bad_regex_is_422_with_the_compile_message(seeded_db, auth_client, app):
    worker = RecordingWorker()
    app.state.worker = worker

    response = auth_client.post(
        "/tables/dialplan/new", data=dict(BASE, match_exp="(")
    )

    assert response.status_code == 422
    page = _squash(response.text)
    assert "does not compile" in page
    assert 'href="#field-match_exp"' in response.text
    assert 'aria-invalid="true"' in response.text
    # Values are preserved on the re-render.
    assert 'value="^112$"' not in response.text
    assert 'value="("' in response.text

    assert _row(seeded_db, "(") is None
    assert worker.calls == []


def test_pcre_only_pattern_saves_with_the_approximate_check_warning(
    seeded_db, auth_client, app
):
    app.state.worker = RecordingWorker()

    # (?<name>…) is PCRE named-group syntax; Python's re rejects it, but the
    # existing regex_compiles rule downgrades PCRE-only syntax to a warning.
    pattern = "(?<area>112)"
    response = auth_client.post(
        "/tables/dialplan/new", data=dict(BASE, match_exp=pattern)
    )

    assert response.status_code == 303, response.text
    flash = _flash(response)
    assert "Saved" in flash
    assert "Warning" in flash
    assert "PCRE-only" in flash
    assert "approximate" in flash
    assert _row(seeded_db, pattern) is not None


# ---------------------------------------------------------------------------
# locked invariants
# ---------------------------------------------------------------------------
def test_tampered_identity_rewrite_is_422_naming_the_invariant(
    seeded_db, auth_client, app
):
    worker = RecordingWorker()
    app.state.worker = worker

    response = auth_client.post(
        "/tables/dialplan/new",
        data=dict(BASE, subst_exp="^(.*)x$", repl_exp="\\1x"),
    )

    assert response.status_code == 422
    page = _squash(response.text)
    assert "identity rewrite by design" in page.lower()
    assert "matching only" in page.lower()
    assert 'href="#field-subst_exp"' in response.text
    assert _row(seeded_db, "^112$") is None
    assert worker.calls == []


def test_non_regex_match_op_is_422(seeded_db, auth_client, app):
    app.state.worker = RecordingWorker()

    response = auth_client.post(
        "/tables/dialplan/new", data=dict(BASE, match_op="0")
    )

    assert response.status_code == 422
    page = _squash(response.text)
    assert "regex matching only in this console" in page
    assert _row(seeded_db, "^112$") is None


def test_non_zero_match_len_is_422(seeded_db, auth_client, app):
    app.state.worker = RecordingWorker()

    response = auth_client.post(
        "/tables/dialplan/new", data=dict(BASE, match_len="4")
    )

    assert response.status_code == 422
    page = _squash(response.text)
    assert "0" in page
    assert _row(seeded_db, "^112$") is None


# ---------------------------------------------------------------------------
# unreferenced partition: warning saves
# ---------------------------------------------------------------------------
def test_unreferenced_dpid_saves_with_a_warning(seeded_db, auth_client, app):
    worker = RecordingWorker()
    app.state.worker = worker
    pattern = "^77[0-9]{4}$"

    response = auth_client.post(
        "/tables/dialplan/new", data=dict(BASE, dpid="77", match_exp=pattern)
    )

    assert response.status_code == 303, response.text
    flash = _flash(response)
    assert "Saved" in flash
    assert "Warning" in flash
    assert "not referenced" in flash.lower()
    assert "77" in flash

    row = _row(seeded_db, pattern)
    assert row is not None
    assert row["dpid"] == 77
    assert worker.calls == [("dialplan", "dialplan.reload", [])]
