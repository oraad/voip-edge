"""Unit tests for the T5 delete guard / reverse-reference scanner.

Plan §5.2 (delete rules), §7.8 and §8 T5.  The scanner is descriptor-driven:
every other descriptor's ``cross_refs`` is read in reverse to discover rows
that still point at the row about to be deleted.  Dispatcher set ids are named
through :func:`console.rules.dispatcher.set_referenced` so the guard reports
tenant domains, not opaque row ids.
"""
from __future__ import annotations

import pytest

from console import db, descriptors, forms_ui
from tests.fixtures import seeddb


def _conn(path):
    return db.connect(path)


def _descriptor(name: str):
    return descriptors.load().tables[name]


def test_scanner_names_tenants_using_a_route_set(tmp_path):
    path = seeddb.seed(tmp_path / "edge.sqlite")
    conn = _conn(path)
    try:
        # The dispatcher primary key is the auto id; id 1 carries setid 11.
        row = conn.execute(
            'SELECT id, setid FROM "dispatcher" WHERE setid = 11'
        ).fetchone()
        refs = forms_ui.scan_references(
            conn, _descriptor("dispatcher"), row["id"]
        )
    finally:
        conn.close()

    labels = [ref.label for ref in refs]
    # Set 11 belongs to alpha; set_referenced names the owning tenant domain.
    assert "sip.alpha.home.example.net" in labels
    assert all(ref.value == "11" for ref in refs)


def test_scanner_finds_landline_and_trusted_rows_referencing_a_home(tmp_path):
    path = seeddb.seed(tmp_path / "edge.sqlite")
    conn = _conn(path)
    try:
        refs = forms_ui.scan_references(
            conn,
            _descriptor("tenant_registry"),
            "sip.alpha.home.example.net",
        )
    finally:
        conn.close()

    by_table = {ref.table: ref for ref in refs}
    # tenant_landline.tenantid -> tenant_registry.tenantid
    assert "tenant_landline" in by_table
    assert "01111111" in by_table["tenant_landline"].label
    # trusted.tag -> tenant_registry.tprefix
    assert "trusted" in by_table
    assert "100.64.10.10" in by_table["trusted"].label


def test_scanner_returns_no_refs_for_a_free_dispatcher_row(tmp_path):
    path = seeddb.seed(tmp_path / "edge.sqlite")
    conn = _conn(path)
    try:
        cursor = conn.execute(
            "INSERT INTO dispatcher "
            "(setid, destination, flags, priority, attrs, description) "
            "VALUES (99, 'sip:10.0.0.10:5060;transport=tcp', 0, 0, '', 'free')"
        )
        conn.commit()
        refs = forms_ui.scan_references(
            conn, _descriptor("dispatcher"), cursor.lastrowid
        )
    finally:
        conn.close()

    assert refs == []


@pytest.mark.parametrize("table", ["tenant_registry", "tenant_landline"])
def test_delete_policy_rejects_lifecycle_tables(table):
    descriptor = _descriptor(table)

    assert forms_ui.delete_is_blocked_by_policy(descriptor) is True
    with pytest.raises(forms_ui.LifecycleDeleteError):
        forms_ui.assert_deletable(descriptor)


def test_delete_policy_allows_blocked_if_refs_tables():
    for table in ("dispatcher", "dialplan", "trusted"):
        descriptor = _descriptor(table)
        assert forms_ui.delete_is_blocked_by_policy(descriptor) is False
        forms_ui.assert_deletable(descriptor)  # does not raise
