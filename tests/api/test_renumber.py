"""Renumber guard for cross-referenced values."""
from __future__ import annotations


def test_dispatcher_setid_rename_is_blocked(seeded_db, auth_client):
    # seeded: home sip.alpha uses set 11 (via on-net)
    response = auth_client.get("/tables/dispatcher/1")
    assert response.status_code == 200
    # setid 11 is row id 1; get the current row hash first (stale-edit guard)
    page = auth_client.get("/tables/dispatcher/1/edit")
    import re
    m = re.search(r'name="row_hash" value="([^"]+)"', page.text)
    assert m, page.text
    row_hash = m.group(1)
    # POST rename
    response = auth_client.post(
        "/tables/dispatcher/1/edit",
        data={
            "setid": "99",
            "destination": "sip:10.0.0.10:5060;transport=tcp",
            "flags": "0",
            "priority": "0",
            "attrs": "",
            "description": "test",
            "row_hash": row_hash,
        },
    )
    assert response.status_code == 422
    assert "is used by" in response.text
