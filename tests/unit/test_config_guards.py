"""Static regression guards for config strings that have live failure modes."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _substdef_regex(text: str, name: str) -> str:
    match = re.search(rf'#!substdef "!{name}!(.*?)!"', text)
    assert match, f"{name} substdef not found"
    # The replacement parser needs the trailing anchor escaped; Kamailio sees
    # the single backslash-escaped dollar as a literal '$'.
    return match.group(1).replace(r"\$", "$")


def test_tenant_suffix_gate_uses_a_generated_substdef():
    cfg = (ROOT / "kamailio" / "kamailio.cfg").read_text()
    assert '#!include_file "tenant_suffix.cfg"' in cfg
    assert '#!define TENANTSUFFIXRE' not in cfg

    generated = (ROOT / "kamailio" / "tenant_suffix.cfg").read_text()
    assert "#!substdef" in generated
    regex = _substdef_regex(generated, "TENANTSUFFIXRE")
    assert re.fullmatch(regex, "sip.alpha.home.example.net")
    assert not re.fullmatch(regex, "sip.alpha.home.example.net.evil")
    assert not re.fullmatch(regex, "evil.example.net")


def test_both_call_paths_reference_the_suffix_gate():
    invite = (ROOT / "kamailio" / "invite.cfg").read_text()
    register = (ROOT / "kamailio" / "register.cfg").read_text()
    assert '=~ "TENANTSUFFIXRE$"' in invite
    assert '=~ "TENANTSUFFIXRE$"' in register
    # REGISTER must check the To domain before the mtree prefix match.
    assert register.index("TENANTSUFFIXRE") < register.index('mt_match("tenantdom"')


def test_cert_promotion_avoids_gnu_only_mv_T():
    script = (ROOT / "acme" / "deploy-certs.sh").read_text()
    assert "mv -Tf" not in script
    assert 'ln -sfn "releases/$TAG" "$LIVE/current"' in script


def test_indialog_and_phone_media_guards_are_present():
    misc = (ROOT / "kamailio" / "misc.cfg").read_text()
    invite = (ROOT / "kamailio" / "invite.cfg").read_text()
    media = (ROOT / "kamailio" / "media.cfg").read_text()
    assert "Unknown Dialog Source" in misc
    assert "s.substr,4,0" in misc
    assert "does not match regcache flow" in invite
    assert "direction=public direction=public" in media
