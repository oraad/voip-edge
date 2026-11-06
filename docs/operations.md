# Operations runbook — VoIP Edge Console

Operator-facing skeleton. It covers the console only; the edge stack (Kamailio,
Pangolin, TLS, rtpengine) is documented in
`/root/voip-stack/kamailio-edge/DEPLOY.md`. Do not copy that file here — link to
it so the two deployment stories cannot drift.

The console is a single-process FastAPI app. Run exactly one instance
(`workers=1`); reload serialization, sidecar state and the write lock are
process-local.

## Environment

All values are read at import from the process environment; restart the console
after changing them. Defaults below come from `console/settings.py`; the
production values in the notes come from plan §10. `CONSOLE_SECURE_COOKIE`,
`CONSOLE_SESSION_TTL` and `CONSOLE_LOGIN_RATE` are implementation extensions
from plan §2.

| Variable | Default | Purpose / production note |
|---|---|---|
| `CONSOLE_TOKEN` | random per process | Shared login token. **Set a strong value in production.** If unset/blank a random token is generated, so no one can sign in. The token is a second factor in front of Pangolin SSO, not a replacement for it. |
| `CONSOLE_SECRET` | random per process | Session/flow cookie signing secret. **Set an explicit random value.** It is never derived from the token; a generated secret invalidates sessions on restart. |
| `CONSOLE_BIND` | `127.0.0.1` | Production: `100.64.0.9` (host-networked container, reachable only through Pangolin). |
| `CONSOLE_PORT` | `8080` | Pangolin resource target port. |
| `CONSOLE_DB_PATH` | `edge.sqlite` | Absolute path to the mounted Kamailio DB, e.g. `/data/edge.sqlite` (host: `${EDGE_HOME}/kamailio/db/edge.sqlite`). Never point at a copy. |
| `CONSOLE_SIDECAR_DIR` | `console-data` | Directory for `console.sqlite` and backups. Production: `/var/lib/console` (host: `${EDGE_HOME}/console`). The directory must exist. |
| `CONSOLE_RPC_SOCK` | `/run/kamailio/kamailio_rpc.sock` | Kamailio `jsonrpcs` unix datagram socket; the console needs read/write access and the shared `console` group. |
| `CONSOLE_RPC_TIMEOUT_MS` | `2000` | Per-RPC timeout. A timeout is surfaced as an unreachable edge, not a crash. |
| `CONSOLE_THROTTLE_TENANTDOM` | not read | Reserved. Per-table throttles are authoritative in `console/tables.yaml` (`reload.throttle_seconds`, 5 s per table). |
| `CONSOLE_ALLOWED_SUFFIXES` | `home.example.net` | Comma-separated allowed home suffixes. Keep in sync with `TENANTSUFFIXRE` in `kamailio/kamailio.cfg`; the edge also enforces it before the mtree lookup. |
| `CONSOLE_POLL_MS` | `15000` | Live-dashboard polling interval. In the lab use ~500 ms; never use `wait_for_timeout` in tests. |
| `CONSOLE_READONLY` | `0` | `1` gates every mutating route, disables action buttons and shows the read-only banner. Use for the first production session. |
| `CONSOLE_TLS_LIVE` | `1` | Enables the wizard's mandatory TLS-SAN notice (`ACME_DOMAINS` + force-reissue). |
| `CONSOLE_SECURE_COOKIE` | `0` | Set `1` when Pangolin terminates HTTPS; adds `Secure` to the session cookie. |
| `CONSOLE_SESSION_TTL` | `43200` | Absolute session lifetime in seconds (12 h); there is no sliding refresh. |
| `CONSOLE_LOGIN_RATE` | `5` | Failed auth attempts per minute per IP before a 429. |
| `CONSOLE_SWEEPER_INTERVAL` | `30` | Seconds between lifecycle-intent/reload sweeps; `0` disables (tests use 0). |
| `CONSOLE_ENFORCE_CAPABILITIES` | `0` | `1` refuses park/destroy up front when `htable.delete` is proven unavailable. Set `1` in production. |
| `OIDC_ALLOW_ANY` | `0` | Explicitly allow any authenticated subject when no group/email-domain allowlist is configured. SSO fails closed without it. |
| `OIDC_INSECURE` | `0` | Allow `http://` issuer/endpoints for loopback development only. |

### OIDC configuration

The full SSO surface is `CONSOLE_AUTH_MODE`, `OIDC_ISSUER`, `OIDC_CLIENT_ID`,
`OIDC_CLIENT_SECRET`, `OIDC_REDIRECT_URI`, `OIDC_SCOPES`,
`OIDC_POST_LOGOUT_URI`, `OIDC_ALLOWED_GROUPS`,
`OIDC_ALLOWED_EMAIL_DOMAINS`, `OIDC_PROVIDER_NAME`, `OIDC_ALLOW_ANY` and
`OIDC_INSECURE`; see `.env.example` for defaults and `docs/oidc.md` for the
provider registration and threat model.

## Backups and restore

Backups use `console.backups.create_backup`: the SQLite online backup API, never
a file copy of a running database. Each backup is `edge-<UTC>-<tag>.sqlite`
with a sibling `.console.sqlite.bak` (WAL-safe sidecar copy, including parked
snapshots) and a readable `.parked.json` dump. Rotation keeps the newest **5
globally** and removes all three companions together. A backup is taken before
every edge-mutating lifecycle operation (park, restore, active destroy) and
before wizard apply; parked destroy only removes the sidecar snapshot.

