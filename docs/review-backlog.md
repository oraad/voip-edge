# Review notes: accepted risks and follow-ups (v0.1.9)

The second review round closed the open findings from v0.1.8. What remains is
**intentional**, either accepted for this single-operator deployment or a
follow-up that does not change the security posture.

## Accepted risks

1. **Stateless sessions cannot be revoked before expiry (12 h).** Logout clears
   the cookies and flow cookies; a stolen cookie lives until `exp`. Accepted for
   a single-operator console behind Pangolin SSO; revisit with `jti` + a
   revocation table if the console becomes multi-user.
2. **RP-initiated logout sends no `id_token_hint`.** The design stores no
   tokens (deliberate). Providers show a logout confirmation instead. Accepted.
3. **Forms/wizard still do blocking sqlite work on async handlers.** Lifecycle
   paths are offloaded to the threadpool; the remaining writes are single-row
   and bounded by the 2 s busy timeout on a `workers=1` console. Accepted.
4. **Domain matching is longest-prefix with a suffix gate.** A stored domain
   still prefix-matches names that end in the configured tenant suffix; the
   suffix gate blocks the practical spoofing vector. Exact matching would need
   a second htable/DB lookup per request. Accepted.
5. **Base image pinned by tag, not digest.** `.dockerignore` is in place; pin a
   digest (and ideally a lockfile for transitives) when publishing the image.

## Follow-ups (non-blocking)

- Sidecar/certificate ownership for the non-root deployment recipe is documented
  but not automated (`docs/ops/deploy.md`).
- Compose runs the console as root with `cap_drop: ALL` and
  `no-new-privileges`; a read-only rootfs + tmpfs is the next hardening step.
- Add an alert on certificate `notAfter` and acme renewal failures
  (`docs/tls.md` §Renewal visibility).
- Optional OIDC features: back-channel logout, UserInfo enrichment, idle
  timeout (30 min) in addition to the absolute 12 h expiry.
