"""Unit tests for console.live — pure view builders (T6).

The builders are deliberately FastAPI-free: they take a raw RPC result and
return plain dictionaries, so the flag/transport/domain parsing can be tested
without a request or a socket.
"""
from __future__ import annotations

import pytest

from console.live import build_dispatcher_view, build_regcache_view, edge_health
from console.rpc import UnixDatagramRPC
from tests.fixtures.fakerpc import FakeRPC


@pytest.fixture()
def fake(tmp_path):
    with FakeRPC(tmp_path / "kamailio_rpc.sock") as server:
        yield server


# ---------------------------------------------------------------------------
# regcache
# ---------------------------------------------------------------------------


REGCACHE_DUMP = [
    {
        "entry": 0,
        "size": 2,
        "slot": [
            {
                "item": {
                    "name": "garden@sip.beta.home.example.net",
                    "value": "sip:198.51.100.21:43774;transport=tcp",
                    "expires": 1740,
                }
            },
            {
                "item": {
                    "name": "mobile1@sip.alpha.home.example.net",
                    "value": "sip:198.51.100.20:2416;transport=TLS",
                }
            },
        ],
    },
    # A bucket with exactly one entry returns ``slot`` as a dict, not a list.
    {
        "entry": 1,
        "size": 1,
        "slot": {
            "item": {
                "name": "desk@sip.beta.home.example.net",
                "value": "sip:10.0.6.20:5060",
            }
        },
    },
]


def _by_aor(view: list[dict]) -> dict[str, dict]:
    return {row["aor"]: row for row in view}


def test_regcache_flattens_single_item_slot_and_parses_transports():
    view = build_regcache_view(REGCACHE_DUMP)
    rows = _by_aor(view)

    assert len(view) == 3
    assert rows["garden@sip.beta.home.example.net"]["transport"] == "tcp"
    assert rows["mobile1@sip.alpha.home.example.net"]["transport"] == "tls"
    # No ;transport= on this flow: the documented default is UDP.
    assert rows["desk@sip.beta.home.example.net"]["transport"] == "udp"


def test_regcache_keeps_flow_and_raw_expires_but_adds_no_countdown():
    rows = _by_aor(build_regcache_view(REGCACHE_DUMP))

    garden = rows["garden@sip.beta.home.example.net"]
    assert garden["flow"] == "sip:198.51.100.21:43774;transport=tcp"
    # The raw value is available for diagnostics; expiry is enforced by
    # $shtex/autoexpire, and the view must not invent a countdown field.
    assert garden["expires"] == 1740
    assert "expires_in" not in garden
    assert "countdown" not in garden
    assert rows["mobile1@sip.alpha.home.example.net"]["expires"] is None


def test_regcache_splits_the_aor_domain_at_the_allowed_suffix():
    rows = _by_aor(build_regcache_view(REGCACHE_DUMP))
    mobile = rows["mobile1@sip.alpha.home.example.net"]

    assert mobile["user"] == "mobile1"
    assert mobile["domain"] == "sip.alpha.home.example.net"
    assert mobile["domain_prefix"] == "sip.alpha"
    assert mobile["suffix"] == "home.example.net"


def test_regcache_handles_plain_keys_and_empty_input():
    assert build_regcache_view(None) == []
    assert build_regcache_view([]) == []

    view = build_regcache_view(
        [{"entry": 0, "size": 1, "slot": {"item": {"name": "no-at-sign", "value": "x"}}}]
    )
    assert view[0]["aor"] == "no-at-sign"
    assert view[0]["domain"] == ""
    assert view[0]["suffix"] == ""


# ---------------------------------------------------------------------------
# dispatcher flags
# ---------------------------------------------------------------------------


def _dispatcher_row(flags, setid=11, uri="sip:10.0.0.10:5060"):
    view = build_dispatcher_view(
        [{"SETID": setid, "DEST": {"URI": uri, "FLAGS": flags, "PRIORITY": 0}}]
    )
    assert len(view) == 1
    return view[0]


@pytest.mark.parametrize(
    ("flags", "state", "tone", "probe", "label"),
    [
        ("AP", "active", "ok", "P", "Active · probed"),
        ("AX", "active", "ok", "X", "Active · not probed"),
        ("TP", "trying", "warn", "P", "Trying · last probe: no answer"),
        ("IP", "inactive", "err", "P", "Inactive · last probe: no answer"),
        ("DX", "disabled", "err", "X", "Disabled · not probed"),
    ],
)
def test_dispatcher_flag_corpus(flags, state, tone, probe, label):
    row = _dispatcher_row(flags)

    assert row["setid"] == 11
    assert row["target"] == "sip:10.0.0.10:5060"
    assert row["flags"] == flags
    assert row["state"] == state
    assert row["tone"] == tone
    assert row["probe"] == probe
    assert row["label"] == label
    # v1 has no latency measurement; never invent one.
    assert "latency" not in row


