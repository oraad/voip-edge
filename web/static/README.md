# Vendored static assets

This directory is mounted at `/static` and copied into the console image. The
console **never** loads assets from a CDN at runtime; it must work on a host
with no outbound network.

`base.html` references exactly two files:

| Path | Purpose | Pinned version |
|---|---|---|
| `/static/pico.min.css` | PicoCSS v2 (classless baseline) | **2.0.6** |
| `/static/htmx.min.js` | htmx 2 (fragment swaps, CSRF header wiring) | **2.0.4** |

## Status in this tree

The files are **not committed yet**. Until they are vendored, both URLs return
404. That is safe: every page is server-rendered and usable without CSS or
JavaScript (the base template carries a small inline fallback stylesheet).
Production should not ship without the pinned files, however, because the CSRF
header wiring and fragment swaps depend on htmx.

## Vendoring procedure

Do this once, offline, from the release artifacts you have verified. Never
fetch at runtime and never use a `latest` URL.

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
   no runtime asset mount.

## Integrity record

| File | Version | SHA-256 |
|---|---|---|
| `pico.min.css` | 2.0.6 | `TODO: fill at vendoring time` |
| `htmx.min.js` | 2.0.4 | `TODO: fill at vendoring time` |

> The version numbers above are the pins this repo documents. If you deliberately
> upgrade, change the version in the table, update the two SHA-256 values, run
> the page/fragment tests, and rebuild the image. Do not mix versions in one
> deployment.

## Docker

The Dockerfile copies `web/static` into the image and serves it from
`/static`. A missing file is a 404, not a build failure; run a page smoke test
after vendoring to confirm both assets return 200.
