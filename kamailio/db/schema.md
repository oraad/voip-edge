# Edge database schema + reload workflow

Reference implementation: **sqlite** (`db/seed.sql` → `create-db.sh` →
`edge.sqlite`, wired via the `DBURL` `#!define` in `kamailio.cfg`). No external
DB server needed. To move to MySQL/PostgreSQL: keep table and column names
identical, create them with `kamdbctl` or ported SQL, and override the URL:

```bash
kamailio -f kamailio.cfg -A 'DBURL="mysql://kam:secret@db/voip_edge"'
```
(and for kamctl set `DBENGINE=MYSQL`/`DBHOST=...` in `kamctlrc`).

All tables are read into memory at start; after any data change run the
reloads below — **no restart, no script edits, adding a home is pure data**.
Commands are shown for the container deployment (`docker exec voip-edge`);
drop the prefix on bare metal.
| Table | Module / use | Reload (docker) |
|---|---|---|
| `tenant_registry` | mtree `tenantdom`: SIP domain → tenant record | `docker exec voip-edge kamcmd mtree.reload tenantdom` |
| `tenant_landline` | mtree `landline`: dialed digits → target record | `docker exec voip-edge kamcmd mtree.reload landline` |
| `dispatcher` | destination sets (homes' voip-stack + PSTN gateways) | `docker exec voip-edge kamcmd dispatcher.reload` |
| `dialplan` | emergency patterns per country (dpid) | `docker exec voip-edge kamcmd dialplan.reload` |
| `trusted` | permissions: tenant detection by source IP | `docker exec voip-edge kamcmd permissions.trustedReload` |

(kamctl/kamdbctl with sqlite would need `DBENGINE=SQLITE`, blank `DBHOST` and
`DB_PATH` pointing at the file — only relevant for DB administration, which
this repo does via plain SQL in `seed.sql`; the RPCs above never read the DB
file themselves — Kamailio does.)

## tenant_registry (mtree `tenantdom`)

Exact-match domain table, `(tprefix, tvalue)`. `tvalue` is the packed tenant
record — fields comma-joined in this order, extracted in the script with
`{s.select,idx,,}`:

| select idx | field | meaning |
|---|---|---|
| — | `tprefix` | SIP domain as phones/homes use it (matched as mtree longest-prefix) |
| 0 | tenantid | opaque short id (`T1`, …) |
| 1 | ds_onnet | dispatcher set id of the home's **voip-stack** (tunnel address) |
| 2 | ds_pstn | dispatcher set id of the home's **PSTN gateway(s)**; **0 = no trunk** |
| 3 | cli | landline number **as dialed locally** (no E.164 assumption) |
| 4 | label | display name for inter-home From rewrite (no commas) |
| 5 | dpid | dialplan partition holding this home's **emergency patterns** |
| 6 | flags | `"ih_on+pstn_on+block_dpid"` (`+`-packed, third part optional) |

`ih_on`: tenant participates in inter-home calling — **both** origin and
target tenant must have it on. `pstn_on`: on-net inter-home failure may fail
over to this tenant's PSTN set (Phase 2 step 3). `block_dpid`: dialplan
partition id listing destinations that may NEVER leave via a trunk (premium /
audiotex ranges); the edge answers 403. `0` or omitted = nothing blocked.

### dialplan partition conventions

| dpid | use |
|---|---|
| free (operator-defined; none seeded) | emergency "country profile" — matched calls go straight to the origin home's PSTN set (Phase 2 step 1) |
| free (operator-defined; none seeded) | blocked/premium destinations — matched calls get 403 before any routing |

Emergency vs blocked differ in one crucial way: emergency still REACHES the
origin home's PSTN gateway (never on-net, never blocked); premium matches are
rejected outright.

> The default 2-column mtree form is used (record packed into `tvalue`) —
> db_sqlite only handles that load reliably. With MySQL/PostgreSQL you may
> instead add real columns and an mtree `cols=…` spec; the script's packed
> string stays identical either way. Column type must map to DB1_STRING
> (declare as CHAR/VARCHAR; sqlite sees only CHAR or CLOB as strings).

## tenant_landline (mtree `landline`)

Longest-prefix match on dialed digits. One row per landline (or a shorter
prefix covering a delegated range; longer full-number rows win).
`tvalue = tenantid,target,ds_onnet,ih_on`:

| select idx | field | meaning |
|---|---|---|
| — | `tprefix` | digits as dialed, local format |
| 0 | tenantid | target tenant id |
| 1 | target | ring group / extension name (new R-URI user) |
| 2 | ds_onnet | dispatcher set id of the target home's voip-stack |
| 3 | ih_on | 1 = this landline accepts inter-home calls |

## dispatcher

Standard Kamailio dispatcher table. Only the setid/destination conventions
matter:

* per tenant two set ids: voip-stack set and PSTN-gateway set (ids are free;
  the seed uses `11/12/13` on-net and `31/32/33` PSTN as examples),
* `destination` for a voip-stack: its **home LAN IP** (reached through the
  site's native tunnel routing), e.g. `sip:10.0.0.10:5060;transport=tcp`.
  **TCP is required** over the 1280-MTU tunnel for any SDP-bearing SIP — UDP
  fragments die silently there. Pangolin `*.internal` resource names are the
  legacy convention and are not used.
* `destination` for a PSTN gateway: this deployment routes PSTN via the
  tenant's voip-stack (HT813 stays LAN-only), so the PSTN set destination is
  the same LAN-IP URI with `;transport=tcp`. (Generic alternative: a direct
  ATA address over any LAN-routed tunnel.)
* destinations are OPTIONS-pinged (`ds_probing_mode=1`); any SIP response
  class counts as alive (`ds_ping_reply_codes` accepts 1xx–5xx), so ATAs that
  answer 405/404 to OPTIONS still stay active.

## dialplan (emergency)

`dpid` groups = "country profiles"; `tenant_registry.dpid` selects one per
home. Only matching matters (identity rewrite): edge goes straight to the
origin home's PSTN set when a row matches the dialed `$rU`. Add/adjust
patterns as rows — never in kamailio.cfg. Example rows in the seed are
clearly marked fictional.

## trusted

One row per tenant: `src_ip` = voip-stack tunnel address, `proto=any`, `tag` =
tenant domain. `allow_trusted()` on overlay-originated requests yields the tag,
which is then resolved through `tenant_registry`. Because the tunnel
authenticates the peer and the tag decides the caller-id rewrite, one tenant
cannot present another tenant's landline **by construction** (the From header
is never used for tenant attribution).
