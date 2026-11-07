# VoIP Edge

Operator console and container images for a self-hosted, multi-tenant SIP edge
built on **Kamailio** and **rtpengine**.

[![publish console](https://github.com/oraad/voip-edge/actions/workflows/publish-ghcr.yml/badge.svg)](https://github.com/oraad/voip-edge/actions/workflows/publish-ghcr.yml)
[![publish rtpengine](https://github.com/oraad/voip-edge/actions/workflows/publish-rtpengine.yml/badge.svg)](https://github.com/oraad/voip-edge/actions/workflows/publish-rtpengine.yml)
![Python 3.15](https://img.shields.io/badge/python-3.15-blue)

## Overview

VoIP Edge is the operator-facing control plane for a home/small-office VoIP
edge. It manages **homes (tenants)**, landlines, ring groups, dispatcher sets,
number rules and trusted tunnel sources, writes the Kamailio routing tables,
and triggers reloads over Kamailio's JSON-RPC socket. It also owns the
park/restore/destroy lifecycle and online SQLite backups.

The edge itself runs two containers:

- **Kamailio** — SIP proxy for tenant detection, REGISTER relay and INVITE
  routing;
- **rtpengine** — userspace media relay for NATed phones.

The console is a single-process FastAPI app. It uses host networking, talks to
Kamailio over a unix datagram socket, and is meant to sit behind a reverse proxy
with SSO (for example Pangolin/Traefik). It is **not** a public endpoint.

## Features

- Multi-tenant data model: domains, landlines, ring groups, dispatcher sets,
  dialplan partitions and trusted tunnel sources.
- Descriptor-driven create/edit/delete with validation, reference guards and
  packed mtree encoding.
- Live dashboard: registered phones, route-set probes and edge health.
- Guided onboarding wizard for a new home.
- Park, restore and destroy lifecycle with online SQLite backups (keep-5).
- Single uvicorn worker; process-local locks, queues and session state stay
  consistent.
- OIDC SSO with break-glass token login; CSRF protection, secure cookies and
  login rate limiting.
- Published console and rtpengine images, built by GitHub Actions.

## Architecture

```text
 phones / homes ──SIP/TLS──▶ Kamailio edge ──SIP──▶ tenant voip-stack / PSTN
                                   │
                              rtpengine
                                   │
 operator ──HTTPS/SSO──▶ console ──┴── JSON-RPC (unix socket) + SQLite
```

| Component | Role |
|---|---|
| `console/` | FastAPI operator console; edits the edge DB and calls Kamailio RPC |
| `kamailio/` | Edge SIP configuration and routing fragments |
| `rtpengine/` | Userspace media-relay configuration |
| `deploy/` | ACME and certificate deployment helpers |
| `compose.yml` | Host-networked console service |

## Quick start

### Prerequisites

- Docker Engine with Compose v2.
- A running Kamailio edge stack with `jsonrpcs` enabled and its database at
  `${EDGE_HOME}/kamailio/db/edge.sqlite`.
- A private or tunnel address for the console (for example `100.64.0.9`).
- A reverse proxy with SSO in front of the console.

The full edge and console runbook is in
[docs/ops/deploy.md](docs/ops/deploy.md).

### 1. Pull the image

```bash
docker pull ghcr.io/oraad/voip-edge-console:0.1.11
```

The images are public; no registry login is required. For a fork, replace
`oraad` with your GitHub owner.

### 2. Configure

```bash
git clone https://github.com/oraad/voip-edge.git
cd voip-edge
cp .env.example .env
```

Set at least:

```dotenv
EDGE_HOME=/opt/voip-edge
EDGE_RUN=/run/kamailio-edge
CONSOLE_BIND=100.64.0.9
CONSOLE_PORT=8080
CONSOLE_TOKEN=<random token>
CONSOLE_SECRET=<random secret>
CONSOLE_IMAGE=ghcr.io/oraad/voip-edge-console:0.1.11
```

Generate the credentials with `openssl rand -hex 32`. If either value is left
blank the process generates a random one, which makes login impossible until
you set it.

### 3. Start

```bash
docker compose pull
docker compose up -d
```

### 4. Open the console

From the host or the overlay:

```bash
curl http://<CONSOLE_BIND>:<CONSOLE_PORT>/health
```

Then open the console through the SSO-protected resource and sign in with the
token (or OIDC). The onboarding wizard creates the first home.

> Host networking is deliberate. Do not publish `${CONSOLE_BIND}:${CONSOLE_PORT}`
> directly to the Internet; expose it through the reverse proxy with SSO.

### Build locally instead

```bash
docker build -f Dockerfile.console -t voip-edge-console:0.1.11 .
docker compose up -d
```

## Configuration

Settings are read once at import; restart the container after changing them.
The full surface, with comments, is in [`.env.example`](.env.example).

| Variable | Default | Purpose |
|---|---|---|
| `EDGE_HOME` | `/opt/voip-edge` | Host path holding `kamailio/db` and `console/` |
| `EDGE_RUN` | `/run/kamailio-edge` | Host run directory with `kamailio_rpc.sock` |
| `CONSOLE_BIND` | `127.0.0.1` | Bind address; use the tunnel address in production |
| `CONSOLE_PORT` | `8080` | HTTP port |
| `CONSOLE_TOKEN` | random | Break-glass login token |
| `CONSOLE_SECRET` | random | Session-cookie signing secret |
| `CONSOLE_AUTH_MODE` | `both` | `both`, `oidc` or `token` |
| `CONSOLE_SECURE_COOKIE` | `0` | Set to `1` behind HTTPS |
| `CONSOLE_SESSION_TTL` | `43200` | Absolute session lifetime (seconds) |
| `CONSOLE_LOGIN_RATE` | `5` | Failed logins per minute per source |
| `CONSOLE_READONLY` | `0` | Disable all writes |
| `CONSOLE_ALLOWED_SUFFIXES` | `home.example.net` | Comma-separated allowed tenant domain suffixes |
| `CONSOLE_RPC_SOCK` | `/run/kamailio/kamailio_rpc.sock` | Kamailio JSON-RPC socket |
| `CONSOLE_RPC_TIMEOUT_MS` | `2000` | Per-RPC timeout |
| `CONSOLE_ENFORCE_CAPABILITIES` | `0` | Refuse lifecycle actions when the RPC capability probe fails |

OIDC variables (`OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`,
`OIDC_REDIRECT_URI`, `OIDC_ALLOWED_GROUPS`, `OIDC_ALLOWED_EMAIL_DOMAINS`, …) are
documented in [docs/oidc.md](docs/oidc.md).

## Container images

| Image | Registry | Tags |
|---|---|---|
| Console | `ghcr.io/oraad/voip-edge-console` | `<version>`, `sha-<short>`, `latest` |
| rtpengine | `ghcr.io/oraad/voip-edge-rtpengine` | `<rtpe_version>`, `sha-<short>`, `latest` |

- Both images are published on a `v*` tag or by manual workflow dispatch.
- The console version tracks this repository (`0.1.11`, `0.1.12`, …).
  rtpengine tracks the **upstream source release** (`mr26.3.1.2` by default),
  not the console version.
- `latest` follows the newest published build and is intended for lab/staging.
  Pin a version tag, or a digest, in production.
- See [docs/ops/packages.md](docs/ops/packages.md) for publishing, private
  packages and digest pinning.

Build the images locally:

```bash
docker build -f Dockerfile.console -t voip-edge-console:local .
docker build -f Dockerfile.rtpengine -t voip-edge-rtpengine:local .
```

rtpengine is built from upstream source; the first build takes several minutes.
Select the upstream release with `--build-arg RTPE_VERSION=mr26.3.1.2`.

## Repository layout

```text
console/              FastAPI application
web/                  Jinja2 templates and vendored static assets
kamailio/             Edge SIP configuration and routes
rtpengine/            Media-relay configuration
deploy/               ACME / certificate helpers
tests/                Unit, API and live-Kamailio tests
docs/                 Operator runbooks and design notes
Dockerfile.console    Console image (Python 3.15, one worker)
Dockerfile.rtpengine  rtpengine image (upstream source build)
compose.yml           Console service (host networking)
```

## Development

Python 3.15 or newer is required.

```bash
python3.15 -m venv .venv
.venv/bin/pip install \
  'fastapi>=0.162' 'uvicorn>=0.54' 'jinja2>=3.1' 'httpx>=0.28' \
  'authlib>=1.6' 'cryptography>=43' 'PyYAML>=6' 'MarkupSafe>=3' pytest
.venv/bin/python -m pytest
```

The live Kamailio lifecycle gate binds SIP port 5060 and must run on its own:

```bash
./tests/live/run-live.sh
```

The console image runs exactly **one** uvicorn worker. Reload serialization,
the coalescing queue, the write lock and session state are process-local; do
not add `--workers` or a multi-process manager.

## Documentation

- [Deployment](docs/ops/deploy.md) — edge prerequisites, compose service,
  mounts, permissions, SSO resource and rollout.
- [Operations](docs/operations.md) — environment, lifecycle, reload
  troubleshooting and exposure.
- [Backups and restore](docs/ops/backups.md) — online backups and stopped-stack
  recovery.
- [Releases](docs/ops/releases.md) — image versioning, bump and rollback.
- [GitHub Packages](docs/ops/packages.md) — publishing and consuming the images.
- [OIDC](docs/oidc.md) — SSO design and configuration.
- [Plan](docs/plan.md) — architecture, decisions and task history.
- [AGENTS.md](AGENTS.md) — module map and contributor conventions.

## Security

- Never expose the console directly to the Internet. Bind it to a private or
  tunnel address and put an authenticating reverse proxy in front of it.
- Set `CONSOLE_TOKEN` and `CONSOLE_SECRET` explicitly; the fail-closed random
  defaults are not usable for a real deployment.
- The console mounts the Kamailio database read/write and needs write access to
  the JSON-RPC socket. Treat the host and its tunnel as trusted.
- Use `CONSOLE_READONLY=1` for a first rollout, then enable writes.

## Contributing

Bug reports and pull requests are welcome. Run the test suite before opening a
PR and follow the conventions in [AGENTS.md](AGENTS.md).
