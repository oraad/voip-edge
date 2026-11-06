# AGENTS.md — VoIP Edge Console

## What this repo is

An operator web console for the Kamailio edge proxy. It edits the five routing
tables in the edge SQLite database (homes, landlines, route sets, number rules,
trusted sources), drives Kamailio reloads over the `jsonrpcs` unix datagram,
and owns tenant lifecycle (park/restore/destroy) plus online backups.

- Spec: `docs/plan.md` (v2) · validation matrix: `docs/validation-matrix.md`
- Operator runbook: `docs/operations.md` · design artifact: `docs/wireframe.html`
- Edge-stack deployment (Kamailio, Pangolin, TLS): `/root/voip-stack/kamailio-edge/DEPLOY.md`

Do not edit `docs/wireframe.html` as part of implementation work; it is the
design reference, not a template source.

## Architecture in five lines

1. A single-process FastAPI app serves full Jinja2 pages and htmx fragments;
   `workers=1` is a hard constraint because reloads and sidecar state are
   process-local.
2. Auth is a signed session cookie plus double-submit CSRF; `/health` is public
   and always answers 200, even when Kamailio is unreachable.
3. Every table-shaped behaviour comes from `console/tables.yaml`; routes are
   generic projections over descriptors, never per-table code.
4. `edge.sqlite` is read/written in place with WAL and `busy_timeout`; the file
   is never replaced or deleted as part of normal operation.
5. All reloads go through one background `ReloadWorker`; the console's own
   `console.sqlite` sidecar stores audit, parked snapshots, lifecycle intents
   and pending reloads, and backups are taken through the SQLite backup API.

## Module map

| Path | Responsibility |
|---|---|
| `console/app.py` | FastAPI factory, health/login/logout, auth+CSRF middleware, router wiring |
| `console/settings.py` | Single env-var config surface (plan §10) |
| `console/errors.py` | Frozen error taxonomy: `ValidationError`, `ConflictError`, `KamailioUnavailable`, `SidecarUnavailable`, `DBBusy`, `ReloadError` |
| `console/descriptors.py` | `tables.yaml` loader, descriptor types, row pack/unpack, startup DB consistency check |
| `console/pack.py` | Packed mtree codec and flags (`1+1+90`) |
| `console/db.py` | Connection-per-request, WAL/busy-timeout, generic reads, `write_transaction` lock |
| `console/rpc.py` | Unix-datagram JSON-RPC client, typed faults, `htable.dump` flattening |
| `console/reloads.py` | Serialized `ReloadWorker`, per-table coalescing throttle |
| `console/lifecycle.py` | Park/restore/destroy state machines, regcache purge, resume |
| `console/backups.py` | Online SQLite backup, keep-5 global rotation, `.parked.json` dump |
| `console/sidecar.py` | `console.sqlite` schema/migrations, audit, snapshots, intents, pending reloads |
| `console/security.py` | Session cookie, CSRF, login rate limit |
| `console/homes_ui.py`, `console/tables_ui.py`, `console/live.py` | Homes landing, generic read browser, live dashboard routes |
| `console/tables.yaml` | The five table descriptors (grammar v1) |
| `console/derivations.py` | Named `derived:` field rules (e.g. landline `ds_onnet`) |
| `web/templates/`, `web/static/` | Jinja2 templates; vendored htmx/PicoCSS and `app.js` |
| `tests/` | `unit/`, `api/`, `fixtures/`; `live/` is the real-Kamailio gate; `e2e/` is a reserved placeholder |

## Run the tests

Use the repo virtualenv; there is no network or install step in this workspace.

```bash
.venv/bin/python -m pytest -q
```

Current baseline: **779 passed, 1 deselected**. Focused runs are fine while
developing:

```bash
.venv/bin/python -m pytest tests/unit/test_pack.py -q
```

Unit/API tests never touch the live edge. Use `tests/fixtures/seeddb.py`,
`tests/fixtures/edgedb.py` and `tests/fixtures/fakerpc.py`; per-test temporary
databases and injected clocks only. `tests/live/run-live.sh` is the serialized
singleton gate that boots the sanitized Kamailio config and binds SIP 5060.

## Coding conventions

- **Descriptor-driven.** All table shape, field order, help text, severity,
  widgets and cross-refs live in `console/tables.yaml`. Validators are named
  Python callables, never inline YAML regex. If a table needs special logic,
  add a named validator or a generic descriptor feature — not a new route.
- **No table logic in `web/`.** `web/` holds templates/static only. Routes live
  in `console/*_ui.py` or `console/live.py` and must stay table-agnostic.
- **Error taxonomy.** Raise the classes in `console/errors.py`; map them once
  through FastAPI handlers. Do not invent parallel exception names. UI routes
  return HTML only; JSON routes never return HTML.
- **SQLite/WAL.** Keep `journal_mode=WAL`, `busy_timeout=2000`, short
  `BEGIN IMMEDIATE` transactions, one writer. Never replace, delete or
  re-create `edge.sqlite`; backups use `sqlite3.Connection.backup`, never a
  file copy of a live database.
