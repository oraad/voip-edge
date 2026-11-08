# Kamailio as Edge Proxy in Front of Downstream Registrar/PBX Servers

> Historical reference. Current deployment and operations are in
> `docs/ops/deploy.md` and `docs/operations.md`.

Research notes: how to run Kamailio as a pure edge/proxy tier in front of one or more
tenant registrar/PBX nodes (Asterisk, FreeSWITCH, another Kamailio, …) where the
**downstream Registrar remains the single source of truth** for accounts and contacts.
Kamailio must NOT maintain a duplicate registration/location database.

Sources: kamailio.org module docs (`path`, `outbound`, `registrar`, `usrloc`, `mtree`,
`dispatcher`, tm), Kamailio core cookbook, RFC 3327 / 5626 / 5627, Asterisk PJSIP docs,
FreeSWITCH mailing lists. Verified against the current stable module docs
(kamailio.org/docs/modules/stable/...) on 2026-11.

---

## 1. RFC 3327 (Path) — the `path` module

Docs: https://kamailio.org/docs/modules/stable/modules/path.html
(module originates from SER, © 2006 Inode GmbH; long-stable, works in all current 5.x/6.x).

### Edge side (Kamailio)

For a topology `[UAC] -> [edge/P1] -> [REGISTRAR/PBX]`:

- `add_path()` prepends `Path: <sip:edge-ip;lr>` (own outgoing interface) to the REGISTER
  before relaying downstream.
- `add_path(user)` / `add_path(user, params)` adds a user part and/or extra URI params,
  e.g. `add_path("edge", "ob")`.
- `add_path_received(...)` additionally encodes the **source address of the REGISTER**
  into a `received=` URI parameter:
  `Path: <sip:edge;received=sip:clientNATip:port;lr>`. This is how the NAT-side address
  of the client survives all the way into the Path vector and comes back later as a Route
  URI parameter.

Important details:

- Some registrars (e.g. older Asterisk) only store/honour Path if the REGISTER advertises
  support. Force-advertise at the edge: `append_hf("Supported: path\r\n");`
  (textops) before `add_path()`.
- `modparam("path", "use_received", 1)` — when a request comes *back* from the downstream
  registrar along the stored Path (the registrar preloads `Route: <sip:edge;lr;received=...>`),
  the edge extracts the `received` parameter of the first Route URI and uses it as the
  **destination URI**, i.e. it sends to the NAT binding of the client instead of the
  (unreachable/private) contact address. Requires `rr` loaded; evaluation happens during
  `loose_route()` processing.
- `modparam("path", "received_format", 1)` encodes the received address in the
  `ip~port~proto` alias format instead of an escaped SIP URI — a workaround "for SIP
  applications that incorrectly match the transport from the received parameter instead
  of the one from the SIP URI" (notably observed with FreeSWITCH+WS deployments).
- `enable_r2=1` adds two Path headers (double Path, like double Record-Route) with
  `r2=on` when in/out sockets differ (e.g. WS in, UDP out).
- `sockname_mode=1` adds a `sn=` param with the socket name (useful on multi-homed edges).
- If `outbound.so` is loaded *before* `path.so`, and the request qualifies as an
  RFC 5626 registration (or `force_outbound_flag` is set), the Path URI becomes
  `Path: <sip:FLOWTOKEN@edge;lr;ob>` — see §2.

### Registrar side (`lookup()` + Path)

