# OIDC authentication for the console (design, best practices, task plan)

Status: **implemented** (v0.1.7) with Authlib + cryptography, including the
negative-test matrix against an in-process fake provider (16 tests). See §7 for
the dependency note and §9 for what remains optional.

Audience: single operator, but the console mounts the routing DB and the
Kamailio RPC socket, so authentication is the highest-value control on the
whole edge.

## 1. Current state

- Token login: `POST /login` with `CONSOLE_TOKEN`, signed session cookie
  `{sub, csrf, exp}` (HMAC, `HttpOnly`, `SameSite=Strict`, optional `Secure`),
  12 h absolute expiry, login rate limit 5/min/IP.
- CSRF: double-submit via header (htmx) or hidden field (plain forms).
- No external identity provider, no per-user identity (single `sub="token"`).

OIDC replaces "who is this operator" while keeping the same session/CSRF
machinery. Token login stays as **break-glass** behind an env switch.

## 2. Protocol decisions (from current OIDC best practice)

| Decision | Value / rule |
|---|---|
| Flow | **Authorization Code + PKCE (S256)**, even for a confidential client. Never implicit/hybrid. |
| Binding | `state` (replay/CSRF), `nonce` (ID-token replay) and the PKCE verifier stored **server-side in a short-lived flow cookie** (5 min, `SameSite=Lax`, `HttpOnly`, `Path=/auth`), one per in-flight login (multi-tab safe). |
| Redirect URI | Exact match, registered at the IdP: `https://<console-host>/auth/callback`. No wildcards. |
| ID-token validation | Signature against the **issuer-bound JWKS** (`kid` resolved only inside that set), `alg` allowlist (RS256/ES256), exact `iss`, `aud` contains client_id, `azp` when multiple audiences, `exp`/`iat` with ≤60 s skew, `nonce` equal to the stored one, `at_hash` when present. |
| Claims | Account key is **`(iss, sub)`** — never email/username. `email`/`name` are display only. |
| UserInfo | Optional; if used, its `sub` MUST equal the ID-token `sub`. |
| Local authorization | Env allowlist: `OIDC_ALLOWED_GROUPS` (any-of) and/or `OIDC_ALLOWED_EMAIL_DOMAINS`. Denied → 403 page naming the missing requirement (not "access denied" alone). |
| Sessions | Issue a **fresh** session on login (fixation defence); store only `sub`, `iss`, `email`, `csrf`, `exp` — **never** tokens. 12 h absolute expiry (implemented). Idle expiry (30 min) is an optional follow-up: it requires rotating the cookie on activity, which is deferred to keep the first version's session semantics simple. |
| Logout | Local session clear **first**, then RP-initiated logout (`end_session_endpoint`, `id_token_hint`, registered `post_logout_redirect_uri`, `state`). No GET logout (CSRF); POST with existing CSRF check. Back-channel logout: out of scope for a single-instance console — documented as a known gap. |
| Errors | Typed states: `idp_unreachable`, `invalid_state`, `token_rejected`, `claims_denied`, `clock_skew`, `callback_mismatch` — each renders a distinct operator-facing message with the correlation id, never a stack trace. |
| Transport | Discovery/JWKS fetched with `httpx` (timeouts, TLS verified), cached (discovery 1 h, JWKS until `kid` miss + 5 min floor); **never** fetch a URL supplied by a token. |
| Break-glass | `CONSOLE_AUTH_MODE=oidc|token|both` (default `both`): `token` keeps today's flow; `oidc` hides the token form. Documented as incident access. |
| Secrets | `OIDC_CLIENT_SECRET` via env/docker secret; never in git (the privacy guard already blocks `.env`/secret patterns). |

## 3. Backend design (FastAPI, no SPA)

New module `console/oidc.py`:

```
GET  /auth/login?next=/homes        -> build auth URL (state, nonce, PKCE), set flow cookie, 302
GET  /auth/callback?code&state      -> verify state, redeem code, validate ID token,
                                       check allowlist, rotate session, 303 next
POST /auth/logout                   -> clear session, then 303 to IdP end-session (or /login)
```

- `OIDCSettings` in `console/settings.py`: issuer, client_id, client_secret,
  scopes (default `openid profile email groups`), allowed groups/domains,
  redirect URI (derived from a configurable public base URL — never from
  `Host`), post-logout URI, auth mode.
- Discovery at startup (non-blocking, like the capability probe) + on
  demand; store in `app.state.oidc` with health surfaced in the status strip.
- `next` handling: only relative paths (`safe_return_to`), reject `//host`.
- Login endpoint is rate-limited with the existing limiter; callback
  consumption is single-use (`state` cookie cleared whatever the outcome).
- Audit: log `oidc_login` / `oidc_logout` / `oidc_denied` with `iss`,
  hashed `sub` (or subject id), outcome — never tokens.
- Compatibility: if `CONSOLE_AUTH_MODE=token`, `/auth/*` returns 404 and the
  token form stays; if `oidc`, the token form is hidden and POST /login
  returns 403 (break-glass only when explicitly enabled).
- No tokens in the session cookie, so cookie size stays small; no refresh
  tokens are requested (`offline_access` absent) — the console does not call
  downstream APIs.

## 4. Frontend / UX (see wireframe tab “Login & account (OIDC)”)

- **Login page**: one primary action `Sign in with <Provider>`, secondary
  `Use an access token` (break-glass, collapsed `<details>`), and a short
  line about what the console controls. Error summary at top with the
  actionable cause (`The identity provider is unreachable — retry`, `Your
  account is not in an allowed group: edge-operators`), focus moved to it.
- **Return-path preservation**: `?next=` survives the round-trip; after login
  you land where you were.
