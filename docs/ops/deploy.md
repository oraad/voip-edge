# Deploying the console in the edge stack

This is the console-specific deployment runbook. The Kamailio/Pangolin/TLS
stack itself is documented in `/root/voip-stack/kamailio-edge/DEPLOY.md`; read
that first and keep the two stories aligned rather than copying its steps here.

The console is a **single-process** FastAPI container. It:

- runs with **host networking** and binds `${CONSOLE_BIND}:${CONSOLE_PORT}`
  (`100.64.0.9:8080` in production);
- mounts the Kamailio DB **read/write** and the `jsonrpcs` run directory
  **read/write**;
- stores its own state (`console.sqlite`, backups) under
  `${EDGE_HOME}/console`;
- is exposed only through a **Pangolin resource with SSO**; the console token
  stays on as a second factor.

`workers=1` is a hard requirement: reload serialization, the coalescing
throttle queue, the transaction write-lock and session state are process-local.
Never add a second worker or a multi-process server.

---

## 1. Prerequisites

1. `voip-edge` (Kamailio 6.1.5) and its DB are running from the stock stack:
   - DB at `${EDGE_HOME}/kamailio/db/edge.sqlite`
   - run dir at `${EDGE_RUN}` (default `/run/kamailio-edge`)
2. A group named `console` exists with a known GID. It must exist in the
   **console image** (baked via `CONSOLE_GID`) and, for the non-root recipe,
   inside the **Kamailio container's `/etc/group`** too, because
   `dgram_group` is resolved by name *inside that container*. Docker
   `group_add` adds a numeric supplementary group; it does **not** create an
   `/etc/group` entry, so it is not enough on its own.

   On the host (convention and a stable GID):
   ```bash
   getent group console || groupadd -r console
   getent group console          # note the GID, e.g. console:x:995:
   ```
   With the default **root** console (section 6) the Kamailio-side group is
   not required: root can use a `0660` socket even if it ends up
   `root:root`. See the note in section 2.
3. The console bind address exists on the host. With host networking,
   uvicorn cannot bind `100.64.0.9` before the tunnel interface is up:
   ```bash
   ip -brief addr show | grep 100.64.0.9
   ```
4. The console image is built **once** (section 5). Do not build on every
   deploy; the image tag is pinned to the code (`docs/ops/releases.md`).
5. Outbound access is only needed at image-build time (pip/apt). The running
   container needs no network beyond the host stack; htmx/PicoCSS are vendored
   into the image and never fetched at runtime.

---

## 2. Enable `jsonrpcs` in Kamailio (one-time, approved restart)

The console speaks JSON-RPC over a **unix datagram socket**. Add the block from
plan §2 to `$EDGE_HOME/kamailio/kamailio.cfg` under the console ifdef:

```
#!ifdef WITH_CONSOLE
loadmodule "jsonrpcs.so"
modparam("jsonrpcs", "dgram_socket", "/run/kamailio/kamailio_rpc.sock")
modparam("jsonrpcs", "transport", 4)          # datagram only
modparam("jsonrpcs", "dgram_mode", 0660)
modparam("jsonrpcs", "dgram_group", "console")
modparam("jsonrpcs", "dgram_max_size", 262144)
modparam("db_sqlite", "db_set_journal_mode", "DBFILE=WAL")
modparam("db_sqlite", "db_set_busy_timeout", "DBFILE=300")
#!endif
```

Then restart `voip-edge` once with `WITH_CONSOLE` defined (the approved
one-time blip). Confirm the socket exists on the host:

```bash
ls -l "${EDGE_RUN}/kamailio_rpc.sock"
# expected with a resolvable group: srw-rw---- 1 root console ... kamailio_rpc.sock
# with the root console recipe, root:root 0660 is also usable
```

`dgram_group` is resolved by **name inside the `voip-edge` container**, not on
the host. The stock Kamailio image may not ship a `console` group, in which
case Kamailio cannot `chown` the socket to `console` and it is created
`root:root 0660` (check `docker logs voip-edge` for a getgrnam/chown warning).
That is harmless for the **default root console** and is why root is the
supported default. If your Kamailio build treats a missing group as fatal
instead of falling back, add the group to its image even for the root recipe.
For the non-root recipe the name must exist in both containers with the same
GID; see section 8.

