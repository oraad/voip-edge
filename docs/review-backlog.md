# Review backlog (post multi-lens review, v0.1.8)

Findings from the four-lens review that were **not** fixed in v0.1.8. Items are
grouped by severity; file references are as of v0.1.8.

## Security — remaining

1. **Session authentication method not recorded (S14).** `auth_mode=oidc` does
   not invalidate token-era sessions. Add `amr` (`token`/`oidc`) to the session
   payload and reject `token` sessions when the mode is `oidc`; document that a
   mode change otherwise needs `CONSOLE_SECRET` rotation.
2. **`Secure` cookie flag remains opt-in (S16).** Default is still off; derive
   from the public HTTPS URL or refuse `auth_mode=oidc` without
   `CONSOLE_SECURE_COOKIE=1`. Add HSTS at the TLS terminator.
3. **Unbounded request bodies (S23/C5).** The CSRF middleware and `/login` read
   whole bodies. Enforce a small `Content-Length` cap (e.g. 1 MiB) before
   reading; return 413.
4. **Security headers (S27).** No CSP, `frame-ancestors`, `nosniff`,
   `Referrer-Policy` or `Cache-Control: no-store` on auth responses; add a
   response-header middleware.
5. **Stateless session revocation (S28).** A stolen cookie is valid until
   `exp`. Consider `jti` + sidecar revocation list, or document the window.
6. **Rate-limit bookkeeping (C36).** `login_rate_exceeded` records successful
   logins despite its docstring; per-IP deques are never pruned.
7. **IdP subject/email bounds (S20).** `sub` is now capped at 255 in the token
   validator, but session email/subject length is not capped when writing the
   cookie; cap both and reject control characters.
8. **OIDC hardening nits:** id-token `id_token_hint`/logout `state` are not
   sent (documented design: no token storage); no single-flight lock on
   discovery/JWKS refresh; redaction does not include `OIDC_CLIENT_SECRET`.

## Correctness — remaining

9. **Stale-edit TOCTOU (C11).** Hash re-check happens before the write
   transaction; re-read inside the transaction (or use a predicate UPDATE).
10. **Delete guard blind to `flags` block partitions (C12).** The generic
    reference scanner does not parse packed `flags`; deleting a dialplan row
    used as `block_dpid` succeeds. Add a dialplan-family reference hook.
11. **Restore can silently skip colliding inserts (C13).** Compare inserted
    counts against the snapshot and fail the step; read collision checks in one
    transaction.
12. **Lifecycle intent race (C14).** `_ensure_no_pending` is per-op and not
    atomic with `create_intent`; check any non-DONE intent for the domain and
    create under a lock/transaction.
13. **Capability gate not enforced (C15).** `require_capability("htable.delete")`
    exists but park/destroy do not call it; refuse before backup/delete.
14. **Wizard ignores reload failures (C16).** `flush()` result and
    `worker.errors` are discarded; warnings are lost on the non-htmx redirect.
15. **Corrupt packed rows return 500 (C18).** `unpack_row` errors are not
    caught in list/detail; skip + badge, and call `validate_against_db` at
    startup.
16. **Status strip capabilities never refresh (C20).** Uses startup snapshot;
    prefer the TTL cache and mark stale results unknown.
17. **Family form checks fail open (C22).** `_run_form_checks` swallows
    exceptions; rule modules also swallow `sqlite3.Error` as "no rows". Fail
    closed on unexpected errors.
18. **Reload worker resilience (C25/C26).** `BaseException` kills the thread
    permanently; lazy RPC/worker construction races. Catch at the boundary,
    restart on enqueue, and create under a lock.
19. **`tenant_landline.ds_onnet` derivation (C9).** Declared derived but not
    implemented; a forged POST can store the wrong set id.
20. **Async handlers block the loop (C6).** Lifecycle now runs in the
    threadpool; forms/wizard still do blocking sqlite/backup work in `async def`.
21. **Exact domain matching (K16).** `mt_match` uses longest-prefix, so a
    subdomain can match a tenant entry. Verify exact equality or use an exact
    table; align `schema.md`.
22. **`console.sqlite` is not included in backups (ops 10).** Parked tenants
    survive only as `.parked.json` evidence; add a sidecar backup via the
    SQLite backup API and make missing-sidecar backup fail loudly.
23. **Sidecar migration/version guard (ops 21).** Older images do not refuse a
    newer `user_version`; add a hard check.
24. **TLS pair promotion is not atomic (K17).** Two `mv`s plus marker; document
    the window or use a directory swap. Add cert-file ownership/group for the
    non-root variant.

## Operations / packaging — remaining

25. **`.dockerignore` and pinned base digest (ops 17).** Build context currently
    includes `.venv`/`.git`; pin `python:3.13-slim@sha256:…` and constrain
    transitive deps or use a lock file.
26. **Reproducible dev env (ops 18).** `.venv` config references the old path
    and relies on system packages; document `pip install -e '.[oidc]'` from a
    fresh venv (extra no longer defined — install the runtime deps directly).
27. **Non-root recipe ACL coverage (ops 15/16).** Default ACLs do not cover
    existing `edge.sqlite*`; add recursive `setfacl` and cert `chown`.
28. **TLS promotion + renewal visibility (ops 11/13).** Document staging→live
    promotion, add an external cert-expiry check, and surface acme failures.
29. **Backup rotation/versions drift (ops 24).** Changelog now covers 0.1.8;
    keep README/AGENTS test counts in sync when tests change.
30. **Compose hardening (security 18).** `cap_drop`/`no-new-privileges` added;
    consider a read-only root filesystem, tmpfs, and mounting only the socket
    file instead of the whole run directory.