- **Account affordance**: top-right chip in the status strip showing
  `email` (or `sub`), with a menu: *Sign out* (POST), provider session note.
  No avatar fetching (privacy).
- **Session expiry**: a full-page interstitial (“Session expired — sign in
  again”) with the same next-path, rather than a bare redirect loop.
- **Denied page**: names the requirement and how to fix it (ask the admin to
  add you to `edge-operators`), plus the `sub` in mono for support.
- **IdP down**: login page still renders; error names it and offers the
  break-glass path if enabled. Never block the console's own health page.
- **Accessibility**: single primary button, error summary `tabindex=-1`,
  provider button is a real link (works without JS), no auto-redirect on
  page load.

## 5. Threat model notes

- Console compromise = edge compromise (DB + RPC socket): hence PKCE,
  issuer pinning, strict redirect, session rotation, short idle timeout,
  and audit lines.
- CSRF: unchanged double-submit for POSTs; OIDC `state` covers the callback.
- Session fixation: fresh session id/cookie on login.
- Open redirect: `next` restricted to same-origin relative paths.
- Token leakage: no tokens in cookies, logs, or `localStorage` (no JS state).
- Clock skew: 60 s tolerance, explicit `clock_skew` error state.
- Replay: `state` single-use, `nonce` checked, code exchanged once.
- Provider mix-up: single-issuer config; discovery URL pinned by env.

## 6. Scenarios to cover in tests

1. Happy path: login → callback → session → `next` honoured.
2. `state` mismatch / replayed callback → `invalid_state`, no session.
3. `nonce` mismatch → `token_rejected`.
4. Wrong `iss` / `aud` / expired token / bad signature / `alg=none` → rejected.
5. Allowlist: group present / absent (403 page), email domain present/absent.
6. UserInfo `sub` mismatch → rejected even if ID token valid.
7. `next=//evil.example` or absolute URL → dropped, lands on `/homes`.
8. IdP discovery down → login page shows `idp_unreachable`; health still 200.
9. Logout: POST only; session cleared before redirect; IdP end-session called
   with `id_token_hint`; CSRF-less POST → 403.
10. Auth mode `token` hides `/auth/*`; `oidc` hides token form; `both` works.
11. Session fixation: pre-login cookie value cannot access pages after login
    (cookie rotated).
12. Audit: login/denied/logout recorded without tokens.

## 9. Implementation notes

- `console/oidc.py`: discovery/JWKS caches (issuer-bound), signed 5-minute flow
  cookie (state/nonce/PKCE verifier/next), algorithm allowlist (rejects
  `alg=none` and algorithm confusion), ID-token claims validation, `at_hash`,
  group/email-domain allowlist, session rotation, RP-initiated logout.
- `console/app.py`: login page renders the SSO button + collapsible token
  fallback per `CONSOLE_AUTH_MODE`; account chip + POST sign-out in the status
  strip; `/auth/*` 404 in token-only mode; token POST 403 in SSO-only mode.
- Tests: `tests/api/test_oidc.py` — happy path, wrong aud/iss, expired (outside
  skew), nonce, bad signature, `alg=none`, state mismatch, group/domain denial,
  open-redirect neutralisation, discovery outage, auth modes, logout CSRF.
- Dependencies: Authlib + cryptography (Debian packages `python3-authlib`,
  `python3-cryptography`; or the `oidc` extra). Both are optional at runtime:
  without `OIDC_ISSUER` the console behaves exactly as before.

## 7. Dependency decision (history)

ID-token verification requires JWT signature checking. This environment has
**no `cryptography`, `authlib`, `joserfc`, `pyjwt`** and no network, so any
code written here would be untestable and unverifiable — unacceptable for the
component that guards the edge.

Options, in preference order:

1. **Authlib** (`authlib>=1.6`) — mature, FastAPI/Starlette integration,
   handles discovery, JWKS, state/nonce/PKCE, ID-token validation; ~2 direct
   deps (`cryptography`). Best default.
2. **oidcauth-py** — purpose-built (PKCE always, encrypted flow cookie,
   strict ID-token checks, FastAPI adapter, in-process fake provider for
   tests). Younger project; excellent test story.
3. Hand-rolled flow + `joserfc` for JWTs — fewest moving parts, most code to
   review; only if the above are unacceptable.

Recommendation: **Authlib**, pinned. It is a normal runtime dependency in
`pyproject.toml` (`authlib>=1.6`, with `cryptography>=43`), not an optional
extra: the console imports the OIDC module on every start and the image must
always be able to validate tokens. Tests use Authlib's client with a stub
provider (httpx transport) or `oidcauth`-style fake — no network needed at test
time beyond localhost.

## 8. Task breakdown (T11, TDD, after deps are available)

| Task | Red first |
|---|---|
| T11.1 settings + discovery cache | config parsing; discovery timeout/health; never-block startup |
| T11.2 flow start (`/auth/login`) | state/nonce/PKCE in flow cookie; 302 params exact; multi-tab isolation |
| T11.3 callback validation | matrix: state/nonce/iss/aud/exp/signature/alg/at_hash (stub provider) |
| T11.4 session + allowlist | session rotation; groups/domain allow; 403 page wording; `next` safe |
| T11.5 logout | POST-only; clear-then-redirect; end-session params |
| T11.6 auth modes + token fallback | `oidc`/`token`/`both` behaviour; /login gating; 404s |
| T11.7 UI + wireframe states | login/denied/expired/account chip; a11y assertions (Playwright) |
| T11.8 docs/ops | IdP app registration (redirect URIs, scopes, groups claim), env table, break-glass runbook |

Estimate: ~4–5 days including the negative-test matrix; the security work is
in the tests, not the happy path.