def test_dispatcher_unknown_flags_degrade_to_an_unknown_state():
    row = _dispatcher_row("ZZ")

    assert row["state"] == "unknown"
    assert row["tone"] == "muted"
    assert row["flags"] == "ZZ"
    assert "Unknown" in row["label"]


def test_dispatcher_parses_kamailio_structured_sets():
    result = {
        "NRSETS": 2,
        "RECORDS": [
            {
                "SET": {
                    "ID": 11,
                    "TARGETS": [
                        {
                            "DEST": {
                                "URI": "sip:10.0.0.10:5060",
                                "FLAGS": "AP",
                                "PRIORITY": 0,
                            }
                        }
                    ],
                }
            },
            {
                "SET": {
                    "ID": 12,
                    "TARGETS": [
                        {
                            "DEST": {
                                "URI": "sip:10.0.7.10:5060",
                                "FLAGS": "TP",
                                "PRIORITY": 0,
                            }
                        }
                    ],
                }
            },
        ],
    }

    view = build_dispatcher_view(result)
    assert [row["setid"] for row in view] == [11, 12]
    assert [row["state"] for row in view] == ["active", "trying"]
    assert view[1]["label"] == "Trying · last probe: no answer"


def test_dispatcher_parses_text_output():
    text = (
        "SET: 11 URI: sip:10.0.0.10:5060 FLAGS: AP PRIORITY: 0\n"
        "SET: 12 URI: sip:10.0.7.10:5060 FLAGS: TP PRIORITY: 0\n"
    )

    view = build_dispatcher_view(text)
    assert [row["setid"] for row in view] == [11, 12]
    assert [row["flags"] for row in view] == ["AP", "TP"]
    assert view[1]["state"] == "trying"


def test_dispatcher_parses_multiline_camcmd_text_blocks():
    text = """
SET: 11
DEST: {
        URI: sip:10.0.0.10:5060
        FLAGS: AP
        PRIORITY: 12
}
SET: 12
DEST: {
        URI: sip:10.0.7.10:5060
        FLAGS: TP
        PRIORITY: 0
}
"""

    view = build_dispatcher_view(text)
    assert [row["setid"] for row in view] == [11, 12]
    assert view[0]["label"] == "Active · probed"
    assert view[1]["label"] == "Trying · last probe: no answer"


def test_dispatcher_parses_flat_json_records():
    result = [
        {"SETID": 11, "DEST": {"URI": "sip:10.0.0.10:5060", "FLAGS": "AP"}},
        {"SETID": 12, "DEST": {"URI": "sip:10.0.7.10:5060", "FLAGS": "DX"}},
    ]

    view = build_dispatcher_view(result)
    assert [row["setid"] for row in view] == [11, 12]
    assert [row["state"] for row in view] == ["active", "disabled"]
    assert view[0]["probed"] is True
    assert view[0]["state_token"] == "A"


def test_dispatcher_accepts_empty_inputs():
    assert build_dispatcher_view(None) == []
    assert build_dispatcher_view("") == []
    assert build_dispatcher_view({}) == []


# ---------------------------------------------------------------------------
# edge health
# ---------------------------------------------------------------------------


def test_edge_health_ok(fake):
    fake.script("core.uptime", {"uptime": "6 days, 3 hours, 2 minutes"})

    health = edge_health(fake)
    assert health == {
        "reachable": True,
        "uptime": "6 days, 3 hours, 2 minutes",
    }


def test_edge_health_rpc_fault_is_unreachable(fake):
    fake.fault("core.uptime", code=500, message="core.uptime failed")

    assert edge_health(fake) == {"reachable": False, "uptime": None}


def test_edge_health_timeout_is_unreachable(fake):
    fake.silent("core.uptime")
    rpc = UnixDatagramRPC(fake.sock_path, timeout_ms=50)

    assert edge_health(rpc) == {"reachable": False, "uptime": None}


def test_edge_health_never_raises_on_a_broken_client():
    class Broken:
        def call(self, method, params=None):
            raise RuntimeError("boom")

    assert edge_health(Broken()) == {"reachable": False, "uptime": None}
    assert edge_health(None) == {"reachable": False, "uptime": None}
