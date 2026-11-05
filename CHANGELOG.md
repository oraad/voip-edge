# Changelog

All notable changes to the VoIP Edge Console are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While the project is pre-1.0, MINOR bumps carry feature sets and PATCH bumps
carry fixes.

## [0.1.10] - 2026-11-05

Third review round: closes the regressions found while verifying 0.1.9 and
finishes the operations/docs alignment.

### Security

- No default or derivable credentials: unset `CONSOLE_TOKEN`/`CONSOLE_SECRET`
  become per-process random values (never `dev-token` and never derived from
  each other); an empty token can never match at login. `.env.example` ships
  blank values; an SSO session cannot survive a switch to token-only mode.
- OIDC callback/logout validate the `state` cookie-name suffix and tolerate
  malformed flow signatures; `/auth/login` no longer counts starts in the
  failure bucket (a tab burst cannot lock out /login or the callback).
- Request bodies are counted as they are streamed (1 MiB cap) for every unsafe
  method, including chunked/HTTP-2 bodies with no trustworthy
  `Content-Length`; the capped bytes are replayed for form parsing.
- SSO startup validation now runs whenever OIDC is enabled, including `both`
  mode; the status strip prefers the fresh capability cache over a one-shot
  startup snapshot.
- Privacy guard file gate now actually fails the commit (the `fail` flag no
  longer dies in a pipeline subshell); vendored assets are served with
  `script-src 'self'` and no `unsafe-eval`.

### Fixes

- Kamailio tenant-suffix gate is now a `#!substdef` (defines are not expanded
  inside quoted strings) and is applied to the REGISTER path before the mtree
  prefix lookup; `sip.<stored-domain>.evil` is rejected on both INVITE and
  REGISTER.
- TLS promotion is busybox-safe: the `current` release symlink is replaced
  with `ln -sfn` (no GNU `mv -T`), the pair always resolves inside one release,
  and the acme hook can optionally chown the release to a non-root Kamailio.
- Backups now include a WAL-safe `.console.sqlite.bak` sidecar copy with
  atomic temp+rename, rotation removes all three companions, and the restore
  runbook documents the sidecar path and schema-version refusal.
- Lifecycle: the startup/sweeper calls use the real signatures, `resume_pending`
  takes the global operation lock, active destroy checks `htable.delete` before
  touching data, and restore verifies row *presence* inside its transaction so
  a crash after the edge COMMIT but before the sidecar transition resumes
  cleanly. `CONSOLE_ENFORCE_CAPABILITIES=1` is fail-closed on an unreachable
  probe, and capability probes share the reload RPC lock.
- Reloads: enqueue persists the pending row synchronously (before the throttle
  window), in-flight intents persist on shutdown, and worker state is cleared
  in `finally` even when a store callback raises `BaseException`.
- Wizard: runs the cross-home landline prefix guard (no tenant-isolation
  bypass) and waits up to 30 s for reloads instead of falsely reporting
  "Kamailio unreachable" after 2 s.
- Forms: `row_hash` is mandatory on edit (missing = stale), the in-transaction
  conflict page carries the fresh row, both edit paths reject corrupt packed
  rows with 404, and landline `ds_onnet` is always derived from the home.
- Dialplan delete guard maps the row id to `dpid`, ignores `dpid 0`/empty block
  partitions, and names the referencing homes.
- Wizard reload warnings only mention tables from this apply batch; corrupt
  rows are skipped with a banner on list/detail/homes; status/capability,
  worker shutdown/restart, and pending-reload persistence are wired.
- UI: parked table moved out of `<title>`, active homes have a Destroy link,
  lifecycle rows hide Delete, landlines get a New-row action, stale
  "unimplemented" copy is gone, and read-only mode disables submit buttons,
  hides every write link (including the empty-state onboarding CTA) and keeps
  login/sign-out reachable.
- Static assets htmx 2.0.4, PicoCSS 2.0.6 and project `app.js` are vendored;
  `hx-on` handlers moved to external event listeners so the CSP can stay
  strict.

### Ops

- `.dockerignore`, pinned dependency list, compose "own stack" guidance,
  environment-variable table, OIDC dependency note, release examples and
  version metadata aligned to 0.1.10.
- Privacy: guard rejects `.env.*` variants (except the reviewed `.env.example`);
  test/live fixtures and docs use the documented alpha/beta/gamma placeholders;
  repository history rewritten to remove the earlier deployment identifiers.
