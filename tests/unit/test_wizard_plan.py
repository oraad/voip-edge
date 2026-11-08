"""Unit tests for the onboarding wizard plan + validation aggregation (T7).

The plan is a pure function: given a draft it returns the exact rows the apply
transaction writes, so the 5-row (with trunk) and 4-row (no trunk) shapes are
asserted without a route or a database.  The validation test proves that the
wizard reuses the descriptor rules and aggregates one error per offending
wizard field.
"""
from __future__ import annotations

import sqlite3

from console import descriptors
from console import wizard_ui
from console.wizard_ui import WizardDraft, draft_from_form, plan_writes, validate_step
from tests.fixtures import seeddb

TABLESET = descriptors.load()

DRAFT_WITH_TRUNK = WizardDraft(
    domain="sip.delta.home.example.net",
    landline="04444444",
    ring_group="home",
    label="Home example",
    lan_ip="10.0.6.20",
    wire_ip="100.64.10.20",
    has_pstn=True,
    tenantid="T4",
    ds_onnet=14,
    ds_pstn=34,
)


# ---------------------------------------------------------------------------
# plan_writes — pure row list
# ---------------------------------------------------------------------------
def test_plan_writes_with_trunk_returns_five_rows_in_wire_order():
    writes = plan_writes(DRAFT_WITH_TRUNK, TABLESET)

    assert [table for table, _row in writes] == [
        "tenant_registry",
        "tenant_landline",
        "dispatcher",
        "dispatcher",
        "trusted",
    ]

    registry = writes[0][1]
    assert registry["tprefix"] == "sip.delta.home.example.net"
    assert registry["tvalue"] == "T4,14,34,04444444,Home example,0,1+1"

    landline = writes[1][1]
    assert landline["tprefix"] == "04444444"
    assert landline["tvalue"] == "T4,home,14,1"

    onnet = writes[2][1]
    pstn = writes[3][1]
    assert onnet["setid"] == 14
    assert pstn["setid"] == 34
    assert onnet["destination"] == "sip:10.0.6.20:5060;transport=tcp"
    assert pstn["destination"] == onnet["destination"]
    assert onnet["flags"] == pstn["flags"] == 0

    trusted = writes[4][1]
    assert trusted == {
        "src_ip": "100.64.10.20",
        "proto": "any",
        "from_pattern": "",
        "ruri_pattern": "",
        "tag": "sip.delta.home.example.net",
        "priority": 0,
    }


def test_plan_writes_without_trunk_returns_four_rows_and_skips_pstn_set():
    draft = WizardDraft(
        **{
            **DRAFT_WITH_TRUNK.__dict__,
            "has_pstn": False,
            "ds_pstn": 0,
        }
    )
    writes = plan_writes(draft, TABLESET)

    assert [table for table, _row in writes] == [
        "tenant_registry",
        "tenant_landline",
        "dispatcher",
        "trusted",
    ]
    assert writes[0][1]["tvalue"] == "T4,14,0,04444444,Home example,0,1+0"
    assert writes[2][1]["setid"] == 14
    assert all(row["setid"] != 34 for table, row in writes if table == "dispatcher")


def test_plan_writes_requires_derived_ids():
    incomplete = WizardDraft(
        domain="sip.delta.home.example.net",
        landline="04444444",
        ring_group="home",
        label="Home example",
    )
    try:
        plan_writes(incomplete, TABLESET)
    except ValueError as exc:
        assert "tenant id" in str(exc)
    else:  # pragma: no cover - the assertion above is the contract
        raise AssertionError("plan_writes accepted a draft with no derived ids")


# ---------------------------------------------------------------------------
# validation aggregation — descriptor rules, per wizard field
# ---------------------------------------------------------------------------
def _seeded_connection(tmp_path):
    path = seeddb.seed(tmp_path / "edge.sqlite")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def test_validate_step1_aggregates_errors_per_wizard_field(tmp_path):
    conn = _seeded_connection(tmp_path)
    try:
        bad = WizardDraft(
            domain="sip.beta.example.com",  # not an allowed suffix
            landline="07x",  # not digits
            ring_group="home,1",  # packing separator
            label="B,Example",  # packing separator
            tenantid="T4",
            ds_onnet=14,
            ds_pstn=34,
        )
        errors, _warnings = validate_step(1, bad, conn, TABLESET)
    finally:
        conn.close()

    assert set(errors) >= {"domain", "landline", "ring_group", "label"}
    for field in ("domain", "landline", "ring_group", "label"):
        assert errors[field], field
    assert any("end with" in message for message in errors["domain"])
    assert any("digits" in message.lower() for message in errors["landline"])


def test_validate_step1_accepts_the_planned_sets_and_domain(tmp_path):
    conn = _seeded_connection(tmp_path)
    try:
        errors, _warnings = validate_step(1, DRAFT_WITH_TRUNK, conn, TABLESET)
    finally:
        conn.close()

    # The two dispatcher sets and the trusted tag are written by the same
    # transaction, so they must not produce a false "create it first" error.
    assert errors == {}


def test_validate_step2_rejects_a_missing_or_bad_network(tmp_path):
    conn = _seeded_connection(tmp_path)
    try:
        bad = WizardDraft(
            **{
                **DRAFT_WITH_TRUNK.__dict__,
                "lan_ip": "voip.beta.internal",  # FQDN, not a literal IPv4
                "wire_ip": "8.8.8.8",  # public, no override ticked
            }
        )
        errors, _warnings = validate_step(2, bad, conn, TABLESET)
    finally:
        conn.close()

    assert errors["lan_ip"]
    assert errors["wire_ip"]
    assert any("literal IPv4" in message for message in errors["lan_ip"])
    assert any("wire-observed" in message for message in errors["wire_ip"])


