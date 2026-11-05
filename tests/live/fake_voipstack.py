#!/usr/bin/env python3
"""fake_voipstack.py — minimal asyncio stand-in for a home voip-stack / PSTN GW.

Simulates the server side the edge lab tests against (see TESTING.md):
  * REGISTER digest challenge with the fixed voip_stack realm, MD5 verify,
    200 OK echoing Contact (like sip_registrar.py);
  * OPTIONS -> 200 (dispatcher keepalive);
  * INVITE  -> 180 + 200 (--fail CODE returns CODE instead, for failover tests)
  * BYE     -> 200
Everything noteworthy is appended to a log file as key=value lines that
run-lab.sh asserts on.

Usage:
  fake_voipstack.py --bind 127.0.0.1 --port 5190 --log /tmp/fake1.log
                    [--user alice --password secret1] [--fail 480]
"""
import argparse
import asyncio
import hashlib
import os
import re
import time

REALM = "voip_stack"     # fixed realm, like custom_components/voip_stack


def md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def hdrs(msg: str) -> dict:
    out = {}
    for line in msg.split("\r\n")[1:]:
        if ": " in line:
            k, v = line.split(": ", 1)
            out.setdefault(k.lower(), v)
    for line in msg.split("\r\n")[1:]:  # compact forms
        if line.startswith("f:"):
            out["from"] = line[2:].strip()
        if line.startswith("t:"):
            out["to"] = line[2:].strip()
    return out


def via_branch(msg):
    m = re.search(r"Via:.*branch=([^;\r\n]+)", msg, re.I)
    return m.group(1) if m else ""


def tag(msg, name):
    v = hdrs(msg).get(name, "")
    m = re.search(r"tag=([^;>\r\n]+)", v)
    return m.group(1) if m else ""


def user_of(uri_hdr: str) -> str:
    m = re.search(r"sip:([^@;>]+)", uri_hdr)
    return m.group(1) if m else ""


class Fake(asyncio.DatagramProtocol):
    def __init__(self, a):
        self.a = a
        self.log = open(a.log, "a", buffering=1)
        self.nonce = "labnonce1234"

    def connection_made(self, tr):
        self.tr = tr

    def rec(self, **kv):
        self.log.write(" ".join(f"{k}={v}" for k, v in kv.items()) + "\n")

    def reply(self, addr, msg, code, reason, extra=""):
        h = hdrs(msg)
        top = msg.split("\r\n", 1)[0]
        cseq, method = h.get("cseq", "0 X").split(" ", 1)
        to = h.get("to", "")
        if code >= 200 and "tag=" not in to:
            to += f";tag={os.getpid()}fake"
        resp = (
            f"SIP/2.0 {code} {reason}\r\n"
            f"Via: {self.viasep(msg)}\r\n"
            f"From: {h.get('from','')}\r\n"
            f"To: {to}\r\n"
            f"Call-ID: {h.get('call-id','')}\r\n"
            f"CSeq: {cseq} {method.upper()}\r\n"
            + extra +
            "Content-Length: 0\r\n\r\n"
        )
        self.tr.sendto(resp.encode(), addr)
        self.rec(event="reply", code=code, ruri=top.split(" ")[1] if top.startswith(tuple("INVR")) else "-")

    def viasep(self, msg):
        # echo ALL via headers verbatim (proxy chains)
        vias = [l[5:].strip() for l in msg.split("\r\n") if l.lower().startswith("via:")]
        return "\r\nVia: ".join(vias)

    def datagram_received(self, data, addr):
        msg = data.decode(errors="replace")
        first = msg.split("\r\n", 1)[0]
        a = self.a
        if first.startswith("REGISTER"):
            h = hdrs(msg)
            auth = h.get("authorization", "")
            if not auth:
                self.reply(addr, msg, 401, "Unauthorized",
                           f'WWW-Authenticate: Digest realm="{REALM}", nonce="{self.nonce}", algorithm=MD5, qop="auth"\r\n')
                self.rec(event="register", result="challenge")
                return
            if self.check_digest(h, first):
                contact = h.get("contact", "")
                extra = f"Contact: {contact}\r\nExpires: 600\r\n" if contact else "Expires: 600\r\n"
                self.reply(addr, msg, 200, "OK", extra)
                self.rec(event="register", result="ok", user=user_of(h.get("to", "")))
            else:
                self.reply(addr, msg, 403, "Forbidden")
                self.rec(event="register", result="baddigest")
        elif first.startswith("OPTIONS"):
            self.reply(addr, msg, 200, "Keepalive OK")
        elif first.startswith("INVITE"):
            ruri = first.split(" ")[1]
            h = hdrs(msg)
            self.rec(event="invite",
                     ruri=user_of(ruri),
                     fromuser=user_of(h.get("from", "")),
                     pai=h.get("p-asserted-identity", "-").strip(),
                     rpid="yes" if "remote-party-id" in h else "-",
                     src=f"{addr[0]}:{addr[1]}")
            if a.fail:
                self.reply(addr, msg, a.fail, "Forced Failure")
                return
            self.reply(addr, msg, 180, "Ringing")
            self.reply(addr, msg, 200, "OK")
        elif first.startswith("ACK"):
            pass
        elif first.startswith("BYE"):
            self.reply(addr, msg, 200, "OK")
        elif first.startswith("CANCEL"):
            self.reply(addr, msg, 200, "OK")
        elif first.startswith("SIP/2.0"):
            pass

    def check_digest(self, h, firstline):
        a = self.a
        p = dict(re.findall(r'(\w+)="?([^",\s]+)"?', h.get("authorization", "")))
        if p.get("username") != a.user:
            return False
        ha1 = md5(f"{p['username']}:{REALM}:{a.password}")
        ha2 = md5(f"REGISTER:{p.get('uri','')}")
        nc = p.get("nc", "")
        cnonce = p.get("cnonce", "")
        if nc and cnonce:
            expect = md5(f"{ha1}:{p.get('nonce')}:{nc}:{cnonce}:auth:{ha2}")
        else:
            expect = md5(f"{ha1}:{p.get('nonce')}:{ha2}")
        return expect == p.get("response")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--user", default="alice")
    ap.add_argument("--password", default="secret1")
    ap.add_argument("--fail", type=int, default=0)
    a = ap.parse_args()
    loop = asyncio.get_running_loop()
    await loop.create_datagram_endpoint(
        lambda: Fake(a), local_addr=(a.bind, a.port))
    print(f"fake_voipstack listening on {a.bind}:{a.port} log={a.log}")
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