- **Reloads.** Endpoints only `enqueue()`; only `ReloadWorker` calls
  `*_reload`. The worker owns serialization and the per-table coalescing
  window. Never call a reload RPC from a request handler.
- **Tests.** Prefer the public seam (route/validator function), temporary DBs,
  and explicit expected errors. A warning rule must have a test proving the
  save succeeds with the badge; an error rule must prove no write occurred.

## T4.2–T4.8 family workflow

T4.1 establishes the pattern; each later family follows it without touching
templates or another lane's files.

1. Pick one row family from `docs/validation-matrix.md` (T4.2 key/suffix and
   uniqueness, T4.3 cross-refs, T4.4 landline prefixes, T4.5 dispatcher,
   T4.6 trusted sources, T4.7 dialplan, T4.8 stale edit).
2. The descriptor already names the validator, severity and help text in
   `console/tables.yaml`. Implement that named validator; do not add form
   markup. If the rule needs a new descriptor capability, get the phase-0
   owner to extend the grammar first.
3. Add the unit test in `tests/unit/test_validators_<table>.py` and the form
   test in `tests/api/test_forms_<table>.py`. Cover error, warning-save,
   cross-ref, and rollback paths that apply to the family.
4. Update the matrix row with the real test IDs and tick the family in the
   plan/issue tracker. A matrix row without both test IDs is not done.
5. Run the focused tests, then the full `pytest -q` baseline. No live edge,
   no network, no template edits.

## Deployment & packaging conventions (T10)

These are additive to the conventions above; they do not relax the
descriptor/SQLite/reload rules.

- **One process.** `uvicorn --workers 1` is a hard requirement. Reload
  serialization, the coalescing queue, the write lock and session state are
  process-local. Never add a second worker or a multi-process manager to the
  image or compose service.
- **The image is the release.** `console/`, `web/` and `console/tables.yaml`
  are copied into the image; `tables.yaml` is **baked**, never a runtime mount.
  Changing code, templates, static assets, descriptors or dependencies means a
  rebuild and a new version tag (`docs/ops/releases.md`), not a restart.
- **Host networking.** `compose.yml` uses `network_mode: host` and no `ports:`;
  the console binds `${CONSOLE_BIND}:${CONSOLE_PORT}` (`100.64.0.9:8080` in
  production) and is exposed only through Pangolin SSO. The token is a second
  factor only when console OIDC is not enabled; otherwise it is break-glass
  (and only in `both` mode).
- **Mounts.** DB `rw` (in-place SQLite + WAL/SHM), `${EDGE_RUN}` `rw` (unix
  datagram client needs write on the socket path), `${EDGE_HOME}/console` `rw`
  (sidecar + backups). Never point `CONSOLE_DB_PATH` at a copy, and never
  hot-swap the DB (`docs/ops/backups.md`).
- **Healthcheck is console liveness only.** `/health` is public and always 200,
  even when Kamailio is unreachable; its state belongs in the status strip, not
  the container exit code. Do not add an RPC probe to the healthcheck or Docker
  will restart-loop the console during an edge outage.
- **Group story.** Kamailio creates the jsonrpcs datagram socket as
  `0660 root:console` (`dgram_group "console"`). `dgram_group` is resolved by
  name **inside the Kamailio container**, so the name must exist in that
  container's `/etc/group`; Docker `group_add` does not create it. The stock
  stack runs Kamailio and its DB/WAL as root, so `compose.yml` runs the console
  as `0:0` by default (a `0660 root:root` socket is still usable). The non-root
  recipe needs a matching GID in both containers plus DB ACL/umask work; see
  `docs/ops/deploy.md`.
- **Env surface is single-source.** `.env.example` mirrors plan §10. Note that
  `CONSOLE_THROTTLE_TENANTDOM` is reserved but not read yet; throttles come from
  `console/tables.yaml`. Restart after changing env values (read at import).
- **Testing a packaging change.** Run `.venv/bin/python -m pytest` (baseline
  **779 passed, 1 deselected**), `.venv/bin/python -m compileall console`
  (offline smoke), and `./tools/check-privacy.sh --all`. Do not put `pytest` or
  test doubles in the image, and do not claim `docker build` succeeded unless
  it actually ran on a network-enabled host.

## TLS

Edge TLS is automated: `deploy/acme/run.sh` (issue/renew, DNS-01) →
`deploy/acme/deploy-certs.sh` (atomic live install + marker) →
`deploy/watch-certs.sh` (`kamcmd tls.reload`). Secrets (TSIG, API tokens) live
only in env/docker-secrets, never in git. See `docs/tls.md`.

## Privacy

Never commit tenant data, live DBs, certs or DNS-01 secrets. Run `tools/check-privacy.sh --all` before publishing; the pre-commit hook enforces it (enable with `git config core.hooksPath .githooks`). Placeholder conventions are in docs/privacy.md.
