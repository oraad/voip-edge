# VoIP Edge Console

Operator web console for the Kamailio edge proxy described in `docs/plan.md`.
It manages homes (tenants), landlines, route sets, number rules and trusted
sources, drives Kamailio reloads, and owns park/restore/destroy lifecycle plus
online backups.

## Status

| Task | Scope | State |
|---|---|---|
| T0 | Descriptor engine + pack/unpack golden vectors | done |
| T1 | RPC client + serialized reload worker + capability probes | done |
| T2 | App skeleton, auth/CSRF, health, host-lab harness | done |
| T3 | Read-only table browser and Homes landing | done |
| T4 | Create/edit + validation families (T4.1–T4.8) | **not started** |
| T5 | Row delete + reference guards | **not started** |
| T6 | Live dashboard (registered phones, route-set probes, health) | done |
| T7 | Onboarding wizard | **not started** |
| T8 | Lifecycle park/restore/destroy + backups | done |
| T9 | Hardening + observability | **not started** |
| T10 | Packaging + docs | **packaging not started** (docs half in progress) |

Test baseline: **198 passed** with `.venv/bin/python -m pytest -q`.
`tests/live/` and `tests/e2e/` are reserved placeholders; they are not part of
that count.

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
  tables_ui.py      generic read-only table browser
  live.py           live dashboard + polling fragment
  tables.yaml       five table descriptors (grammar v1)
web/
  templates/        Jinja2 shells and generic table templates
  static/           vendored htmx / PicoCSS landing spot
tests/
  unit/ api/        unit and API suites
  fixtures/         seed DB, edge DB and FakeRPC doubles
  live/ e2e/        reserved for the singleton lab/journey work
docs/
  plan.md                    spec v2
  validation-matrix.md       §5.2 rule → test → owner traceability
  operations.md              operator runbook
  wireframe.html             design artifact (do not edit during implementation)
  research-*.md              background research
AGENTS.md           contributor/agent conventions and T4 workflow
```

## Docs

- [Plan](docs/plan.md) — decisions, architecture, task breakdown, review ledger.
- [Validation matrix](docs/validation-matrix.md) — the T4 deliverable: every
  §5.2 rule with an id, severity and test/owner placeholder.
- [Operations](docs/operations.md) — env vars, backup/restore, lifecycle and
  reload troubleshooting, Pangolin exposure.
- [Wireframe](docs/wireframe.html) — visual/IA reference.
- [AGENTS.md](AGENTS.md) — module map, code conventions, T4.2–T4.8 workflow.
- Edge-stack deployment: `/root/voip-stack/kamailio-edge/DEPLOY.md`.

## Not yet

- **T4 families** — validation engine and form tests; `docs/validation-matrix.md`
  currently has `TODO` for nearly all form and validator tests.
- **T5** — row delete, consequence dialogs, reference guards.
- **T7** — onboarding wizard and its fault-injection / TLS-SAN paths.
- **T9** — CSRF/read-only hardening, observability, audit surfaces.
- **T10 packaging** — compose service, read-only and write rollouts, Pangolin
  resource steps. The docs half (this README, `AGENTS.md`,
  `docs/validation-matrix.md`, `docs/operations.md`) is in progress.
- **Live/e2e coverage** — `tests/live/` and `tests/e2e/` are empty until the
  singleton lab harness lands.

## Dev

```bash
.venv/bin/python -m pytest -q
```

Or activate once with `. .venv/bin/activate` and run `pytest -q`. No test touches
the live edge; fixtures use temporary databases and a FakeRPC datagram double.