If the downstream registrar is itself Kamailio, the `registrar` module
(https://kamailio.org/docs/modules/stable/modules/registrar.html) has full RFC 3327
server support:

- `modparam("registrar", "use_path", 1)` — save() stores Path header(s) with the contact
  in usrloc (`usrloc` has a dedicated `path` column; matching mode 2 = CONTACT+PATH).
- `modparam("registrar", "path_mode", X)` controls the REGISTER reply:
  - **0 (off):** store Path, don't echo in 200 OK.
  - **1 (lazy):** store; echo in reply only if UA sent `Supported: path`.
  - **2 (strict, *default*, RFC-recommended):** store + echo only if `Supported: path`;
    otherwise reject with **420 Bad Extension** + `Unsupported: path`.
- `modparam("registrar", "path_use_received", 1)` — take the `received=` param of the
  first Path URI as the contact's "received" address and set the NAT branch flag
  (registrar behind a load balancer).
- `modparam("registrar", "path_check_local", 1)` — during lookup skip a first Path hop
  that resolves to "myself" (shared location DB across servers).

From the registrar docs (verbatim semantics of `lookup()` with Path):

> "A call to lookup(...) always uses the path header if found, and inserts it as Route
> header field either in front of the first Route header field, or after the last Via
> header field if no Route is present. It also sets the destination uri to the first
> Path uri, thus overwriting the received-uri, because NAT has to be handled at the
> outbound-proxy of the UAC."
>
> "The whole process is transparent to the user, so no config changes are required
> beside setting the registrar-parameters `use_path` and `path_mode`."

So an inbound INVITE to a registered AoR leaves the registrar with
`Route: <sip:edge;lr;received=...>` preloaded and R-URI = stored contact; the edge does
`loose_route()` → per `use_received` the destination becomes the client's NAT binding,
and the request tunnels through the edge back to the UA.

### What the downstream registrar MUST support (if it is Asterisk/FreeSWITCH/…)

Minimum to make RFC 3327 work end-to-end:

1. Parse and **store the Path vector per registered contact**.
2. Echo Path in the REGISTER 200 OK (at least when UA signalled `Supported: path`).
3. When routing an inbound request to the contact, **prepend the stored Path vector as a
   Route set** (loose-routable, in order) and still use the contact as the target.
4. Preserve `received=` and `;ob`-style URI parameters.

Known status for common PBXs:

- **Asterisk chan_pjsip**: server-side Path is implemented by `res_pjsip_path`
  (added in the Asterisk 12 / chan_pjsip era, review ASTERISK-21084, Dec 2013). Enable
  per AOR: `support_path=yes`. The Path vector is stored on the contact
  (`path` attribute, "Stored Path vector for use in Route headers on outgoing requests").
  **Long-standing bug ASTERISK-30100 "Path is ignored on INVITE to endpoint"** was fixed
  only around Asterisk **18.17 / 20.2 (2023)** — use at least those versions, and note
  that `Dial(PJSIP/exten)` honours Path while `Dial(${PJSIP_DIAL_CONTACTS(...)})` can
  bypass it (reported on Asterisk 20.2, sr-users Mar 2023).
- **FreeSWITCH mod_sofia**: stores Path on registration, echoes it in the 200 OK
  (`Supported: timer, path, replaces`), and encodes it into the stored contact as
  `fs_path=` so originated calls traverse the proxy. Known quirk: FS may derive the wrong
  transport/destination from an escaped `received=` SIP URI inside Path (WS contacts) —
  this is exactly what Kamailio's `path` `received_format=1` is for
  (freeswitch-users, Feb 2018, "received= in path header").

---

## 2. RFC 5626 (SIP Outbound) — the `outbound` module

Docs: https://kamailio.org/docs/modules/stable/modules/outbound.html (© 2012-13 Peter
Dunkley, Crocodile RCS; introduced with **Kamailio 4.0 (2013)** together with registrar
5626/5627 support).

> "This module provides C-API functions to enable Kamailio to be used as an outbound
> Edge Proxy (see RFC5626 section 5). The *path* and *rr* modules will bind to this
> module if it is loaded before they are."

The module itself implements **edge-proxy-side** mechanics only; the registrar-side
procedures live in the `registrar` module.

### What counts as an "outbound" request (module Conditions)

- REGISTER: single Via (first hop from UA) and Contact has a `;reg-id` parameter.
- Non-REGISTER: top Route URI is myself and has `;ob`; **or** `Supported: outbound`,
  single Via and Contact has `;ob`.

`force_outbound_flag` / `force_no_outbound_flag` module params let the script force or
suppress outbound processing per request (both default -1 = unset).

### Flow tokens

When conditions match, `add_path()` / `record_route()` automatically embed an opaque
**flow-token** in the Path/Record-Route URI user part and append `;ob`:

```
Path: <sip:JFq8...tok@edge-ip;lr;ob>
Route: <sip:JFq8...tok@edge-ip;lr;ob>
```

The token encodes local/remote address+port and transport of the client-side connection.
On the way back (request from registrar), `check_flow_token()` validates and binds the
request to the existing TCP/TLS/WS connection. Return codes: 1 ok; -1 no Route; -2 top
Route not myself; -3 no token (fall back to plain loose_route); -4 decode failed;
-5 connection gone → **430 Flow Failed**; -6 token expired → 430; -7/-8 parse errors.

`flow_token_secret` sets the key used to compute tokens; if unset, a random key is
generated at start. **On a farm of edges, set the same secret on all** of them (tokens
survive restarts / can be decoded by siblings). Note Kamailio's choice of an encrypted-token
design is deliberate: an edge cannot open a new connection to the UA, so the token must
map to a live connection on that specific node.

### Keepalive (double-CRLF and STUN)

RFC 5626 §3.5: the UA and the edge keep the flow alive; for stream transports the UA sends
periodic **CRLFCRLF** pings and the edge answers with a single CRLF; for UDP, STUN
binding requests are used.