Backups live in the configured backup directory (the deployment convention is
`${CONSOLE_SIDECAR_DIR}/backups`). The `.parked.json` file is disaster-recovery
evidence for parked homes; it is not imported automatically into the sidecar.

### Restore procedure

**Never hot-swap the database.** A running Kamailio or console can hold WAL
state and will lose writes or corrupt the file if the inode is replaced under it.

1. Choose the backup: newest suitable `edge-*.sqlite`, or the specific tag taken
   before the failed operation.
2. Stop the writers: `voip-edge-console`, then `voip-edge` (Kamailio). Confirm
   neither container is running.
3. Preserve the current file (rename or copy it aside); do not overwrite it.
4. Replace `edge.sqlite` with the chosen backup. Remove stale
   `edge.sqlite-wal` and `edge.sqlite-shm` files from the old database; they do
   not belong to the restored file.
5. Start `voip-edge` first, then `voip-edge-console`. The console re-asserts
   `journal_mode=WAL` and `busy_timeout` on startup.
6. Verify: `/health` returns 200, the Homes list reads, the status strip shows
   the edge reachable, and no pending reloads remain. If a reload is still
   pending after Kamailio is up, the startup sweeper will re-issue it.
7. Keep `console.sqlite` and its parked snapshots unless the sidecar itself is
   being recovered. Do not copy `.parked.json` into `edge.sqlite`.

A backup failure must abort the operation before any edge mutation. If you see
`BackupError`, fix the path/permissions and retry the lifecycle action; do not
proceed without a backup.

## Tenant lifecycle

All lifecycle work is durable and resumable. Each operation writes an intent
row in `console.sqlite`; the process sweeper resumes the oldest unfinished
intent at startup and every 30 s. A failed step keeps its last committed state
and records `last_error`; the sweeper retries it and the Audit page shows the
error. There is no separate Retry button.

### Park (active home)

UI: Homes → home → **Park**. One warning-styled confirmation (not typed).

Sequence: backup → snapshot into `console_parked` → delete that home's registry,
landline, dispatcher and trusted rows → reload all four tables → purge every
regcache key ending `@<domain>` → dump-verify zero → done. Reload runs **before**
purge so new registrations are rejected at the mtree level before cleanup. A
residual in-flight REGISTER is accepted; it ages out at its autoexpire TTL and
no delayed re-sweep is scheduled.

Operator expectation, verified manually in the lab: calls already in progress
finish; new requests receive 403/404. Do not promise this from console tests.

### Restore (parked home)

UI: Parked section → **Restore**. Collision check blocks on any of the five
classes: domain, tenantid, landline digits, dispatcher set ids, trusted
`src_ip`; the error names the colliding row. Then a backup, guarded inserts
(never `OR REPLACE`), reload, and the coherence checklist must go green.

### Destroy

- **Active home:** type the full domain to confirm. Runs the park sequence
  (including regcache purge), then discards the snapshot on success.
- **Parked home:** normal consequence-naming confirmation; deletes the snapshot
  only.
- Shared dialplan partitions (for example 10 and 90) are never deleted; the
  console does not add a post-destroy “unreferenced partition” indicator.

If the sidecar is unreadable, destructive operations are refused before
`edge.sqlite` is touched; browsing continues.

## Reload troubleshooting

Reloads are **scheduled → applied → verified → failed**. A save commits to the
database immediately; the reload is enqueued. Per-table windows coalesce saves;
the window starts at the first save and is not extended by later saves. Exactly
one RPC is sent per table per window, serialized by `ReloadWorker`.

### Pending reloads

- `console_pending_reload` in `console.sqlite` persists `table` +
  `enqueued_at` while Kamailio is unreachable. A console restart loses nothing.
- The sweeper re-derives the RPC and params from the descriptor and re-issues
  the reload on its next pass.
- Symptom: global banner shows a pending count, writes still work. Fix the edge
  or socket, then wait for the sweep (or restart the console).
- A JSON-RPC reload fault is recorded in the worker's pending-reload error and
  the structured logs; the status strip shows the pending count. There is no
  per-save toast. Old data remains active — fix the data/RPC and save again.

### Live polling stops on a down edge

`GET /live/partial` always returns 200 with the banner. When the edge is
unreachable it adds `HX-Stop-Polling: true`, so htmx stops hammering the dead
socket. Polling resumes on the next page load or navigation back to Live. This
is intentional; it is not a console crash.

The Docker healthcheck tests console liveness only (`/health` is 200 even when
Kamailio is down). A healthy container does **not** mean the edge is reachable;
read the status strip / `core.uptime` surface.

## Exposure and Pangolin SSO

Production binds `100.64.0.9:8080` on host networking and is exposed only
through a Pangolin resource with SSO in front. The console token remains ON as a
second factor. Do not publish the port directly or bypass the resource.

- Terminate TLS at Pangolin and set `CONSOLE_SECURE_COOKIE=1`.
- `/health` is intentionally unauthenticated so the container healthcheck and
  Pangolin can probe liveness; it reveals only `{"status":"ok"}`.
- First production session: `CONSOLE_READONLY=1`, verify reads and the status
  strip, then enable writes.
- Follow the existing Pangolin resource pattern in
  `/root/voip-stack/kamailio-edge/DEPLOY.md` §C and the concrete steps in
  `docs/ops/deploy.md` rather than inventing a second exposure path.