> The console never falls back to `kamcmd` or binrpc. A missing/wrong socket
> surfaces as `KamailioUnavailable` and a pending-reload banner; the console
> still starts and serves reads.

---

## 3. Host directories and permissions

```bash
mkdir -p "${EDGE_HOME}/console"
# The sidecar and backups are written by the console process:
#   root:root by default (compose runs the console as 0:0)
#   console:console if you use the non-root recipe below
```

The stock stack's `edge.sqlite` is created by Kamailio as `root:root`, and
`edge.sqlite-wal` / `edge.sqlite-shm` are created next to it by whichever
process opens the database first. That is why `compose.yml` runs the console as
`root` by default: a non-root process cannot write a root-owned SQLite file or
its WAL/SHM siblings without extra ownership/ACL work.

The `jsonrpcs` socket is `0660` (ideally `root:console`; `root:root` when the
group name does not resolve inside the Kamailio container). Root can use it
regardless; in the non-root recipe the `console` group must resolve to the same
GID in **both** containers (section 8).

**Do not** `chmod 777` the DB directory as a shortcut. It exposes the routing
database to every process on the VPS and is not needed with the root default.

---

## 4. Environment

Copy `.env.example` to `.env` next to `compose.yml` (or paste the values into
the Arcane stack editor). At minimum change:

| Variable | Production value |
|---|---|
| `EDGE_HOME` | `/opt/voip-edge` (same as the edge stack) |
| `EDGE_RUN` | `/run/kamailio-edge` (same as the edge stack) |
| `CONSOLE_TOKEN` | long random value, distinct from `CONSOLE_SECRET` |
| `CONSOLE_SECRET` | long random value; never leave the dev fallback |
| `CONSOLE_BIND` | `100.64.0.9` |
| `CONSOLE_PORT` | `8080` |
| `CONSOLE_SECURE_COOKIE` | `1` when Pangolin terminates HTTPS |
| `CONSOLE_READONLY` | `1` for the first session, then `0` |

`compose.yml` pins the in-container paths (`/data/edge.sqlite`,
`/var/lib/console`, `/run/kamailio/kamailio_rpc.sock`) so the host locations
are changed through the volume mounts, not these variables.

Arcane note: `compose.yml` uses `env_file: .env`. If your Arcane version injects
the stack environment directly instead of materializing a `.env` file next to
the compose file, either create that file (mode `600`) or mirror the same keys
under the service's `environment:`. The container needs them at start, not only
for compose interpolation.

Note: `CONSOLE_THROTTLE_TENANTDOM` is reserved by plan §10 but is **not read**
by the current settings module; per-table throttles come from
`console/tables.yaml`. See `.env.example`.

---

## 5. Build the image once

Plain Docker, with the host `console` GID baked in for the non-root option:

```bash
cd /root/voip-edge
CONSOLE_GID="$(getent group console | cut -d: -f3)"
docker build \
  --build-arg CONSOLE_GID="${CONSOLE_GID}" \
  -t voip-edge-console:0.1.8 .
```

Arcane: build the same Dockerfile once via **Images → Build** and tag it
`voip-edge-console:0.1.8`. The compose service references that image; it does
not rebuild during a stack deploy.

The build pins `fastapi`, `uvicorn`, `jinja2`, `MarkupSafe` and `PyYAML` to the
versions verified in this repo. `pytest` and the test doubles are deliberately
not installed. `console/tables.yaml` and `web/templates` + `web/static` are
copied into the image; there is no runtime bind mount for code or descriptors.

Build context is the repo root. Until a `.dockerignore` is added, exclude local
state by building from a clean checkout (`git archive`) rather than a working
tree containing `.venv`, `.git` and test databases.

---

## 6. Start the service

If the console is part of the existing stack, merge this service into
`/root/voip-stack/kamailio-edge/docker/docker-compose.yml`:

```yaml
  console:
    # Merge is discouraged: `env_file: .env` and `build.context: .` are
    # relative to the compose file, so a copy into another stack silently
    # loads the WRONG env and build directory. Deploy this repo's compose.yml
    # as its own stack, or rewrite both paths to absolute values.
```

Otherwise deploy `/root/voip-edge/compose.yml` as its own Arcane stack
on the same host, with the same `EDGE_HOME` / `EDGE_RUN` values.

