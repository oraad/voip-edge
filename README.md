# VoIP Edge Console

Operator web console for the Kamailio edge proxy described in `docs/plan.md`.
It manages homes (tenants), landlines, route sets, number rules and trusted
sources, drives Kamailio reloads, and owns park/restore/destroy lifecycle plus
online backups.

## Status

Implementation tasks T0–T10 are complete and have passed multiple independent
review rounds. A few explicit documentation/coverage follow-ups remain: the
validation matrix still has `TODO` rows, the Homes coherence column is a
placeholder, and the image build has not been executed here. The current
release is **0.1.10**.

| Task | Scope | State |
|---|---|---|
| T0 | Descriptor engine + pack/unpack golden vectors | done |
| T1 | RPC client + serialized reload worker + capability probes | done |
| T2 | App skeleton, auth/CSRF, health, host-lab harness | done |
| T3 | Read-only table browser and Homes landing | done |
| T4 | Create/edit + validation families (T4.1–T4.8) | done; matrix TODOs tracked |
| T5 | Row delete + reference guards | done |
| T6 | Live dashboard (registered phones, route-set probes, health) | done; Homes coherence placeholder |
| T7 | Onboarding wizard | done |
| T8 | Lifecycle park/restore/destroy + backups | done |
| T9 | Hardening + observability | done |
| T10 | Packaging + docs | done; image build not executed in this workspace |

Test baseline: **777 passed, 1 deselected** with
`.venv/bin/python -m pytest`. The live Kamailio park/restore gate is
`tests/live/run-live.sh` and is run separately (it binds SIP port 5060).

The Docker image and compose service are authored and statically verified, but
`docker build` was **not executed** in this workspace. Verify the image once
before first deployment; see `docs/ops/deploy.md`.

## Layout

```
console/            Python package
  app.py            FastAPI factory, auth/CSRF, health
  settings.py       env-var config surface (plan §10)
  errors.py         frozen error taxonomy
  descriptors.py    tables.yaml loader + DB consistency check
  pack.py           packed mtree codec and flags
  db.py             WAL connections, generic reads, write transaction lock
  rpc.py            unix-datagram JSON-RPC client
  reloads.py        one background ReloadWorker + coalescing throttle
  lifecycle.py      park / restore / destroy state machines
  backups.py        online SQLite backup, keep-5 rotation
  sidecar.py        console.sqlite: audit, parked, intents, pending reloads
  security.py       session cookie, CSRF, login rate limit
  homes_ui.py       Homes landing
  forms_ui.py       descriptor-driven create/edit/delete routes
  tables_ui.py      generic read-only table browser
  live.py           live dashboard + polling fragment
  wizard_ui.py      onboarding wizard
  capabilities.py   startup RPC capability probe
  observability.py  structured redacted logs
  system_ui.py      audit/backups + status-strip routes
  derivations.py    named `derived:` field rules
  tables.yaml       five table descriptors (grammar v1)
web/
  templates/        Jinja2 shells and generic table templates
  static/           vendored htmx / PicoCSS (see web/static/README.md)
tests/
  unit/ api/        unit and API suites
  fixtures/         seed DB, edge DB and FakeRPC doubles
  live/             real-Kamailio lifecycle gate; e2e/ reserved
docs/
  plan.md                    spec v2
  validation-matrix.md       §5.2 rule → test → owner traceability
  operations.md              operator runbook
  ops/deploy.md              console deployment in the Arcane stack
  ops/backups.md             backup + stopped-stack restore runbook
  ops/releases.md            image versioning, bump and rollback
  wireframe.html             design artifact (do not edit during implementation)
  research-*.md              background research
Dockerfile           single-process console image (workers=1)
compose.yml          host-networked service for the existing edge stack
.env.example         plan §10 environment surface with safe defaults
CHANGELOG.md         Keep a Changelog history
AGENTS.md            contributor/agent conventions
```

## Deploy (short form)

The full runbook, including the Kamailio `WITH_CONSOLE` block, group/GID story,
Pangolin SSO resource and healthcheck semantics, is `docs/ops/deploy.md`.

```bash
# Build once, pinned to the code version (no test deps in the image).
CONSOLE_GID="$(getent group console | cut -d: -f3)"
docker build --build-arg CONSOLE_GID="${CONSOLE_GID}" \
  -t voip-edge-console:0.1.10 .

cp .env.example .env        # set token, secret, bind, read-only=1 first
# Deploy compose.yml as its own Arcane stack with the same EDGE_HOME/EDGE_RUN.
```

The console binds `100.64.0.9:8080` on host networking and is exposed only
through a Pangolin resource with SSO. The token is a second factor only when
console OIDC is not enabled; with OIDC on it is break-glass only in `both` mode.
`/health` is console liveness only and stays 200 when Kamailio is down.

## Docs

- [Plan](docs/plan.md) — decisions, architecture, task breakdown, review ledger.
- [Validation matrix](docs/validation-matrix.md) — rule ids, severity and
  current test/owner traceability; explicit `TODO` gaps remain for some rules.
- [Operations](docs/operations.md) — env vars, backup/restore, lifecycle and
  reload troubleshooting, Pangolin exposure.
- [Deployment](docs/ops/deploy.md) — image build, compose service, mounts,
  group/permission story, SSO resource, rollout.
- [Backups and restore](docs/ops/backups.md) — online backups, keep-5 rotation,
  never hot-swap, stopped-stack recovery.
- [Releases](docs/ops/releases.md) — image pinned to code, baked
  `tables.yaml`, bump and rollback.
- [Changelog](CHANGELOG.md) — release history and review-round fixes.
- [Wireframe](docs/wireframe.html) — visual/IA reference.
- [AGENTS.md](AGENTS.md) — module map, code conventions, deployment
  conventions, T4.2–T4.8 workflow.
- Edge-stack deployment: `/root/voip-stack/kamailio-edge/DEPLOY.md`.

## Not yet

- **Image build** — the Dockerfile/compose pair has not been executed here;
  the first build must happen on a host with pip/apt access.
- **Live/e2e coverage** — `tests/live/` contains the real Kamailio lifecycle
  journey; broader end-to-end browser coverage is still a follow-up.

## Dev

```bash
.venv/bin/python -m pytest             # 777 passed, 1 deselected
.venv/bin/python -m compileall console # packaging smoke, no network
./tests/live/run-live.sh               # real Kamailio; run alone
```

No test touches the live edge; fixtures use temporary databases and a FakeRPC
datagram double. There is no install step and no network access in this
workspace.
