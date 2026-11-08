# Static assets

This directory is mounted at `/static` and copied into the console image. The
console **never** loads assets from a CDN at runtime; it must work on a host
with no outbound network.

`base.html` references three files:

| Path | Purpose | Version |
|---|---|---|
| `/static/app.css` | project design system: tokens, dark/light themes, layout, components | this repo |
| `/static/htmx.min.js` | htmx 2 (fragment swaps, CSRF header wiring) | **2.0.4** pinned |
| `/static/app.js` | project-owned htmx handlers and theme toggle (CSP-safe) | this repo |

The console uses a custom token-based CSS layer instead of a classless
framework. It is intentionally small and dashboard-oriented: semantic tokens,
dark-first surfaces, a light override via `data-theme="light"`, and components
for tables, forms, panels, badges, wizards and destructive dialogs.

## Status in this tree

`app.css`, `app.js` and `htmx.min.js` are vendored/committed. The console loads
them from its own origin only; the CSP is `script-src 'self'` with no
`unsafe-eval`.

## Vendoring htmx

Do this only from a release artifact you have verified. Never fetch at runtime
and never use a `latest` URL.

1. Download htmx **2.0.4** — `htmx.min.js`.
2. Copy it into this directory with the exact name `base.html` expects:
   ```bash
   cp /path/to/htmx-2.0.4/dist/htmx.min.js web/static/htmx.min.js
   ```
3. Record the upstream SHA-256 in the table below and keep it with the release
   notes. Compare the hash against the upstream release page before committing.
   ```bash
   sha256sum web/static/htmx.min.js
   ```
4. Rebuild the console image. `web/static` is copied into the image; there is
   no runtime asset mount. Run `pytest tests/api/test_hardening_round3.py` to
   confirm the static URLs return 200.

## Integrity record

| File | Version | SHA-256 |
|---|---|---|
| `htmx.min.js` | 2.0.4 | `e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447` |

Do not mix versions in one deployment. `app.css` and `app.js` are edited in
this repo; changing them requires a console image rebuild, not a restart.

## Docker

`Dockerfile.console` copies `web/static` into the image and serves it from
`/static`. A missing file is a 404, not a build failure; run a page smoke test
after changing assets.