The important properties of that service:

| Property | Value | Why |
|---|---|---|
| `network_mode` | `host` | binds the tunnel IP directly; no NAT/port mapping |
| `ports` | none | the port must not be published |
| `restart` | `unless-stopped` | survives host/daemon restarts |
| `mem_limit` / `cpus` | `256m` / `0.5` | guard rails, not a sizing promise |
| `user` | `0:0` | stock root-owned DB/WAL and socket access |
| `group_add` | `${CONSOLE_GID}` | keeps the `console` GID available for non-root switching |
| volumes | DB rw, run rw, sidecar rw | in-place SQLite writes + datagram client + durable sidecar |

The run-dir mount is read/write because a unix datagram client needs **write**
permission on the socket path (mode `0660`, group `console`). The directory
mount (rather than a single-file mount) also lets the socket be recreated by a
Kamailio restart without recreating the console container.

---

## 7. Verify, then expose

```bash
docker ps --filter name=voip-edge-console
docker exec voip-edge-console curl -fsS http://100.64.0.9:8080/health
# {"status":"ok"}

docker logs --tail 50 voip-edge-console
# structured JSON lines; no traceback, no "database is locked"
```

Then open the console through Pangolin (section 7.1), sign in with
`CONSOLE_TOKEN`, and check the status strip: edge reachable (or the expected
unreachable banner), read-only state, pending reloads, sidecar state.

### 7.1 Pangolin SSO resource

Pangolin UI labels vary by version; this is the same resource pattern used in
`/root/voip-stack/kamailio-edge/DEPLOY.md` §C, applied to HTTP instead of SIP.

1. **Pangolin UI → Resources → New resource** (or the equivalent in your
   Pangolin version).
