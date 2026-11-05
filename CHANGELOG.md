# Changelog

All notable changes to the VoIP Edge Console are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While the project is pre-1.0, MINOR bumps carry feature sets and PATCH bumps
carry packaging/docs fixes.

## [0.1.0] - 2026-11-05

### Added

- Descriptor-driven console (T0–T10 from docs/plan.md): pack/unpack engine, tables.yaml grammar,
  sqlite WAL layer, JSON-RPC unix-datagram client, coalescing reload worker with pending-intent
  persistence, auth/CSRF (header + hidden-field double submit), generic read/edit/delete UI,
  validation families (tenant/landline/dispatcher/dialplan/trusted), stale-edit 409, landline
  prefix interruption, onboarding wizard (4 steps, 5/4 rows, backup-first), tenant lifecycle
  park/restore/destroy with durable resume and bounded regcache purge, live dashboard,
  capabilities probe, read-only mode, audit/backups pages, Docker/compose packaging, ops docs.
- Live Kamailio integration gate (`tests/live/run-live.sh`): park → REGISTER 403 → restore → 200.

### Notes

- `WITH_CONSOLE` ifdef added to the edge repo's kamailio.cfg (jsonrpcs dgram socket).
- Static assets (htmx 2.0.4, PicoCSS 2.0.6) are not vendored yet — see web/static/README.md.
- Docker build/compose unverified in this environment (no Docker/network); runbook in docs/ops/deploy.md.

## [Unreleased]

## [0.1.8] - 2026-11-05

### Security (review findings fixed)

- OIDC flow cookies are domain-separated from sessions (`typ` claim): a flow
  cookie can no longer authenticate requests (critical bypass found in review).
- Session signing has no fixed public fallback: derived from `CONSOLE_TOKEN`
  when `CONSOLE_SECRET` is absent; empty tokens can no longer log in.
- CSRF double-submit is bound to the signed session, not only a sibling cookie.
- Cookies switched to `SameSite=Lax` (Strict broke the OIDC callback redirect).
- OIDC: per-transaction flow cookies (multi-tab safe), fail-closed allowlist,
  `email_verified` required for domain rules, `azp`/`sub`/`iat` validation,
  algorithm-aware `at_hash`, https-only issuer/endpoints, `/auth/login` and
  `/auth/callback` rate-limited, OIDC login/logout audit events, read-only mode
  no longer blocks sign-out, backslash/open-redirect hardening.
- Docker image now installs its OIDC runtime dependencies (httpx/authlib/
  cryptography) and disables credential-bearing access-log query strings.
- Privacy guard checks staged blobs (not the worktree), includes untracked
  files in `--all`, blocks encrypted keys and `.env.*`; `.env` is gitignored.

### Fixes (configuration review)

- kamailio: `WGIP` default aligned with `WGSOCK`; regcache gains `autoexpire`;
  keepalive pinger iterator arguments fixed (it never pinged); unknown-tenant
  paths `return` instead of `exit` so the 403 is delivered; exactly one
  `direction=` pair per call; `dpid 0` can no longer act as a profile; PSTN
  dial-out no longer gated by the fallback-only `pstn_on` flag; re-INVITEs of
  anchored calls reach rtpengine; TLS cipher list drops `aNULL`.
- lifecycle UI wired (Park/Restore/Destroy routes + homes actions) and the
  pending-reload/lifecycle sweeper started in the app lifespan.
- UI navigation/actions reach the real table routes, forms and wizard.
- Error taxonomy mapped to HTTP statuses (422/409/503/502) incl. `DBBusy`.
- Form values are whitespace-stripped before storage; wizard always validates
  the landline length; audit page reads newest-first; backup rotation orders by
  encoded timestamp; deploy docs no longer add the nonexistent
  `dgram_max_size`; acme fallback hook writes the filenames tls.cfg reads;
  watcher retries failed reloads.

### Known backlog

See `docs/review-backlog.md` for the review items still open (body limits,
threadpool offload beyond lifecycle, restore/delete race hardening, exact
domain matching, TLS promotion doc, non-root ACL recipe, and similar).



### Added

- **T0 — Descriptor engine + pack/unpack.** `console/tables.yaml` grammar v1,
  strict descriptor loader, packed mtree codec and flags (`1+1+90`), startup DB
  consistency check, golden-vector tests in both directions including the
  absent `block_dpid` segment.
- **T1 — RPC client + reload worker + capability probes.** Unix-datagram
  JSON-RPC client with typed faults, `htable.dump` bucket flattening, one
  serialized `ReloadWorker` with a per-table coalescing throttle, and advisory
  capability probes. The Kamailio-down path persists reload intent instead of
  crashing.
- **T2 — App skeleton + host-lab harness.** FastAPI app factory, token login,
  signed session cookie, double-submit CSRF, always-200 `/health`, PicoCSS/htmx
  layout with an inline fallback, and the loopback host-lab harness.
- **T3 — Read-only table browser + Homes landing.** All five tables browsed
  through descriptors with zero per-table route code; empty states and
  fragment/full-page assertions.
- **T4 — Create/edit + validation families (T4.1–T4.8).** Generic
  descriptor-driven forms, named validator registry, the §5.2 matrix across
  homes, landlines, route sets, number rules and trusted sources, warning-save
  paths, cross-refs, longest-prefix interruption page and stale-edit 409
  handling. `docs/validation-matrix.md` is the traceability deliverable.
- **T5 — Row delete + guards.** Named-object confirmation and blocked
  consequence pages, reference guards, and lifecycle-only tables refusing row
  delete. *Landing in parallel at the time of this entry.*
- **T6 — Live dashboard + detail fragments.** Registered-phone panel, route-set
  probe state, edge health in the status strip, and polling fragments with
  `HX-Stop-Polling` on a down edge.
- **T7 — Onboarding wizard.** Four-step onboarding (identity, network, review,
  verify), one-transaction apply of 4–5 rows, per-insert rollback, checklist
  verification, and the mandatory TLS-SAN notice when `CONSOLE_TLS_LIVE=1`.
- **T8 — Lifecycle park/restore/destroy + backups.** Durable, resumable state
  machines with fault-injection resume, reload-before-purge ordering, regcache
  purge gate, five collision classes on restore, typed confirmation for an
  active destroy, online SQLite backups with global keep-5 rotation, and
  `.parked.json` evidence.
- **T9 — Hardening + observability.** CSRF and login-rate tests, read-only mode
  across mutating routes, capability probe surface, structured redacted JSON
  logs, audit and backups pages, and the throttle burst test. *Landing in
  parallel at the time of this entry.*
- **T10 — Packaging + docs.** Single-process `Dockerfile` (non-root default,
  pinned runtime dependencies, `workers=1`, liveness healthcheck),
  `compose.yml` for the existing Arcane stack (host networking, rw DB/run/
  sidecar mounts, group story, limits, restart policy), `.env.example` for the
  plan §10 surface, vendored-asset instructions, deployment/release/backup
  runbooks, and updated README/AGENTS status.

### Tests

- **676 passed** with `.venv/bin/python -m pytest -q` (current working tree;
  the 619+ baseline was verified before the parallel T5/T9 lanes added their
  test suites). `tests/live/` and `tests/e2e/` remain reserved for the
  serialized lab journeys and are not part of that count.

### Notes

- T5 and T9 are parallel lanes in the same working tree; the status table in
  `README.md` marks them as landing. Their counts may change as those lanes
  merge.
- The Docker image was authored and statically reviewed in an offline
  workspace. `docker build` was **not** executed here (no network for pip/apt);
  see `README.md` and the T10 hand-off notes.