# ---------------------------------------------------------------------------
# domain input modes
# ---------------------------------------------------------------------------
def test_draft_from_form_composes_subdomain_and_base_domain():
    draft = draft_from_form(
        {
            "domain_mode": ["suffix"],
            "domain_subdomain": ["sip.echo"],
            "domain_base": ["home.example.net"],
        }
    )
    assert draft.domain == "sip.echo.home.example.net"
    assert draft.domain_mode == "suffix"
    assert draft.domain_subdomain == "sip.echo"
    assert draft.domain_base == "home.example.net"


def test_draft_from_form_custom_domain_wins():
    draft = draft_from_form(
        {
            "domain_mode": ["custom"],
            "domain": ["sip.delta.home.example.net"],
            "domain_subdomain": ["ignored"],
            "domain_base": ["home.example.net"],
        }
    )
    assert draft.domain == "sip.delta.home.example.net"
    assert draft.domain_mode == "custom"


def test_draft_from_form_legacy_full_domain_is_custom():
    draft = draft_from_form({"domain": ["sip.delta.home.example.net"]})
    assert draft.domain == "sip.delta.home.example.net"
    assert draft.domain_mode == "custom"


def test_draft_as_form_carries_domain_mode_state():
    draft = WizardDraft(
        domain="sip.echo.home.example.net",
        domain_mode="suffix",
        domain_subdomain="sip.echo",
        domain_base="home.example.net",
    )
    form = draft.as_form()
    assert form["domain_mode"] == "suffix"
    assert form["domain_subdomain"] == "sip.echo"
    assert form["domain_base"] == "home.example.net"


def test_trusted_entries_reads_text_and_structured_dumps():
    text = "table: 33\nitem: {\n ip: 100.64.10.20\n tag: sip.delta.home.example.net\n}"
    assert wizard_ui.trusted_entries(text) == [
        {"ip": "100.64.10.20", "tag": "sip.delta.home.example.net"}
    ]
    structured = [{"ip": "100.64.10.20", "tag": "sip.delta.home.example.net"}]
    assert wizard_ui.trusted_entries(structured) == [
        {"ip": "100.64.10.20", "tag": "sip.delta.home.example.net"}
    ]


def test_dispatcher_target_ok_rejects_fqdn_and_accepts_ipv4():
    assert wizard_ui.dispatcher_target_ok("sip:10.0.6.20:5060;transport=tcp")
    assert not wizard_ui.dispatcher_target_ok("sip:pbx.lan:5060;transport=tcp")


def test_trusted_entries_reads_duplicate_key_pairs():
    payload = [
        ("table", 0),
        ("item", [("ip", "100.64.10.10"), ("tag", "sip.alpha.home.example.net")]),
        ("table", 1),
        ("item", [("ip", "100.64.10.12"), ("tag", "sip.beta.home.example.net")]),
    ]
    assert wizard_ui.trusted_entries(payload) == [
        {"ip": "100.64.10.10", "tag": "sip.alpha.home.example.net"},
        {"ip": "100.64.10.12", "tag": "sip.beta.home.example.net"},
    ]


def test_verify_checklist_rejects_fqdn_and_missing_set(tmp_path):
    from tests.fixtures import seeddb

    path = seeddb.seed(tmp_path / "edge.sqlite")
    conn = sqlite3.connect(path)
    conn.execute(
        "UPDATE dispatcher SET destination = ? WHERE setid = 11",
        ("sip:pbx.lan:5060;transport=tcp",),
    )
    conn.commit()
    checklist = wizard_ui.verify_checklist(conn, "sip.alpha.home.example.net")
    items = {item["key"]: item for item in checklist["items"]}
    assert not items["edge_trunk_out"]["ok"]
    assert "non-literal-IPv4" in items["edge_trunk_out"]["detail"]

    conn.execute("DELETE FROM dispatcher WHERE setid = 11")
    conn.commit()
    checklist = wizard_ui.verify_checklist(conn, "sip.alpha.home.example.net")
    items = {item["key"]: item for item in checklist["items"]}
    assert not items["edge_trunk_out"]["ok"]
    assert "no dispatcher row" in items["edge_trunk_out"]["detail"]


def test_build_live_checks_handles_rpc_client_failure():
    def broken():
        raise RuntimeError("rpc factory failed")

    live = wizard_ui.build_live_checks(
        broken, "sip.alpha.home.example.net", "100.64.10.10", 11, 31
    )
    assert live["reachable"] is False
    assert live["items"] == []
    assert "rpc factory failed" in live["errors"][0]


def test_dispatcher_target_ok_matches_dispatcher_rule():
    assert wizard_ui.dispatcher_target_ok("sip:10.0.6.20:5060;transport=tcp")
    assert wizard_ui.dispatcher_target_ok(
        "sips:trunk@10.0.6.20:5060;transport=tcp"
    )
    assert not wizard_ui.dispatcher_target_ok("sip:pbx.lan:5060;transport=tcp")


def test_trusted_entries_does_not_pair_an_untagged_row():
    text = (
        "table: 0\nitem: {\n ip: 100.64.10.10\n}\n"
        "table: 1\nitem: {\n ip: 100.64.10.12\n"
        " tag: sip.beta.home.example.net\n}"
    )
    assert wizard_ui.trusted_entries(text) == [
        {"ip": "100.64.10.12", "tag": "sip.beta.home.example.net"}
    ]
