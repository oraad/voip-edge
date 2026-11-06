# Review notes: accepted risks and follow-ups (v0.1.10)

The final independent gates (security, correctness, SIP/config and
operations/privacy) returned **GO** at 786 tests, with the live
park/restore journey passing. The entries below are **intentional** for this
single-operator deployment or are non-blocking follow-ups; nothing here blocks
v0.1.10.

## Accepted risks

1. **Stateless sessions cannot be revoked before expiry (12 h).** Logout clears
   the session and flow cookies; a stolen cookie lives until `exp`. Accepted for
   a single-operator console behind Pangolin SSO; revisit with `jti` + a
   revocation table if the console becomes multi-user.
2. **RP-initiated logout sends no `id_token_hint`.** The design stores no
   tokens (deliberate). Providers show a logout confirmation instead. Accepted.
3. **Forms/wizard still do blocking SQLite work on async handlers.** Lifecycle
   paths are offloaded to the threadpool; the remaining writes are single-row
   and bounded by the 2 s busy timeout on a `workers=1` console. Accepted.
4. **Domain matching is longest-prefix with a suffix gate.** A stored domain
   still prefix-matches names that end in the configured tenant suffix; the
   `TENANTSUFFIXRE` gate (INVITE and REGISTER) blocks the practical spoofing
   vector. Exact matching would need a second htable/DB lookup per request.
   Accepted.
5. **Base image pinned by tag, not digest.** `.dockerignore` is in place; pin a
   digest (and ideally a lockfile for transitives) when publishing the image.
6. **Generated credentials when env is unset.** `CONSOLE_TOKEN` and
   `CONSOLE_SECRET` fall back to per-process random values (never a public
   default and never derived from each other). A random token means nobody can
   sign in, and a random secret invalidates sessions on restart; production
   must set both. This is fail-closed rather than a public default.

## Follow-ups (non-blocking)

- The image now builds and passes a local smoke test (Python 3.15.2, non-root,
  `/health` 200, healthy); a full compose run against a real edge host is
  still pending.
- Browser end-to-end coverage is limited to API tests and the server-rendered
  page assertions; add Playwright coverage for htmx swaps and the readonly UI.
- Certificate ownership for a non-root Kamailio is supported through
  `KAM_UID`/`KAM_GID` in the acme hook, but not automated in compose.
- Compose runs the console as root with `cap_drop: ALL` and
  `no-new-privileges`; a read-only rootfs + tmpfs is the next hardening step.
- Add an alert on certificate `notAfter` and acme renewal failures
  (`docs/tls.md` §Renewal visibility).
- Optional OIDC features: back-channel logout, UserInfo enrichment, idle
  timeout (30 min) in addition to the absolute 12 h expiry.
