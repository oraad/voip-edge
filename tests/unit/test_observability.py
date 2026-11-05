"""T9 unit tests — JSON log shape and secret redaction."""
from __future__ import annotations

import json

from console import observability, settings as settings_module
from console.observability import (
    REDACTED,
    format_event,
    log_auth_failure,
    log_event,
    log_lifecycle_transition,
    log_readonly_refusal,
    log_reload_outcome,
    redact_fields,
)


def _record(line: str) -> dict:
    return json.loads(line)


def test_format_event_has_ts_event_fields_and_never_a_trailing_newline():
    line = format_event("reload.outcome", {"table": "tenantdom", "status": "applied"})
    assert "\n" not in line
    record = _record(line)
    assert set(record) == {"ts", "event", "fields"}
    assert record["event"] == "reload.outcome"
    assert record["ts"].endswith("Z")
    assert record["fields"] == {"table": "tenantdom", "status": "applied"}


def test_sensitive_keys_are_redacted_at_any_depth():
    payload = {
        "token": "super-secret-token",
        "password": "hunter2",
        "Cookie": "session=abc",
        "X-CSRF-Token": "csrf-value",
        "Authorization": "Bearer abc.def",
        "nested": {
            "api_key": "key-123",
            "session_cookie": "sess",
            "safe": "visible",
        },
        "items": [{"secret": "nope"}, {"safe": "yes"}],
    }

    line = format_event("auth.failure", payload)
    record = _record(line)
    fields = record["fields"]

    assert fields["token"] == REDACTED
    assert fields["password"] == REDACTED
    assert fields["Cookie"] == REDACTED
    assert fields["X-CSRF-Token"] == REDACTED
    assert fields["Authorization"] == REDACTED
    assert fields["nested"]["api_key"] == REDACTED
    assert fields["nested"]["session_cookie"] == REDACTED
    assert fields["nested"]["safe"] == "visible"
    assert fields["items"][0]["secret"] == REDACTED
    assert fields["items"][1]["safe"] == "yes"
    for literal in ("super-secret-token", "hunter2", "csrf-value", "key-123", "abc.def"):
        assert literal not in line


def test_configured_token_and_secret_are_scrubbed_even_under_safe_keys(monkeypatch):
    from dataclasses import replace

    monkeypatch.setattr(
        settings_module,
        "settings",
        replace(
            settings_module.settings,
            token="tok-value-123",
            secret="secret-value-456",
        ),
    )
    line = format_event(
        "login.debug",
        {"message": "used tok-value-123 with secret-value-456 and Bearer abc.def"},
    )
    assert "tok-value-123" not in line
    assert "secret-value-456" not in line
    assert "abc.def" not in line
    assert REDACTED in line


def test_log_event_writes_one_json_line_to_stdout(capsys):
    log_event("reload.outcome", table="dispatcher", status="failed", password="nope")

    captured = capsys.readouterr()
    assert captured.err == ""
    lines = [line for line in captured.out.splitlines() if line.strip()]
    assert len(lines) == 1
    record = _record(lines[0])
    assert record["event"] == "reload.outcome"
    assert record["fields"]["table"] == "dispatcher"
    assert record["fields"]["status"] == "failed"
    assert record["fields"]["password"] == REDACTED


def test_domain_helpers_emit_stable_event_names_and_no_secret(capsys):
    log_reload_outcome(table="tenantdom", status="applied", rpc="mtree.reload")
    log_lifecycle_transition(
        op="park", domain="beta.home.example.net", state="PURGED", intent_id="i-1"
    )
    log_auth_failure(reason="invalid_token", client="127.0.0.1", path="/login")
    log_readonly_refusal(method="POST", path="/tables/tenant_registry/new")

    records = [_record(line) for line in capsys.readouterr().out.splitlines() if line]
    assert [record["event"] for record in records] == [
        "reload.outcome",
        "lifecycle.transition",
        "auth.failure",
        "readonly.refused",
    ]
    assert records[1]["fields"]["domain"] == "beta.home.example.net"
    assert settings_module.settings.token not in json.dumps(records[2])


def test_key_value_style_secrets_in_strings_are_scrubbed():
    line = format_event(
        "debug",
        {"message": "login token=abc123 password=hunter2 cookie=session-x"},
    )
    assert "abc123" not in line
    assert "hunter2" not in line
    assert "session-x" not in line
    assert line.count(REDACTED) >= 3


def test_unserialisable_values_fall_back_to_str():
    class Opaque:
        def __str__(self) -> str:
            return "opaque-value"

    line = format_event("debug", {"value": Opaque()})
    assert _record(line)["fields"]["value"] == "opaque-value"


def test_redact_fields_is_pure_and_recursive():
    original = {"token": "x", "nested": [{"password": "y", "ok": 1}]}
    redacted = redact_fields(original)
    assert redacted == {
        "token": REDACTED,
        "nested": [{"password": REDACTED, "ok": 1}],
    }
    assert original["token"] == "x", "redaction must not mutate the caller's data"
