# Vendored static assets (T3)

This directory is mounted at `/static`.

`base.html` already references the assets this console will serve:

| path | purpose |
| --- | --- |
| `/static/pico.min.css` | PicoCSS v2 (classless baseline) |
| `/static/htmx.min.js` | htmx 2 (fragment swaps, CSRF header wiring) |

They are **not committed in T2** and the environment has no network access, so
both URLs currently return 404. That is intentional and safe: every page in
T2 is server-rendered and usable without CSS or JavaScript (the base template
carries a small inline fallback stylesheet).

T3 (read-only table browser) vendors the pinned files here:

```bash
# offline copy from the release artifacts — never fetched at runtime
cp /path/to/pico.min.css  web/static/pico.min.css
cp /path/to/htmx.min.js   web/static/htmx.min.js
```

Docker builds must copy `web/static` into the image; the console never loads
assets from a CDN.