- Edge MUST support **STUN NAT keep-alives on its SIP UDP ports** → load Kamailio's
  `stun` module alongside outbound.
- **Double-CRLF** ping/pong for TCP/TLS/WSS is implemented in **core**:
  `tcp_crlf_ping = yes` (default) (core cookbook: "Enable SIP outbound TCP keep-alive
  using PING-PONG (CRLFCRLF - CRLF)").
- **Flow-Timer**: the registrar/edge may announce the max keepalive interval via a
  `Flow-Timer:` header in the REGISTER 200 OK. With a separate edge, the recommended
  pattern (from the outbound module docs' own example) is to let the **edge** insert it,
  keeping all flow-related timers in edge config:

```
onreply_route {
    if ($rm == "REGISTER" && $rs >= 200 && $rs <= 299) {
        remove_hf("Flow-Timer");
        if ($(hdr(Require)[*]) =~ "outbound")
            insert_hf("Flow-Timer: 20\r\n", "Call-ID");
    }
}
```
and set edge `tcp_connection_lifetime` slightly **larger** than the flow timer
(e.g. flow timer 20s → `tcp_connection_lifetime=30`); for WebSocket, `keepalive_timeout`
a bit above flow-timer and below tcp_connection_lifetime.

### instance-id / reg-id

- UA sends `+sip.instance="<urn:uuid:...>"` and `reg-id=N` in Contact.
- Registrar (downstream) must store them (Kamailio `usrloc` has `instance`, `reg_id`,
  `ruid`, `connection_id` columns) and implement RFC 5626 registrar procedures:
  - `modparam("registrar", "outbound_mode", 1|2)` — 1 = accept both classic and outbound
    clients, following 5626 for outbound ones; 2 = require `Supported: outbound`.
    0 (default) = classic RFC 3261 only; a client with `Require: outbound` gets 420.
  - `modparam("registrar", "regid_mode", 1)` — honour `reg-id` even without the
    outbound option-tag.
  - `modparam("registrar", "flow_timer", N)` — registrar-side Flow-Timer insertion
    (mutually exclusive in spirit with edge insertion; registrar docs advise it only for
    a combined edge+registrar or a non-modifiable edge).
- **Multiple flows per AoR**: the docs' registrar example uses tm to iterate flows:
  `modparam("tm","contacts_avp","tm_contacts")`,
  `modparam("tm","contact_flows_avp","tm_contact_flows")` — after `lookup()`,
  `t_load_contacts()` / `t_next_contacts()`, and in
  `event_route[tm:branch-failure]` on 430/403/timeout
  `unregister("location","$tu","$T_reply_ruid")` then `t_next_contact_flow()` to try the
  next registered flow.

**Division of work**:
| Component | Edge (this Kamailio) | Downstream registrar |
|---|---|---|
| STUN replies on UDP | `stun` module | n/a |
| CRLF ping/pong | core `tcp_crlf_ping=yes` | n/a |
| Flow-token create/decode | `outbound` + `path`/`rr` | n/a (opaque to it) |
| `Flow-Timer` header | insert at edge (recommended) | or `registrar flow_timer` |
| `;ob`/`reg-id`/`+sip.instance` semantics | detect, tokenize | store, 5626 procedures (`outbound_mode`, `regid_mode`), per-flow serial retry |

---

## 3. RFC 5627 (GRUU)

**Kamailio supports GRUU natively in the `registrar` module** (since Kamailio 4.0 era,
same vintage as outbound). From the registrar docs:

> "GRUU (RFC5627) is supported with both public and temporary addresses. The public GRUU
> is built based on the '+sip.instance' UUID parameter as recommended by RFC. The
> temporary GRUU is built based on internal SRUID (unique id generator) and it is kept
> the same for the duration of contact validity."

`modparam("registrar", "gruu_enabled", 1)` (default 1): if the REGISTER Contact carries
`+sip.instance`, the instance is stored (usrloc `instance` column) and `pub-gruu` /
`temp-gruu` are generated into the 200 OK Contact.

So if the *downstream* registrar is Kamailio, GRUU works end-to-end without any work at
the edge beyond not mangling Contact parameters. If the downstream is Asterisk chan_pjsip
or FreeSWITCH mod_sofia, **GRUU is not implemented** on their side (they store/recognise
`+sip.instance` at best); clients that send `Supported: gruu` simply never receive
pub-gruu/temp-gruu. No edge-side workaround exists (GRUU must be minted by the registrar
that owns the AoR) — treat GRUU as unavailable with those PBXs.

---

## 4. Why the naive "REGISTER relay only" model breaks

Model: edge forwards REGISTER verbatim to the downstream PBX; PBX stores
`Contact: <sip:user@192.168.1.50:5060>` (client's private address) or, with
NAT-awareness on the PBX, the observed source address as seen… **from the PBX's point of
view — which is the edge's address**, not the client's NAT binding.

Failures, concretely:

1. **Private Contact unreachable.** PBX later sends INVITE to 192.168.1.50:5060 → nowhere.
2. **Wrong "received" view.** If the PBX rewrites/stores the observed address
   (Asterisk `rewrite_contact`, nathelper-style fixes), it stores the *edge's* IP:port —
   packets from the PBX then go back to the edge which has no idea which client/flow the
   packet is for (the edge isn't in the signalling path by default; nothing links that
   inbound packet to a registration).
3. **NAT binding expiry.** Even with a correct public NAT address stored, UDP NAT
   bindings die in ~15–60 s; nothing keeps them open if keepalives must originate at the
   PBX, which cannot punch through the NAT from inside for inbound requests. TCP/TLS/WS
   connections **cannot be opened inbound to the UA at all**.
4. **No topology control.** PBX talking directly to UACs defeats the point of an edge
   (TLS termination, single public IP, policy, encryption boundary).

**How Path fixes it:** edge inserts itself into the registration path
(`Path: <sip:edge;lr;received=sip:NATip:port>`); the registrar stores it per contact and
preloads it as Route on inbound requests. Now (a) the request necessarily passes the
edge, and (b) the edge learns the *current* NAT binding from its own `received=` param
(`path.use_received=1`). Keepalive can now be done at the edge (NAT ping / CRLF / STUN)
because the edge owns the client-side flow.

**How Outbound fixes it further:** for connection-oriented transports the Path URI alone
can't resurrect a dead TCP connection; the flow-token (`;ob` Route/Path) lets the edge
map the request onto the still-open connection, `Flow-Timer` lets the network dictate
keepalive cadence, and reg-id/instance-id let the registrar manage several explicit flows
per AoR (430 Flow Failed → try next flow).

---

## 5. Generic, scalable tenant routing (number → tenant downstream node)

Goal: replace hardcoded maps like `"01111111" -> "alpha"` with a DB-driven,
hot-reloadable structure at the edge.

| Option | Mechanism | Scale/lookup | Hot reload | Verdict |
|---|---|---|---|---|
| **mtree** | in-memory prefix tree, longest-prefix match, DB-loaded | very fast, memory-resident; max prefix 63 chars | `kamctl rpc mtree.reload` (kamcmd mtree.reload) | **recommended for E.164 prefix/number → tenant** |
| **dialplan** | regex/equality rules per dpid, DB-loaded | flexible rewriting (dp_translate), heavier per call | `kamctl rpc dialplan.reload` | good for number *normalisation/translation*, can complement mtree |
| **dispatcher** | destination sets, not a prefix matcher | node lists health-checked (OPTIONS) | `kamctl rpc dispatcher.reload` | use **for the node clusters** mtree points at |
| **htable / PV maps** | in-memory hash, exact key only | O(1) exact match, no prefix logic | `htable.reload` / RPC set | exact-number mappings only |
| **sqlops** (per-request SQL) | query per call | latency + no cache | implicit (always fresh) | avoid at scale |
| **domain** | domain table, is_local()/lookup | domain-based tenant resolution | `domain.reload` | if tenancy is keyed by SIP domain, not numbers |
| **alias_db** | alias_map lookup per AoR | single-value alias resolution | `alias_db.reload` | alias resolution, not tenant topology |

**Recommended pattern:** mtree maps E.164 prefix (or full number; longest-prefix wins, so
per-number overrides can coexist with range prefixes) → a packed value naming the
dispatcher set ids / trunk ids. Reloadable without restart, multi-column packing avoids
extra lookups.

```c
loadmodule "mysql.so"
loadmodule "mtree.so"
loadmodule "dispatcher.so"

# tree: name=tenant ; columns: prefix -> (ds_onnet_set, ds_pstn_set) packed comma-separated
modparam("mtree", "db_url", DBURL)
modparam("mtree", "mtree",
    "name=tenant;dbtable=tenant_route;cols='tprefix,dsset_onnet,dsset_pstn'")
modparam("mtree", "char_list", "0123456789+")   # allow E.164 '+'
modparam("mtree", "pv_value", "$var(mtval)")

# DB: tenant_route(tprefix VARCHAR, dsset_onnet VARCHAR, dsset_pstn VARCHAR)
#  e.g.  01111111   -> "11,31"      (alpha: home cluster set 11, trunk set 31)
#        2641       -> "12,31"      (another tenant range; longer prefix wins)
```

Usage:

```c
route[TENANT_RTBYNUMBER] {
    # normalise first if needed: dp_translate("10", "$rU/$rU");
    if (!mt_match("tenant", "$rU", "0")) {
        route(OFFNET_DEFAULT);      # no tenant owns this number -> default LCR/attendant
        return;
    }
    $var(ds_on)  = $(var(mtval){s.select,0,,});
    $var(ds_pstn)= $(var(mtval){s.select,1,,});

    if (!ds_select_dst($var(ds_on), "4")) {   # alg 4: round-robin within set
        # no home node active at all -> straight to tenant PSTN trunk
        if (ds_select_dst($var(ds_pstn), "4")) { t_relay(); exit; }
        send_reply("503", "No Route"); exit;
    }
    t_on_failure("TENANT_FALLBACK");
    t_relay();
    exit;
}
```

Reload workflows:

```
kamctl rpc mtree.reload            # (or kamcmd mtree.reload, optional tree name)
kamctl rpc dispatcher.reload
kamctl rpc dialplan.reload         # if dialplan is used for normalisation
kamctl rpc htable.reload tenant    # if htables hold exact mappings
```

mtree type-1 trees (`d:w` dstid:weight pairs) additionally support weighted, longest-prefix
selection with results into AVPs (pv_dstid/pv_weight/pv_count) — useful if one wants
weight-aware multi-node answers without dispatcher; for most cluster setups,
mtree → dispatcher-set is the cleaner composition.

---

## 6. "Tenant home cluster first, tenant PSTN trunk as fallback"

Several generic mechanisms that compose:

### a) dispatcher with per-tenant sets + failure_route (recommended primary pattern)

One dispatcher *setid per tenant home cluster*, another setid per tenant trunk gateway
group (the setids come from mtree, §5). dispatcher OPTION-pings nodes itself
(`ds_probing_mode`), failed nodes are taken out before routing.

```c
failure_route[TENANT_FALLBACK] {
    if (t_is_canceled()) exit;

    # next home node (same tenant cluster)
    if (t_check_status("408|486|500|503") && ds_next_dst()) {
        t_on_failure("TENANT_FALLBACK");
        t_relay(); exit;
    }

    # all home nodes exhausted or "not here": tenant PSTN trunk gateways
    if (t_check_status("404|408|480|486|500|503|603")) {
        if (ds_select_dst($var(ds_pstn), "4")) {
            append_hf("P-D-I-Tenant: $var(tenant)\r\n");  # hand tenant context to GW
            t_on_failure("TRUNK_FAILOVER");
            t_relay(); exit;
        }
    }
}

failure_route[TRUNK_FAILOVER] {
    if (t_check_status("408|500|503") && ds_next_dst()) { t_relay(); exit; }
}
```

Relevant dispatcher algorithms for `ds_select_dst(set, alg)`: `4` round-robin, `6` random,
`9` weight-based (`rweight` in attrs), `8` priority-ordered serial dispatch
(`priority` attribute in attrs), `10` call-load distribution; alg `"X"` also appends the
remaining entries as branches. `ds_next_dst()` / `ds_mark_dst()` handle serial failover
and state. See kamailio.org/docs/modules/stable/modules/dispatcher.html.

### b) q-values + core serial forking

`serialize_branches(n)` / `next_branches()` are **core functions** (migrated from LCR into
the core in the OpenSER era), and work on the destination set with q values — usable after
`lookup()` or manual `append_branch(uri, q)`:

```c
append_branch("sip:$rU@home1.tenant", "1.0");
append_branch("sip:$rU@home2.tenant", "1.0");   # same q as home1 -> parallel pair
append_branch("sip:$rU@trunk-gw",     "0.5");   # lower q -> serial fallback
serialize_branches(1);
if (!next_branches()) { send_reply("404","No route"); exit; }
t_on_failure("SERIAL");
t_relay();
...
failure_route[SERIAL] {
    if (next_branches()) { t_relay(); exit; }
}
```

Parallel branch groups = equal q, tried together; serial order = ascending q cost.

### c) tm contact iteration: t_load_contacts / t_next_contacts

When the contact set comes from a registrar lookup (registrar-side design) or when
dispatcher/append_branch created a dset you want tm-tracked:
`modparam("tm","contacts_avp","tm_contacts")` (+`contact_flows_avp` for outbound),
then `t_load_contacts(); t_next_contacts();` in request route and more
`t_next_contacts()` in `failure_route` (see the outbound module's registrar example in
the docs — it is the canonical 5626 multi-flow failover template).

### d) lcr module

`load_gws()` / `next_gw()` give classic least-cost prefix→gateway routing with failure
recycling. It is oriented to PSTN carrier selection (per-prefix rates), not to tenant
on-net vs off-net; usable for the "off-net" leg but mtree+dispatcher is the better fit
for tenant topology. lcr also supports `lcr.reload` RPC.

---

## 7. AUTH relay: passing downstream 401/407 through the edge

When the edge is a pure proxy for REGISTER, authentication is **end-to-end** between UA
and downstream registrar — RFC 3261 digest challenges traverse the proxy untouched.
This is the desired model (edge never learns/handles account credentials):

- The edge must **not** call `www_authorize()`/`proxy_authorize()`/`pv_*` auth helpers
  on REGISTERs it relays downstream; doing so double-challenges and steals the challenge.
  (Authenticate at the edge only what the edge itself terminates, e.g. its own domains,
  using auth_db with a separate realm — and don't mix.)
- Do not strip `WWW-Authenticate:`/`Proxy-Authenticate:`/`Authentication-Info:` in reply
  routes. The nonce is generated downstream; the UA computes the response against the
  downstream realm.
- **Realm consistency**: the realm the downstream sends must be one the UA believes it is
  registering to (usually the AoR domain). With multitenancy per tenant domain, make sure
  the PBX realm per tenant matches the tenant domain, and the edge `alias=`/advertised
  hostnames don't rewrite domains in From/To (they must not — edge never touches them).
- Don't `consume_credentials()` on messages headed downstream; the downstream needs the
  Authorization header. Also beware `uac_auth()`: it is **uac module (Kamailio acting as
  a UAC)** — used when the edge must *answer* a challenge itself, e.g. authenticating to
  the carrier/PSTN side or on behalf of a trunk:
```c
if (t_check_status("401|407")) { uac_auth(); t_relay(); }   # edge->trunk auth
```
  Typical use in failure/onreply routes for outbound trunk calls; it uses the `uac`
  `auth_username/auth_password/auth_realm` (or `credential`) params. It must never be
  applied on the UA-facing leg.
- REGISTER over a fresh TCP/TLS flow after 401: digest nonces with `qop` interplay are
  fine across proxies; just ensure `force_rport=yes` / rport + received so the challenged
  retry arrives consistently.

---

## 8. Transport: persistent TCP/TLS edge ↔ downstream, GW auth models

### Edge ↔ downstream registrar/PBX

```
tcp_connection_lifetime = 3605   # cookbook example; default is 120 (too short);
                                 # NOTE: 0 does NOT mean "infinite" on these versions,
                                 # it makes connections close quickly - don't use 0.
tcp_keepalive = yes              # default on; SO_KEEPALIVE
tcp_keepidle  = 30               # start probes after 30s idle (Linux)
tcp_keepintvl = 5
tcp_keepcnt   = 6
tcp_connection_match = 1         # stricter match (incl. local port) when reusing
tcp_fd_cache  = yes              # default
tcp_crlf_ping = yes              # default; RFC 5626 CRLF ping/pong on the UA side too
```

Kamailio **automatically reuses** an existing outbound TCP/TLS connection when the next
message targets the same destination socket, so a dispatcher entry (or $du) pointing at a
PBX over TCP/TLS yields one long-lived connection per (edge, PBX) pair. Keep
`tcp_connection_lifetime` comfortably above any application keepalive cadence of the
downstream node and its Flow-Timer; conversely an edge that fronts UA-side TCP/TLS
clients must keep UA connections as long as registrations last (the edge cannot reopen
them into NAT; for RFC 5626 flows, a dead connection = 430 on next inbound).

Path over TCP: the Path URI is just an address; record the transport on it
(`add_path("edge", "transport=tcp")` or rely on the receiving UA's transport), or use
`sockname_mode=1` + named sockets to disambiguate on multi-homed edges. `enable_r2=1`
(double-Path with `r2=on`) handles asymmetric in/out sockets (e.g., TLS in, UDP out).

### Edge → tenant PSTN trunk gateway (e.g. Grandstream HT813 ATA)

Two models:

1. **IP-based peer auth (most common with ATAs/trunks):** the gateway accepts calls only
   from the edge's IP; the edge accepts from the gateway's IP
   (`permissions` module `allow_trusted()`, or an address table). Simple to operate with
   devices like the HT813; rotate-safe only if the ATA supports hostname/DNS peers —
   otherwise pin IPs. Weak against spoofing unless the link is private/LAN or VPN.
2. **TLS mutual auth:** run TLS edge↔gateway with per-peer client certificates.
   Kamailio `tls` module supports multiple TLS profiles in `tls.cfg`:
```ini
[client:gw-tenant1]
verify_certificate = yes
require_certificate = yes
certificate = /etc/kamailio/tls/edge-gw1.pem
private_key = /etc/kamailio/tls/edge-gw1.key
ca_list = /etc/kamailio/tls/ca-gw1.pem
server_name = gw1.tenant1.example
verify_depth = 3
```
   (profile matched by SNI/server_name for client profiles). Small ATAs have limited TLS
   configurability (cert upload, validation toggles); the HT813 supports TLS 1.2 with
   custom certs — workable but operationally heavier; combine with IP ACLs regardless.
   TLS also removes NAT/received mangling questions on that leg entirely.

Digest auth to a carrier on that leg is the `uac_auth()` pattern from §7.

---

## 9. The "multiple sources of truth" pitfall: usrloc at the edge

`usrloc` `db_mode` values (usrloc module docs): **0** memory-only (SQL-NULL, default),
**1** write-through, **2** write-back (timer-flush), **3** DB-only (no cache; direct SQL
ops), **4** load-from-DB-at-startup-then-memory-only.

In the edge-proxy architecture the only authoritative location store is the downstream
registrar's. Running usrloc at the edge creates a second copy that *will* diverge:

- **Different views of the client**: the edge sees the NAT binding; the PBX sees the
  edge's address (or the Path vector). Two stores with different contact/received values
  for the same AoR. Failover decisions taken at the edge (404/parallel forking) can differ
  from what the PBX would have done.
- **Expiry skew**: write-back and memory modes flush/expire on edge-local timers; the PBX
  may already have expired the contact (or vice versa). After an edge restart with
  db_mode 0/4 the edge's table is empty while the PBX still believes clients registered —
  or the reverse ghost-contact problem.
- **Sharing the same DB table is worse**: Kamailio `location` schema ≠ Asterisk
  `ps_contacts` ≠ FreeSWITCH `sip_registrations`; even between two Kamailios, cached modes
  on a shared table corrupt each other's caches (hence usrloc's `server_id_filter`).
- **Outbound flow state is edge-local**: flow tokens decode to connections on one edge
  node; a second edge's usrloc can't represent that.

Rule of thumb:

- At the edge: **do not call `save()` / `lookup()`**. No usrloc needed for the relay
  model — routing derives from mtree/dispatcher (tenant topology) and Path/Route/flow
  tokens (per-subscriber reachability).
- If a transient usrloc at the edge is ever needed (e.g., caching per-request hints,
  `registered()`-style checks for local policy), keep it **memory-only (db_mode 0)**,
  treat it as a cache that may be wrong, and never let it make final reachability
  decisions (always fall through to the downstream).
- Only the node that actually owns the AoR runs `registrar`+`usrloc` (db_mode 1/2/3 as
  appropriate for that tier).

---

## Key complete config sketch: edge (kamailio.cfg style)

```c
#!KAMAILIO
####### Global #########
debug=3
fork=yes
children=8
alias="edge.example.net"
tcp_connection_lifetime=3605
tcp_crlf_ping=yes
tcp_keepalive=yes
force_rport=yes

mpath="/usr/lib/x86_64-linux-gnu/kamailio/modules/"

####### Modules ########
loadmodule "tm.so"
loadmodule "sl.so"
loadmodule "rr.so"
loadmodule "outbound.so"      # BEFORE path so tokens get embedded
loadmodule "path.so"
loadmodule "stun.so"          # RFC 5626: STUN keepalive server on UDP
loadmodule "textops.so"
loadmodule "siputils.so"
loadmodule "pv.so"
loadmodule "xlog.so"
loadmodule "sanity.so"
loadmodule "maxfwd.so"
loadmodule "mysql.so"
loadmodule "mtree.so"
loadmodule "dispatcher.so"
loadmodule "dialplan.so"      # optional, number normalisation

#!substdef "/FLOW_TIMER/20/"

modparam("tm", "failure_reply_mode", 3)
modparam("outbound", "flow_token_secret", "shared-secret-across-edge-nodes")
modparam("path", "use_received", 1)

modparam("mtree", "db_url", "mysql://kam:kam@db/voip")
modparam("mtree", "mtree",
    "name=tenant;dbtable=tenant_route;cols='tprefix,dsset_onnet,dsset_pstn,dsset_registrar'")
modparam("mtree", "char_list", "0123456789+")
modparam("mtree", "pv_value", "$var(mtval)")

modparam("dispatcher", "db_url", "mysql://kam:kam@db/voip")
modparam("dispatcher", "ds_probing_mode", 1)
modparam("dispatcher", "ds_ping_interval", 30)

####### Routing ########
request_route {
    route(SANITY);

    if (is_method("CANCEL")) { if (t_check_trans()) t_relay(); exit; }
    t_check_trans();

    # -------- in-dialog: follow route set (record-route at edge) --------
    if (has_totag()) {
        if (loose_route()) { t_relay(); exit; }
        if (is_method("ACK") && t_check_trans()) { t_relay(); exit; }
        sl_send_reply("404","Not Here"); exit;
    }

    # -------- REGISTER: relay to the tenant's registrar with Path --------
    if (is_method("REGISTER")) {
        # which registrar? by To-domain (multi-tenant domains) or To-user range:
        if (!mt_match("tenant", "$tU", "0"))
            mt_match("tenant", "$td", "0");            # fallback: by domain
        if ($var(mtval) == $null) { sl_send_reply("403","Unknown Tenant"); exit; }
        $var(ds_reg) = $(var(mtval){s.select,2,,});

        append_hf("Supported: path\r\n");
        if (!add_path_received("edge")) {
            sl_send_reply("503","Internal Path Error"); exit;
        }
        if (!ds_select_dst($var(ds_reg), "4")) {
            sl_send_reply("503","Registrar Down"); exit;
        }
        t_relay(); exit;
    }

    # -------- requests coming back FROM a downstream node (to subscribers) --------
    if (ds_is_from_list()) {
        # registrar preloaded Route from stored Path vector
        check_flow_token();   # -5/-6 -> 430; -3/-1 -> plain loose_route
        switch($rc) {
        case -5: case -6:
            sl_send_reply("430","Flow Failed"); exit;
        case -4: case -2: case -7: case -8:
            sl_send_reply("403","Forbidden"); exit;
        }
        loose_route();
        t_on_failure("FLOW_TIMEOUT");
        t_relay(); exit;
    }

    # -------- initial requests from UACs / trunks: tenant routing by number --------
    if (is_method("INVITE|SUBSCRIBE|MESSAGE")) {
        record_route();
        route(TENANT_RTBYNUMBER);   # mt_match + ds_select_dst + TENANT_FALLBACK (§5/§6)
        exit;
    }

    route(RELAY);
}

route[RELAY]   { if (!t_relay()) sl_reply_error(); exit; }
route[SANITY] {
    if (!mf_process_maxfwd_header("10")) { sl_send_reply("483","Too Many Hops"); exit; }
    if (!sanity_check("1511","7")) { exit; }
}

onreply_route {
    if ($rm=="REGISTER" && $rs>=200 && $rs<=299) {
        remove_hf("Flow-Timer");
        if ($(hdr(Require)[*]) =~ "outbound")
            insert_hf("Flow-Timer: FLOW_TIMER\r\n", "Call-ID");
    }
}

failure_route[FLOW_TIMEOUT] {
    if (t_branch_timeout() && !t_branch_replied())
        send_reply("430", "Flow Failed");
}
```

Downstream registrar, if Kamailio (minimal per outbound docs):

```c
modparam("registrar", "use_path", 1)
modparam("registrar", "path_mode", 1)        # lazy: echo Path if UA said Supported: path
modparam("registrar", "path_use_received", 1)
modparam("registrar", "outbound_mode", 1)
modparam("registrar", "regid_mode", 1)
modparam("registrar", "gruu_enabled", 1)
modparam("tm", "contacts_avp", "tm_contacts")
modparam("tm", "contact_flows_avp", "tm_contact_flows")
# route(REGISTRAR): save("location");
# route(LOCATION):  lookup("location"); t_load_contacts(); t_next_contacts(); ...
```
(The full canonical edge+registrar pair is Example 1.1/1.2 of the outbound module doc —
use it verbatim as the reference implementation.)

---

## References

- RFC 3327 — SIP Extension Header Field for Registering Non-Adjacent Contacts (Path).
- RFC 5626 — Managing Client-Initiated Connections in SIP (Outbound). §5 edge proxy duties.
- RFC 5627 — Obtaining and Using GRUUs in SIP.
- RFC 6223 — Indication of Support for Keep-Alive.
- Kamailio module docs: `path`, `outbound`, `registrar`, `usrloc`, `mtree`, `dispatcher`,
  `dialplan`, `tm`, `uac`, `stun` — kamailio.org/docs/modules/stable/modules/*.html
- Kamailio core cookbook (tcp_connection_lifetime, tcp_crlf_ping, tcp_keepidle/…),
  kamailio.org/wikidocs/cookbooks/*/core
- Asterisk: `res_pjsip_path` (ASTERISK-21084), AOR `support_path`; bugfix ASTERISK-30100
  "Path is ignored on INVITE to endpoint" in Asterisk 18.17/20.2; sr-users thread
  "path support fix for asterisk" (Mar 2023).
- FreeSWITCH mod_sofia Path storage/`fs_path` behaviour and the `received=` quirk:
  freeswitch-users, Jan 2017 (fs_path thread) and Feb 2018 ("received= in path header").
- serialize_branches()/next_branches() in core since OpenSER (Klaus/Bogdan sr-users threads).