2. Name it for the console, e.g. `voip-console`.
3. Protocol: **HTTP**.
4. Target: `http://100.64.0.9:8080` (the console's host-networked bind).
   Do not point it at a Docker bridge address; there is no published port.
   The Pangolin proxy must be able to reach the host tunnel address; if it
   runs bridge-networked, add host-gateway routing rather than publishing
   `8080`.
5. Public hostname: the console hostname you will use, e.g.
   `console.example.net` (or an internal-only name if your Pangolin site is
   private).
6. Enable **authentication / SSO** on the resource and attach the same identity
   provider used for the other protected resources. Restrict it to the operator
   account(s).
7. Terminate TLS at Pangolin and set `CONSOLE_SECURE_COOKIE=1` in `.env`.
8. Do **not** create a public TCP port-forward for `8080`, and do not add a
   bypass rule for the UI. The console token is a second factor, not a reason
   to skip SSO.
9. If (and only if) your Pangolin health checking needs it, add a path rule
   that bypasses SSO for exactly `/health`. The Docker healthcheck runs inside
   the container and does not need this.

Smoke test from outside the tunnel:

- an unauthenticated request must land on the Pangolin/IdP login, not on the
  console login form;
- after SSO, the console login form appears and `CONSOLE_TOKEN` is required;
- `/health` reveals only `{"status":"ok"}`.

### 7.2 Healthcheck semantics

The Docker healthcheck is **console liveness only**:

- it curls `http://${CONSOLE_BIND}:${CONSOLE_PORT}/health`;
- `/health` is unauthenticated, does not open the DB, and never calls
  Kamailio;
- it returns 200 even when Kamailio is down or the RPC socket is missing.

That is intentional. A Kamailio outage must leave the container **healthy** so
the operator can still load the console, see the banner and queue writes. If
the healthcheck probed the edge, Docker would restart-loop the console exactly
when it is needed. Kamailio state belongs in the status strip and in
`core.uptime`, never in the container exit code.

If you bind `0.0.0.0` (not recommended for this deployment), the healthcheck
URL must be overridden to `127.0.0.1`: curling the wildcard address is not
portable.

---

## 8. Non-root alternative (optional)

The image is built with a non-root `console` user (UID/GID default 10001). To
run it that way, all of these must be true:

1. **Socket group, in both containers.** Host `console` GID, the console image
   build arg and the Kamailio container's `/etc/group` must agree:
   ```bash
   getent group console                      # e.g. 995
   docker build --build-arg CONSOLE_GID=995 \
     -t voip-edge-console:0.1.8 .
   ```
   `dgram_group "console"` is resolved by name inside `voip-edge`. The stock
   image may not contain that name, and `group_add` does not create one. Build
   a thin Kamailio image for the edge service:
   ```dockerfile
   FROM ghcr.io/kamailio/kamailio:6.1.5-trixie
   ARG CONSOLE_GID=995
   RUN groupadd --gid "${CONSOLE_GID}" console
   ```
   (or use a group name/GID that already exists in both images and set
   `dgram_group` to it). Confirm after restart:
   ```bash
   ls -l "${EDGE_RUN}/kamailio_rpc.sock"
   # srw-rw---- 1 root console ...
   ```
2. **Sidecar.** `chown -R 10001:995 "${EDGE_HOME}/console"`.
3. **DB and WAL/SHM.** Kamailio creates `edge.sqlite`, `edge.sqlite-wal` and
   `edge.sqlite-shm` as `root:root` with umask 022. Giving the console user
   write access requires either:
   - a common group plus Kamailio running with a group-writable umask (for
     example start it with `umask 0002`), so new WAL/SHM files are `0660`; or
   - filesystem ACLs with a default ACL on `${EDGE_HOME}/kamailio/db`, so
     files created by Kamailio are writable by the console UID regardless of
     umask (e.g. `setfacl -m u:10001:rwx -m d:u:10001:rwx .../db`).
4. **Runtime user.** Add `user: "10001:995"` to the compose service and keep
   `group_add: ["995"]`. Remove the default `user: "0:0"` line.
5. **Root-owned backups.** The backup files are created by the console; the
   directory must be writable by the console user.

If any of these is fragile on your host, keep the root default. Running this
single-purpose container as root on a host that already runs Kamailio as root
does not add a new trust boundary; the console still binds only the tunnel IP
and is behind Pangolin SSO.

---

## 9. Rollout and rollback

Plan §10 rollout order:

1. Lab first.
2. Redeploy `voip-edge` with `WITH_CONSOLE` (approved one-time blip), bring up
   the console with `CONSOLE_READONLY=1`.
3. Create the Pangolin SSO resource and verify reads + status strip.
4. Set `CONSOLE_READONLY=0` and onboard `beta` / `gamma` through the
   wizard as the acceptance of record.

Before enabling writes, take/keep a backup (the wizard takes one automatically
before its apply). For upgrades, stop the console, deploy the new pinned image,
and start with `CONSOLE_READONLY=1` until reads and the status strip are
verified.

Rollback is image-tag based: set `image:` back to the previous
`voip-edge-console:<version>` and redeploy. The console never changes the
Kamailio schema; its own sidecar migrations are forward-only (see
`docs/ops/releases.md`). Database recovery is a separate, stopped-stack
procedure: `docs/ops/backups.md`.

---

## 10. Troubleshooting

| Symptom | Check |
|---|---|
| container healthy but page says edge unreachable | `ls -l ${EDGE_RUN}/kamailio_rpc.sock`, group/mode, `CONSOLE_RPC_SOCK`, `docker logs voip-edge` for jsonrpcs errors |
| socket is `root:root 0660` and the console is non-root | `dgram_group` could not resolve inside the Kamailio container; add the `console` group there (section 8) or use the root console default |
| `PermissionError` / `database is locked` on save | DB/WAL ownership vs the runtime user; one writer only; keep the root default or align UID/GID + ACLs |
| container restarts repeatedly | it should not: `/health` is independent of Kamailio. Check the uvicorn traceback in logs, port bind conflict on `100.64.0.9:8080`, or a missing `.env` value |
| healthcheck fails but the UI works | `CONSOLE_BIND` is probably `0.0.0.0`; bind the tunnel IP or override the healthcheck URL |
| SSO login works, console rejects the token | `CONSOLE_TOKEN` mismatch/typo, or `.env` not reloaded (restart the container) |
| parked homes disappeared after a container rebuild | the `${EDGE_HOME}/console` volume is missing or was not preserved; restore the sidecar per `docs/ops/backups.md` |
| save says reload failed | expected when Kamailio is down: intent is persisted, banner remains; fix the edge/socket and wait for the sweeper or restart the console |
