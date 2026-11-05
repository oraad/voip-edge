# Vendored static assets

This directory is mounted at `/static` and copied into the console image. The
console **never** loads assets from a CDN at runtime; it must work on a host
with no outbound network.

`base.html` references exactly three files:

| Path | Purpose | Pinned version |
|---|---|---|
| `/static/pico.min.css` | PicoCSS v2 (classless baseline) | **2.0.6** |
| `/static/htmx.min.js` | htmx 2 (fragment swaps, CSRF header wiring) | **2.0.4** |
| `/static/app.js` | project-owned htmx event handlers (CSP-safe, no `hx-on`) | this repo |

## Status in this tree

All three files are **vendored and committed**. The console loads them from its
own origin only; the CSP is `script-src 'self'` with no `unsafe-eval`.

## Vendoring procedure

Do this only from release artifacts you have verified. Never fetch at runtime
and never use a `latest` URL.

1. Download the exact releases:
   - PicoCSS **v2.0.6** — `pico.min.css`
   - htmx **2.0.4** — `htmx.min.js`
2. Copy them into this directory with the exact names `base.html` expects:
   ```bash
   cp /path/to/pico-2.0.6/css/pico.min.css  web/static/pico.min.css
   cp /path/to/htmx-2.0.4/dist/htmx.min.js  web/static/htmx.min.js
   ```
3. Record the upstream SHA-256 in the table below and keep it with the release
   notes. Compare the hash against the upstream release page / signed artifact
   before committing.
   ```bash
   sha256sum web/static/pico.min.css web/static/htmx.min.js
   ```
4. Rebuild the console image. `web/static` is copied into the image; there is
   no runtime asset mount. Run `pytest tests/api/test_hardening_round3.py` to
   confirm all three URLs return 200.

## Integrity record

| File | Version | SHA-256 |
|---|---|---|
| `pico.min.css` | 2.0.6 | `dd5fd5591afd81ee21dcc117ad85c014dc3f1f19dc2d7b7d101ea0acc29274c2` |
| `htmx.min.js` | 2.0.4 | `e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447` |

> The version numbers above are the pins this repo documents. If you deliberately
> upgrade, change the version in the table, update the two SHA-256 values, run
> the page/fragment tests, and rebuild the image. Do not mix versions in one
> deployment.

## Docker

The Dockerfile copies `web/static` into the image and serves it from
`/static`. A missing file is a 404, not a build failure; run a page smoke test
after vendoring to confirm both assets return 200.
