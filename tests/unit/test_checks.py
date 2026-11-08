"""Shared check code is re-exported from the wizard for existing imports."""
from __future__ import annotations


def test_wizard_reexports_shared_checks():
    from console import checks, wizard_ui

    assert wizard_ui.verify_checklist is checks.verify_checklist
    assert wizard_ui.build_live_checks is checks.build_live_checks
    assert wizard_ui.trusted_entries is checks.trusted_entries
    assert wizard_ui.dispatcher_target_ok is checks.dispatcher_target_ok
