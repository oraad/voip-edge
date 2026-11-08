# VoIP Edge

Self-hosted, multi-tenant SIP edge and operator console: **Kamailio**,
**rtpengine**, optional **Homer** capture, and a FastAPI console. One
`compose.yaml`, one `.env`, one host.

## Stack

| Service | Role |
|---|---|
| `kamailio` | SIP edge: tenant detection, REGISTER relay, INVITE routing |
| `rtpengine` | Userspace media relay for NATed phones |
| `acme` + `cert-reloader` | TLS issue, renewal, hot reload |
| `console` | Routing tables, lifecycle, backups, live status |
| `homer-*` | Optional SIP/RTCP capture (`homer` profile) |

## Quick start

```bash
git clone https://github.com/oraad/voip-edge.git
cd voip-edge
cp .env.example .env
```

Set `EDGE_HOME`, `CONSOLE_BIND`, `CONSOLE_TOKEN`, `CONSOLE_SECRET`, and the
ACME values in `.env`.

Prepare runtime files once:

```bash
export EDGE_HOME=/opt/voip-edge
mkdir -p "$EDGE_HOME"/{kamailio,console,tls,acme/data} /run/kamailio-edge
cp -r kamailio/* "$EDGE_HOME/kamailio/"
DOCKER=1 "$EDGE_HOME/kamailio/db/create-db.sh"
```

Edit `$EDGE_HOME/kamailio/kamailio.cfg` (`PUBIP`, `WGIP`, `WGSOCK`,
`PUBDOM`) and `rtpengine/rtpengine.conf` (`interface`).

```bash
docker compose up -d
docker compose ps
```

Open the console through Pangolin SSO. Do not publish its port.

Full runbook: [docs/ops/deploy.md](docs/ops/deploy.md).

## Optional Homer

Kamailio and rtpengine already send HEP to `127.0.0.1:9060`. Homer is off by
default and isolated:

```bash
docker compose --profile homer up -d     # start
docker compose --profile homer stop heplify-server homer-app homer-db
```

UI: `http://127.0.0.1:9080` (change the default `admin` / `sipcapture`
credentials). Retention: `HOMER_RETENTION_DAYS`.

## Configuration

All settings live in `.env`; the annotated template is
[`.env.example`](.env.example). Settings are read at container start, so
recreate the service after a change.

OIDC: [docs/oidc.md](docs/oidc.md).

## Images

| Image | Tags |
|---|---|
| `ghcr.io/oraad/voip-edge-console` | `<version>`, `sha-<short>`, `latest` |
| `ghcr.io/oraad/voip-edge-rtpengine` | `<rtpe_version>`, `sha-<short>`, `latest` |

Published on a `v*` tag or manual dispatch. Pin a version tag or digest in
production; `latest` is for lab/staging.

Build locally:

```bash
docker build -f Dockerfile.console -t voip-edge-console:local .
docker build -f Dockerfile.rtpengine -t voip-edge-rtpengine:local .
```

## Development

Python 3.15 or newer.

```bash
python3.15 -m venv .venv
.venv/bin/pip install \
  'fastapi>=0.162' 'uvicorn>=0.54' 'jinja2>=3.1' 'httpx>=0.28' \
  'authlib>=1.6' 'cryptography>=43' 'PyYAML>=6' 'MarkupSafe>=3' pytest
.venv/bin/python -m pytest
```

Live Kamailio gate (binds SIP 5060, run alone):

```bash
./tests/live/run-live.sh
```

The console runs exactly one uvicorn worker. Reload serialization, queues and
session state are process-local.

## Docs

- [Deploy](docs/ops/deploy.md) · [Operations](docs/operations.md)
- [Backups](docs/ops/backups.md) · [Releases](docs/ops/releases.md)
- [TLS](docs/tls.md) · [Packages](docs/ops/packages.md)
- [OIDC](docs/oidc.md) · [Contributor notes](AGENTS.md)

Historical design notes: `docs/plan.md`, `docs/research-*.md`,
`docs/validation-matrix.md`, `docs/review-backlog.md`.

## Security

- Keep the console behind SSO. Never bind it to the public Internet.
- Set `CONSOLE_TOKEN` and `CONSOLE_SECRET` explicitly.
- The console mounts the routing DB and RPC socket. Treat the host as trusted.
- Start with `CONSOLE_READONLY=1`, verify, then enable writes.
- Homer captures SIP headers, numbers and auth challenges. Keep retention
  short and expose the UI only through SSO.
