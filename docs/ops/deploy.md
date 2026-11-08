# Deploying the edge stack

One `compose.yaml`: Kamailio, rtpengine, ACME, cert reloader, console, and an
optional Homer profile. All mutable files live under `${EDGE_HOME}`
(default `/opt/voip-edge`).

## 1. Prerequisites

- Docker Engine + Compose v2.
- Public IPv4 for SIP/RTP; a tunnel address for the console.
- Tenant DNS names, e.g. `sip.<client>.home.example.net`.
- SSO reverse proxy (Pangolin) in front of the console.
- Ports: `5060/udp`, `5060/tcp`, `30000-40000/udp`, and `5061/tcp` for TLS.

Homes reach the VPS over the overlay with native routing and carry their LAN
subnets. Dispatcher destinations are literal home LAN IPs; no `*.internal`
names or resolver override.

## 2. Prepare `${EDGE_HOME}`

```bash
export EDGE_HOME=/opt/voip-edge
export EDGE_RUN=/run/kamailio-edge
mkdir -p "$EDGE_HOME"/{kamailio,console,tls,acme/data} "$EDGE_RUN"

cp -r kamailio/* "$EDGE_HOME/kamailio/"
DOCKER=1 "$EDGE_HOME/kamailio/db/create-db.sh"
```

Edit `$EDGE_HOME/kamailio/kamailio.cfg`: `PUBIP`, `WGIP`, `WGSOCK`
(`udp:<WGIP>:5060`), `PUBDOM`. `WGIP` and `WGSOCK` change together.

The tenant suffix gate (`TENANTSUFFIXRE`) is generated from
`TENANT_ALLOWED_SUFFIXES` by the container entrypoint on every start. Do not
edit `tenant_suffix.cfg`; change the env list instead.

Edit `rtpengine/rtpengine.conf`:

```ini
interface = internal/<WGIP>;public/<PUBIP>
```

For TLS, put the TSIG key at `$EDGE_HOME/acme/technitium_tsig`, mode `600`.

Seed the DB before the first Kamailio start. An empty auto-created DB does not
recover dispatcher state on reload alone.

## 3. Environment

```bash
cp .env.example .env
```

Set at least:

```dotenv
EDGE_HOME=/opt/voip-edge
EDGE_RUN=/run/kamailio-edge
CONSOLE_BIND=100.64.0.9
CONSOLE_PORT=8080
CONSOLE_TOKEN=<random>
CONSOLE_SECRET=<random>
ACME_EMAIL=admin@example.net
ACME_DOMAINS=sip.alpha.home.example.net,*.home.example.net
ACME_CERT_NAME=sip.alpha.home.example.net
NSUPDATE_SERVER=192.0.2.53
```

Generate tokens with `openssl rand -hex 32`. Start with `CONSOLE_READONLY=1`.

## 4. Start

Build local images once:

```bash
docker build -f Dockerfile.console -t voip-edge-console:0.1.11 .
docker build -f Dockerfile.rtpengine -t voip-edge-rtpengine:latest .
```

Or set `CONSOLE_IMAGE` / `RTPENGINE_IMAGE` to published GHCR images.

```bash
docker compose up -d
docker compose ps
```

Kamailio starts with `WITH_CONSOLE`, so the console can use the RPC socket.
TLS is opt-in: add `-A WITH_TLS` to the Kamailio `entrypoint` and uncomment
both TLS mounts; see `docs/tls.md`.

## 5. Verify

```bash
docker exec voip-edge kamcmd core.uptime
docker exec voip-edge kamcmd dispatcher.list
docker exec voip-edge kamcmd mtree.list tenantdom
docker exec voip-edge kamcmd permissions.trustedDump
docker exec voip-edge kamcmd rtpengine.show all
docker exec voip-edge-console curl -fsS http://100.64.0.9:8080/health
```

Register a phone and test phone → home, home → phone, PSTN and emergency.
The console Live page shows edge and reload state.

## 6. Pangolin

1. SIP overlay: enable native WireGuard routing on the home Newt client. The
   dispatcher row points at the home LAN IP, e.g.
   `sip:10.0.0.10:5060;transport=tcp`. The site must route that subnet.
2. Console: create an HTTP resource for `http://100.64.0.9:8080` and enable
   SSO. Never publish the console port.
3. Add the home's observed tunnel source IP to the console Trusted table.

## 7. Homer (optional)

Kamailio and rtpengine already send HEP to `127.0.0.1:9060`; no restart is
needed when Homer starts or stops.

```bash
docker compose --profile homer up -d
docker compose --profile homer stop heplify-server homer-app homer-db
```

- UI: `http://127.0.0.1:9080` (default `admin` / `sipcapture`; change it).
  Expose only through Pangolin SSO.
- Retention: `HOMER_RETENTION_DAYS` (default 7).
- Data: named volume `homer-db-data`; not in console backups.
- Permanent stop: optionally `docker exec voip-edge kamcmd siptrace.status off`.

## 8. Non-root console (optional)

The default is root because the Kamailio DB, WAL/SHM files and RPC socket are
root-owned. To use `10001`:

1. Build with `--build-arg CONSOLE_GID=<host console gid>`.
2. Make that group resolvable inside the Kamailio container and set
   `dgram_group "console"`.
3. Grant the console user write access to `${EDGE_HOME}/kamailio/db`,
   `${EDGE_HOME}/console` and the RPC socket.
4. Set `user: "10001:<gid>"` on the console service.

Keep the root default if that is fragile.

## 9. Troubleshooting

| Symptom | Check |
|---|---|
| console healthy, page says edge unreachable | RPC socket mode/group; `docker logs voip-edge` |
| `database is locked` | DB/WAL ownership, one writer, root default |
| REGISTER 403 | tenant domain, suffix, mtree row |
| "Unknown Origin" | `trusted.src_ip` does not match the site tunnel IP |
| dispatcher sets empty after reload | seed DB before first start; restart once |
| one-way audio | rtpengine down, RTP ports, Pangolin route |
| TLS handshake fails | `WITH_TLS`, mounts, `tls.cfg`, ACME logs |

## 10. More

- `docs/operations.md` — day-2 operations.
- `docs/tls.md` — ACME and renewal.
- `docs/ops/backups.md` — backup and restore.
- `docs/ops/releases.md` — versioning and rollback.
