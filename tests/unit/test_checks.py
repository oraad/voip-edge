"""Shared check code is re-exported from the wizard for existing imports."""
from __future__ import annotations


def test_wizard_reexports_shared_checks():
    from console import checks, wizard_ui

    assert wizard_ui.verify_checklist is checks.verify_checklist
    assert wizard_ui.build_live_checks is checks.build_live_checks
    assert wizard_ui.trusted_entries is checks.trusted_entries
    assert wizard_ui.dispatcher_target_ok is checks.dispatcher_target_ok


def test_coherence_summaries_treats_no_pstn_as_neutral(tmp_path):
    import sqlite3

    from console import checks
    from tests.fixtures import seeddb

    path = seeddb.seed(tmp_path / "edge.sqlite")
    conn = sqlite3.connect(path)
    conn.execute(
        'UPDATE tenant_registry SET tvalue = ? WHERE tprefix = ?',
        ("T1,11,0,01111111,01111111,10,1+0", "sip.alpha.home.example.net"),
    )
    conn.execute("DELETE FROM dispatcher WHERE setid = 31")
    conn.commit()

    summary = checks.coherence_summaries(
        conn, ["sip.alpha.home.example.net"]
    )["sip.alpha.home.example.net"]
    assert summary["tone"] == "ok"
    assert summary["text"] == "all green"
    assert summary["missing"] == []