- Backups: the sidecar restore runbook quarantines WAL/SHM with the old file,
  and the live gate restores a T1 landline row instead of asserting a tautology.

## [0.1.9] - 2026-11-05

Second review round: hardening and correctness fixes across auth, lifecycle,
forms, backups and the Kamailio routes.

- OIDC flow/session domain separation (`typ`), CSRF bound to the signed
  session, per-state flow cookies, fail-closed allowlist with
  `email_verified`, `azp`/`sub`/`iat`, alg-aware `at_hash`, https-only endpoints,
  discovery/JWKS single-flight, failure-only rate limiting, logout state.
- Session `amr` claim and SSO-only mode enforcement; token sessions are
  rejected in `oidc` mode on every route.
- Security headers (CSP, nosniff, frame-ancestors), a 1 MiB body limit,
  OIDC-secret redaction, and SameSite=Lax cookies.
- Stale-edit re-check inside the write transaction, derived landline
  `ds_onnet`, dialplan delete guard, restore row-count verification, global
  lifecycle lock and capability gate, wizard reload warnings, corrupt-row
  tolerance, and worker resilience/persistence.
- Kamailio route fixes: pinger arguments, 403 delivery, one `direction=` pair,
  `dpid 0`, PSTN gating, re-INVITE anchoring, `WGIP`/`WGSOCK`, regcache
  `autoexpire`, TLS `!aNULL`, and atomic certificate release directories.

## [0.1.8] - 2026-11-05

First multi-lens review round.

- Fixed a critical OIDC flow-cookie-as-session bypass; removed the public
  fallback secret and empty-token login; bound CSRF to the session; moved
  cookies to SameSite=Lax.
- Added OIDC hardening (per-tab flow cookies, fail-closed allowlist,
  `email_verified`, `azp`/`sub`/`iat`, alg-aware `at_hash`, https-only,
  rate limits, audit, sign-out in read-only mode), and installed the image's
  OIDC dependencies.
- Fixed Kamailio route bugs (pinger iterator order, 403 replies, duplicate
  `direction=`, `dpid 0`, `pstn_on`, re-INVITE anchoring, `WGIP`/`WGSOCK`,
  regcache expiry, `aNULL`), wired the lifecycle UI/sweeper, and mapped the
  error taxonomy to HTTP statuses.
- Privacy guard checks staged blobs and blocks `.env.*`, keys and databases;
  deploy docs drop the nonexistent `dgram_max_size`.

## [0.1.7] - 2026-11-05

- OIDC sign-in: authorization code + PKCE, issuer-bound JWKS, allowlist,
  session rotation, logout, and the fake-provider negative matrix.

## [0.1.6] - 2026-11-05

- OIDC design and UX states: login/denied/expired/account chip, threat model,
  dependency decision and the T11 task breakdown.

## [0.1.5] - 2026-11-05

- Repository renamed to `voip-edge`; image/container remains
  `voip-edge-console`.

## [0.1.4] - 2026-11-05

- ACME/TLS deployment scripts: DNS-01 issue/renew, staged promotion, atomic
  release layout and the certificate reload watcher.

## [0.1.3] - 2026-11-05

- Neutralized example numbering data in docs/fixtures (alpha/beta/gamma and
  the documented landline range).

## [0.1.2] - 2026-11-05

- Removed seeded country-specific number profiles; number rules are
  configured through the console/UI.

## [0.1.1] - 2026-11-05

- Privacy hardening: sanitized configs/docs, pre-commit guard, `.gitignore`
  coverage, and the first history purge.

## [0.1.0] - 2026-11-05

### Added

- Descriptor-driven console (T0–T10 from `docs/plan.md`): pack/unpack engine,
  `tables.yaml` grammar, SQLite WAL layer, JSON-RPC unix-datagram client,
  coalescing reload worker with pending-intent persistence, auth/CSRF,
  generic read/edit/delete UI, validation families, stale-edit 409, landline
  prefix interruption, onboarding wizard, tenant lifecycle
  park/restore/destroy with durable resume, live dashboard, capability probes,
  read-only mode, audit/backups pages, Docker/compose packaging and ops docs.
- Live Kamailio integration gate (`tests/live/run-live.sh`): park → REGISTER
  403 → restore → 200.

### Notes

- `WITH_CONSOLE` ifdef added to the edge repo's `kamailio.cfg` (jsonrpcs
  datagram socket).
- The Docker image was authored and statically reviewed in an offline
  workspace; the first real `docker build` must be verified from a clean
  checkout (see `docs/ops/deploy.md`).
