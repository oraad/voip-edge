# OIDC authentication

Status: implemented (v0.1.7) with Authlib + cryptography. Tests:
`tests/api/test_oidc.py`.

The console mounts the routing DB and RPC socket, so authentication is the
highest-value control on the edge.

## Modes

| `CONSOLE_AUTH_MODE` | Behaviour |
|---|---|
| `both` | SSO + token break-glass (default) |
| `oidc` | SSO only; token form hidden |
| `token` | Token only; `/auth/*` returns 404 |

Token sessions use a signed, `HttpOnly`, `SameSite=Lax` cookie and double-submit
CSRF. Set `CONSOLE_TOKEN` and `CONSOLE_SECRET` explicitly.

## Protocol

- Authorization Code + PKCE (S256).
- `state`, `nonce` and verifier in a 5-minute signed flow cookie.
- Exact redirect URI: `https://<console-host>/auth/callback`.
- ID-token checks: issuer-bound JWKS, RS256/ES256 allowlist, exact `iss`,
  `aud`, `azp`, `exp`/`iat` with 60 s skew, `nonce`, `at_hash` when present.
- Account key is `(iss, sub)`, never email/username.
- Fresh session on login; store only `sub`, `iss`, `email`, `csrf`, `exp`.
- No tokens in the session cookie, logs or local storage.
- Logout: clear local session, then RP-initiated logout. POST + CSRF only.
- `next` limited to same-origin relative paths.

## Allowlist

`OIDC_ALLOWED_GROUPS` (any-of) and/or `OIDC_ALLOWED_EMAIL_DOMAINS`. Without
either, SSO fails closed unless `OIDC_ALLOW_ANY=1`. Denial is a 403 naming the
missing requirement.

## Identity provider

Register the console with:

```text
Redirect URI: https://console.example.net/auth/callback
Post-logout:  https://console.example.net/login
Scopes:       openid profile email groups
```

Set `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`,
`OIDC_REDIRECT_URI`, and the allowlist in `.env`. The client secret is a
runtime secret, never committed. `OIDC_INSECURE=1` is loopback development
only.

## Threat model

| Risk | Control |
|---|---|
| Console compromise | PKCE, issuer pinning, strict redirect, session rotation |
| CSRF | double-submit + OIDC `state` |
| Session fixation | fresh session on login |
| Open redirect | relative `next` only |
| Token leakage | no tokens stored; logs redact secrets |
| Replay | single-use `state`, `nonce`, one code exchange |
| Provider mix-up | single issuer, discovery URL pinned by env |

## Known gaps

- No back-channel logout (single-instance console).
- No idle expiry; sessions expire after 12 h absolute.
- No UserInfo enrichment; ID-token claims are authoritative.
