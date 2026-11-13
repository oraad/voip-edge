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


def test_trusted_sources_record_pbx_heartbeat():
    cfg = (ROOT / "kamailio" / "kamailio.cfg").read_text()
    assert '"trunkseen=>size=8;autoexpire=0;"' in cfg
    assert "$sht(trunkseen=>$si) = $Ts;" in cfg


def test_indialog_relays_guard_loops_and_pin_the_overlay():
    misc = (ROOT / "kamailio" / "misc.cfg").read_text()
    assert '"482", "Loop Detected"' in misc
    assert "Loop Detected" in misc.split("t_relay")[0]  # guard precedes relay
    assert 'xlog("L_WARN"' in misc
    assert "route(TUNNEL_SOCKET)" in misc  # conditional pin inside WITHINDLG
    assert '!is_method("ACK") || !t_check_trans()' in misc  # ACK exemption
    assert "Unknown Dialog Source" in misc  # flow check stays intact


def test_indialog_guard_compares_exact_hosts_and_pins_the_full_overlay():
    misc = (ROOT / "kamailio" / "misc.cfg").read_text()
    # Own-address detection compares the resolved destination host for exact
    # equality: a substring match would 482 prefix-colliding flows, e.g. peer
    # 100.64.0.90 against edge .0.9 or public 203.0.113.100 against a .10 edge.
    assert "$(du{uri.host})" in misc
    # with the last local Route stripped, $du is null and the R-URI is the
    # destination: the pin must fall back to $rd or relays leave on the public
    # socket and die on strict NATs (UPDATE -> 408 observed live).
    assert "$du == $null" in misc and "$rd" in misc
    assert "$var(dhost) == PUBIP || $var(dhost) == WGIP" in misc
    assert '[@:]" + PUBIP' not in misc
    match = re.search(r'\$var\(dhost\) =~ "\^(\(\?[^"]+\)|[^"]+)"', misc)
    assert match, "overlay pin must anchor the destination host from its start"
    pinned = re.compile("^(?:" + match.group(1) + ")")
    assert pinned.match("100.64.10.5")     # WireGuard peer
    assert pinned.match("100.89.0.42")     # tunnel pools inside 100.64.0.0/10
    assert pinned.match("10.87.4.10")      # home LANs, routed only via the hub
    assert pinned.match("192.168.86.5")
    assert pinned.match("172.20.1.1")
    assert not pinned.match("100.128.1.1")  # beyond the /10 boundary
    assert not pinned.match("8.8.8.8")      # public flows keep the default socket
    assert not pinned.match("192.0.2.10")


def test_interhome_legs_anchored_in_rtpengine():
    invite = (ROOT / "kamailio" / "invite.cfg").read_text()
    media = (ROOT / "kamailio" / "media.cfg").read_text()
    assert "$avp(interhome) = 1" in invite
    assert "direction=internal direction=internal" in media
    branch = invite.split('# 2: landline -> on-net')[1].split("# 3:")[0]
    assert "$avp(interhome) = 1" in branch
    assert "route(MEDIA_DECIDE)" in branch and "route(MEDIA_OFFER)" in branch


def test_home_to_phone_from_rewrite_spares_tenant_domains():
    invite = (ROOT / "kamailio" / "invite.cfg").read_text()
    # An inter-home leg arrives with From already at the calling home's
    # tenant domain; rewriting to the callee tenant would misroute redial.
    assert 'if ($var(o_trusted) == 1 && !($fd =~ "TENANTSUFFIXRE$"))' in invite


def test_rtpe_flags_generate_rtcp_for_homer_stats():
    media = (ROOT / "kamailio" / "media.cfg").read_text()
    # rtpengine only forwards RTCP reports to Homer when it generates RTCP
    # itself; the base flags carry the feature so every anchored branch and
    # the stored answer/delete flags inherit it.
    assert '$var(rtpe_flags) = "replace-origin replace-session-connection ICE=remove generate-RTCP";' in media
    assert '$sht(rtpcache=>$ci) = "replace-origin replace-session-connection ICE=remove generate-RTCP"' in media
