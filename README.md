# VoIP Edge Console

Operator web console for the Kamailio edge proxy described in `docs/plan.md`.
It manages homes (tenants), landlines, route sets, number rules and trusted
sources, drives Kamailio reloads, and owns park/restore/destroy lifecycle plus
online backups.

## Status

Current working tree; T5 and T9 are parallel lanes that are still landing, so
their state is marked accordingly.

| Task | Scope | State |
|---|---|---|
| T0 | Descriptor engine + pack/unpack golden vectors | done |
| T1 | RPC client + serialized reload worker + capability probes | done |
| T2 | App skeleton, auth/CSRF, health, host-lab harness | done |
| T3 | Read-only table browser and Homes landing | done |
| T4 | Create/edit + validation families (T4.1–T4.8) | done |
| T5 | Row delete + reference guards | **landing** (uncommitted; code/tests present) |
| T6 | Live dashboard (registered phones, route-set probes, health) | done |
| T7 | Onboarding wizard | done |
| T8 | Lifecycle park/restore/destroy + backups | done |
| T9 | Hardening + observability | **landing** (uncommitted; wired, tests green) |
| T10 | Packaging + docs | done in this change; **image build unverified offline** |

Test baseline: **676 passed** with `.venv/bin/python -m pytest -q` (includes
the T5/T9 tests; the 619+ baseline was verified before those parallel lanes
added their suites). `tests/live/` and `tests/e2e/` are reserved placeholders
and are not part of that count.

The Docker image and compose service are authored but were **not built** in
this offline workspace (no network for pip/apt). Verify the image once before
first deployment; see `docs/ops/deploy.md`.

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
  capabilities.py   startup RPC capability probe (T9, landing)
  observability.py  structured redacted logs (T9, landing)
  system_ui.py      audit/backups + status-strip routes (T9, landing)
  tables.yaml       five table descriptors (grammar v1)
web/
  templates/        Jinja2 shells and generic table templates
  static/           vendored htmx / PicoCSS (see web/static/README.md)
tests/
  unit/ api/        unit and API suites
  fixtures/         seed DB, edge DB and FakeRPC doubles
  live/ e2e/        reserved for the singleton lab/journey work
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
  -t voip-edge-console:0.1.0 .

cp .env.example .env        # set token, secret, bind, read-only=1 first
# Merge compose.yml's console service into the voip-edge stack, or deploy it
# as its own Arcane stack with the same EDGE_HOME/EDGE_RUN.
```

The console binds `100.64.0.9:8080` on host networking and is exposed only
through a Pangolin resource with SSO; the token remains a second factor.
`/health` is console liveness only and stays 200 when Kamailio is down.

## Docs

- [Plan](docs/plan.md) — decisions, architecture, task breakdown, review ledger.
- [Validation matrix](docs/validation-matrix.md) — every §5.2 rule with an id,
  severity and test/owner traceability.
- [Operations](docs/operations.md) — env vars, backup/restore, lifecycle and
  reload troubleshooting, Pangolin exposure.
- [Deployment](docs/ops/deploy.md) — image build, compose service, mounts,
  group/permission story, SSO resource, rollout.
- [Backups and restore](docs/ops/backups.md) — online backups, keep-5 rotation,
  never hot-swap, stopped-stack recovery.
- [Releases](docs/ops/releases.md) — image pinned to code, baked
  `tables.yaml`, bump and rollback.
- [Changelog](CHANGELOG.md) — T0–T10 summary and test count.
- [Wireframe](docs/wireframe.html) — visual/IA reference.
- [AGENTS.md](AGENTS.md) — module map, code conventions, deployment
  conventions, T4.2–T4.8 workflow.
- Edge-stack deployment: `/root/voip-stack/kamailio-edge/DEPLOY.md`.

## Not yet

- **T5 and T9** are landing (uncommitted) in the shared working tree; their
  tests currently pass, but the lanes may still move before integration.
- **Vendored assets** — `web/static/` still has no `pico.min.css` or
  `htmx.min.js`; see `web/static/README.md` for the pinned versions, SHA-256
  record and offline copy procedure.
- **Image build** — the Dockerfile/compose pair has not been executed here;
  the first build must happen on a host with pip/apt access.
- **Live/e2e coverage** — `tests/live/` and `tests/e2e/` are empty until the
  singleton lab harness lands.

## Dev

```bash
.venv/bin/python -m pytest -q          # 676 passed in the current tree
.venv/bin/python -m compileall console # packaging smoke, no network
```

No test touches the live edge; fixtures use temporary databases and a FakeRPC
datagram double. There is no install step and no network access in this
workspace.
