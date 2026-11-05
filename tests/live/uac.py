#!/usr/bin/env python3
"""uac.py — scripted UDP UAC for the edge lab (see TESTING.md).

Covers the client roles the edge talks to:
  register        REGISTER with digest resubmission (like a softphone)
  invite          INVITE through the edge (phone-originated)
  home-invite     INVITE as a home voip-stack trunk call (bound source IP)
  options-answer  (implicit) every client answers OPTIONS keepalives

Usage examples are in run-lab.sh. Prints RESULT=<code> on stdout; assertions
on what the fakes received happen via their log files.
"""
import argparse
import asyncio
import hashlib
import re
import uuid


def md5(s):
    return hashlib.md5(s.encode()).hexdigest()


class UAC(asyncio.DatagramProtocol):
    def __init__(self, a):
        self.a = a
        self.queue = asyncio.Queue()
        self.done = None

    def connection_made(self, tr):
        self.tr = tr

    def datagram_received(self, data, addr):
        msg = data.decode(errors="replace")
        first = msg.split("\r\n", 1)[0]
        if first.startswith("SIP/2.0"):
            self.queue.put_nowait(msg)
        elif first.startswith("INVITE") and self.a.cmd == "listen":
            # auto-answer: 180 + 200 so the relaying edge can complete the call
            h = dict(l.split(": ", 1) for l in msg.split("\r\n")[1:] if ": " in l)
            cs, m = h.get("CSeq", "0 INVITE").split(" ", 1)
            to = h.get("To", "")
            if "tag=" not in to:
                to += ";tag=lab"
            vias = "\r\n".join(l for l in msg.split("\r\n") if l.lower().startswith("via:"))
            for code in ("180 Ringing", "200 OK"):
                self.tr.sendto((
                    f"SIP/2.0 {code}\r\n" + vias + f"\r\nFrom: {h.get('From','')}\r\n"
                    f"To: {to}\r\nCall-ID: {h.get('Call-ID','')}\r\nCSeq: {cs} {m}\r\n"
                    "Content-Length: 0\r\n\r\n").encode(), addr)
            # queue carries the real From so tests can assert rewrites
            self.queue.put_nowait("SIP/2.0 200 OK\r\nFrom: " + h.get("From", "") + "\r\n")
        elif first.startswith("OPTIONS"):
            h = dict(l.split(": ", 1) for l in msg.split("\r\n")[1:] if ": " in l)
            m, cs = h.get("CSeq", "0 OPTIONS").split(" ", 1)
            to = h.get("To", "")
            if "tag=" not in to:
                to += ";tag=lab"
            vias = "\r\n".join(l for l in msg.split("\r\n") if l.lower().startswith("via:"))
            self.tr.sendto((
                "SIP/2.0 200 OK\r\n" + vias + f"\r\nFrom: {h.get('From','')}\r\n"
                f"To: {to}\r\nCall-ID: {h.get('Call-ID','')}\r\nCSeq: {cs} {m}\r\n"
                "Content-Length: 0\r\n\r\n").encode(), addr)

    async def next_reply(self, timeout=10):
        msg = await asyncio.wait_for(self.queue.get(), timeout)
        code = int(msg.split(" ")[1])
        return code, msg

    def base_headers(self, method, ruri, user, domain):
        a = self.a
        cid = f"{uuid.uuid4()}@lab"
        return (
            f"Via: SIP/2.0/UDP {a.bind}:{a.sport};branch=z9hG4bK{uuid.uuid4().hex[:8]};rport\r\n"
            f"From: <sip:{a.fromuser or a.user}@{a.fromhost or domain}>;tag={uuid.uuid4().hex[:8]}\r\n"
            f"To: <sip:{user}@{domain}>\r\n"
            f"Call-ID: {cid}\r\n"
            f"CSeq: {a.cseq} {method}\r\n"
            f"Max-Forwards: 10\r\n"
        )

    def build_register(self, auth=""):
        a = self.a
        contact = f"Contact: <sip:{a.user}@{a.bind}:{a.sport};transport=udp>;expires={a.expires}\r\n"
        req = (f"REGISTER sip:{a.domain} SIP/2.0\r\n"
               + self.base_headers("REGISTER", f"sip:{a.domain}", a.user, a.domain)
               + contact + auth
               + f"Expires: {a.expires}\r\nContent-Length: 0\r\n\r\n")
        return req

    def build_invite(self, auth=""):
        a = self.a
        ruri = f"sip:{a.ruser}@{a.domain}"
        body = ""
        req = (f"INVITE {ruri} SIP/2.0\r\n"
               + self.base_headers("INVITE", ruri, a.touser or a.ruser, a.domain)
               + f"Contact: <sip:{a.user}@{a.bind}:{a.sport};transport=udp>\r\n"
               + auth
               + "Content-Length: 0\r\n\r\n")
        return req

    def digest_auth(self, challenge, method, uri):
        p = dict(re.findall(r'(\w+)="?([^",\s]+)"?',
                            re.search(r"WWW-Authenticate: (.*)\r\n", challenge, re.I).group(1)))
        a = self.a
        nonce = p["nonce"]
        cnonce = uuid.uuid4().hex[:16]
        nc = "00000001"
        ha1 = md5(f"{a.user}:{p['realm']}:{a.password}")
        ha2 = md5(f"{method}:{uri}")
        resp = md5(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}")
        return (f'Authorization: Digest username="{a.user}", realm="{p["realm"]}",'
                f' nonce="{nonce}", uri="{uri}", response="{resp}", algorithm=MD5,'
                f' qop=auth, nc={nc}, cnonce="{cnonce}"\r\n')

    async def run(self):
        a = self.a
        dest = (a.edge.split(":")[0], int(a.edge.split(":")[1]))
        if a.cmd == "register":
            self.tr.sendto(self.build_register().encode(), dest)
            code, msg = await self.next_reply()
            if code == 401:
                a.cseq += 1
                auth = self.digest_auth(msg, "REGISTER", f"sip:{a.domain}")
                self.tr.sendto(self.build_register(auth).encode(), dest)
                code, msg = await self.next_reply()
            print(f"RESULT={code}")
        elif a.cmd == "listen":
            # phone side: registered already; wait for an INVITE down the flow
            code, msg = await self.next_reply(8)
            m = re.search(r"^From:\s*(.+?)\s*$", msg, re.I | re.M)
            print(f"RECEIVED=INVITE FROM={m.group(1) if m else '-'} RESULT={code}")
        else:
            build = self.build_invite
            self.tr.sendto(build().encode(), dest)
            final = None
            while final is None or final < 200:
                final, msg = await self.next_reply()
            print(f"RESULT={final}")
        self.tr.close()


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cmd", choices=["register", "invite", "listen"], required=True)
    ap.add_argument("--edge", default="127.0.0.1:5060")
    ap.add_argument("--bind", default="127.0.0.1",
                    help="source address (use a 127.0.0.x alias to impersonate a home)")
    ap.add_argument("--sport", type=int, default=0)
    ap.add_argument("--user", default="alice")       # account user (auth user)
    ap.add_argument("--password", default="secret1")
    ap.add_argument("--domain", default="home1.test")
    ap.add_argument("--ruser", default="bob")        # R-URI user for invites
    ap.add_argument("--touser", default="")
    ap.add_argument("--fromuser", default="")
    ap.add_argument("--fromhost", default="")      # override From URI host (default: domain)
    ap.add_argument("--expires", type=int, default=600)
    ap.add_argument("--cseq", type=int, default=1)
    a = ap.parse_args()
    loop = asyncio.get_running_loop()
    tr, proto = await loop.create_datagram_endpoint(lambda: UAC(a), local_addr=(a.bind, a.sport))
    proto.tr = tr
    try:
        await proto.run()
    except asyncio.TimeoutError:
        print("RESULT=TIMEOUT")


if __name__ == "__main__":
    asyncio.run(main())
