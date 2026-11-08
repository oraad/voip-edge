# Operations

Day-2 tasks for the whole stack. First deploy: `docs/ops/deploy.md`. TLS:
`docs/tls.md`. Backups: `docs/ops/backups.md`.

The console runs one uvicorn worker. Reload serialization, queues, locks and
session state are process-local.

## Edge services

```bash
docker compose ps
docker logs -f voip-edge
docker logs -f voip-rtpengine
docker exec voip-edge kamcmd core.uptime
docker exec voip-edge kamcmd dispatcher.list
docker exec voip-edge kamcmd mtree.list tenantdom
docker exec voip-edge kamcmd permissions.trustedDump
docker exec voip-edge kamcmd rtpengine.show all
```

Routing tables are memory-cached. After an out-of-band DB edit, run the
matching reload (`mtree.reload`, `dispatcher.reload`, `dialplan.reload`,
`permissions.trustedReload`). The console reloads after its own saves.

Never replace `edge.sqlite` under a running Kamailio. Restore only through
`docs/ops/backups.md`.

Homer is optional:

```bash
docker compose --profile homer ps
docker compose --profile homer logs -f heplify-server
docker compose --profile homer stop heplify-server homer-app homer-db
```

Stopping Homer does not affect SIP or media. To stop HEP mirroring:
`docker exec voip-edge kamcmd siptrace.status off`.

## Console settings

All settings are in `.env`; the annotated list is `.env.example`. Restart the
console after a change. Critical values:

| Variable | Note |
|---|---|
| `CONSOLE_TOKEN` | login token; set explicitly |
| `CONSOLE_SECRET` | session signing secret; set explicitly |
| `CONSOLE_BIND` | tunnel address, e.g. `100.64.0.9` |
| `CONSOLE_READONLY` | `1` gates every write |
| `TENANT_ALLOWED_SUFFIXES` | drives the validator, wizard dropdown and generated Kamailio suffix gate |
| `EDGE_TRUNK_HOST` | optional literal edge tunnel address shown in the wizard trunk note |
| `EDGE_TRUNK_PORT` | edge trunk port shown in the note; default `5060` |
| `CONSOLE_RPC_REPLY_DIR` | shared reply-socket dir when the console is on a bridge |
| `CONSOLE_AUTH_MODE` | `both`, `oidc` or `token` |
| `CONSOLE_ENFORCE_CAPABILITIES` | `1` refuses unsafe lifecycle actions |

## Home checks

The Homes list computes a DB-only coherence badge for each home. It never calls
Kamailio. Open `/homes/<domain>` for the full checklist, the PBX and PSTN
targets, any last reload failure, and the registered phones.

`Run live checks` calls `core.uptime`, `dispatcher.list`,
`permissions.trustedDump`, `htable.dump trunkseen` and `regcache` for that
home. It is on demand so the detail page still renders when the edge is down.
The onboarding `/wizard/verify/<domain>` page remains available and uses the
same checks.

Kamailio records the last SIP request from each trusted source in the
`trunkseen` htable. The live panel reports `PBX → edge: trunk heartbeat` as
seen or `Unconfirmed`. This is the only PBX-side evidence the edge can obtain
without a PBX API: a static trunk that sends no OPTIONS keepalive and carries
no calls stays unconfirmed. Ask the PBX admin to enable OPTIONS/qualify on the
edge trunk, or place a test call, to turn it green.

## Number rules

`/number-rules` is the simple editor for dialplan rows. It has three tabs:

- **Emergency** — exact numbers added to an emergency partition.
- **Blocked** — exact numbers, prefixes, ranges or a custom pattern.
- **Custom** — a raw pattern in any partition.

The partition dropdowns show which homes use the partition. Only a home's
emergency `dpid` and blocked-destinations partition affect calls; an unused
partition warns but still saves. `/tables/dialplan` remains the Advanced view
for existing rows and complex edits.

## Lifecycle

Each operation writes a durable intent in `console.sqlite`. The sweeper resumes
the oldest unfinished intent at startup and every 30 s (0 disables). A failed
step records `last_error`; fix the cause, the sweeper retries.

- **Park:** backup → snapshot → delete routing rows → reload → purge regcache.
- **Restore:** collision check → backup → guarded inserts → reload.
- **Destroy:** park sequence, then discard the snapshot (active home), or
  delete the snapshot only (parked home).

Read-only mode and schema mismatch pause lifecycle resumes and reload reissues.
A backup failure aborts before any edge mutation.

## Reloads

Saves commit to SQLite immediately and enqueue a reload. The worker coalesces
per table; one RPC per table per window. A pending row survives console
restarts and is re-issued by the sweeper.

If Kamailio is down, writes still work and the status strip counts pending
reloads. Fix the edge/socket and wait for the sweep, or restart the console.
Old data stays active until the reload succeeds.

## Exposure

Bind the console to the tunnel address and expose it only through Pangolin
SSO. Set `CONSOLE_SECURE_COOKIE=1` behind HTTPS. `/health` is public and always
200; it reveals only `{"status":"ok"}`.

Start with `CONSOLE_READONLY=1`, verify reads and status, then enable writes.
