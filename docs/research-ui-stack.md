# Web Admin Console Stack Research (Nov 2026)

> Historical reference. Current deployment and operations are in
> `docs/ops/deploy.md` and `docs/operations.md`.

Research digest for a lightweight, single-operator web admin console for the Kamailio SIP edge proxy: Python backend, SQLite CRUD (incl. "packed" comma-joined record columns rendered as structured validated forms), Kamailio RPC calls, small Docker image, minimal JavaScript.

## Q1 — Framework recommendation

**Recommended: FastAPI + Jinja2Templates + htmx 2.x + Pydantic.** Runner-up worth serious consideration: **Litestar + Jinja2 + htmx**.

Decision table:

| Candidate | Weight / deps | Server-rendered CRUD fit | Validation round-trips | Maturity (2025/26) | Verdict |
|---|---|---|---|---|---|
| **FastAPI + Jinja2 + htmx** | Small: fastapi, jinja2, uvicorn; pydantic already present; htmx ~14 KB gzip via CDN/vendored file | Very good. Return HTML fragments (`HTMLResponse`); branch full page vs fragment on `HX-Request` header. De-facto pattern documented widely (testdriven.io) | Excellent. Pydantic models validate form data; catch `RequestValidationError` and re-render the form partial with field errors (422 semantics work well with htmx) | Very mature, huge ecosystem, pydantic v2. OpenAPI docs free (useful for Kamailio RPC debugging) | **Pick** |
| Flask + Jinja2 + htmx | Equally small | Good, Jinja2 bundled; sync request model is fine for 1 operator | Manual or WTForms/Marshmallow — weaker than pydantic; no free API docs | Very mature ("boring tech" is a plus per HN Hyperflask thread), but lacks typing/DI/async; community steers new projects to FastAPI | Fine if team knows Flask, otherwise no |
| **Litestar + Jinja2 + htmx** | Small; msgspec or pydantic; `litestar[htmx]` | Very good — **official htmx plugin** (`HTMXRequest`, `HTMXTemplate`, typed `HX-*` response helpers: `HXLocation`, `TriggerEvent`, `ClientRedirect`) | Good, but HN users note awkward "validate form and re-render with errors" DX — Litestar validation targets API schema mismatch, not per-field form errors | Actively maintained, community-driven (litestar-org); v3 moved htmx to the `litestar[htmx]` extra | Strong #2; best if you want first-class htmx plumbing and less boilerplate |
| Datastar (with any backend) | ~11 KB single file; supersedes htmx+Alpine | Good; **server decides what to swap** (idiomorph morphing by element id) instead of request-side hx-target/hx-swap; SSE is first-class (great for "probe state" / live dialog status panels) | Supported, but expression-based validation model differs; smaller community means fewer Python form patterns | Reached 1.0 in 2025 (~30+ RCs before); officially endorsed as an alternative on htmx.org; still young, smaller ecosystem, known reactivity edge-case issues open | Watchlist. Consider if live SSE status panels become central |
| Django + htmx | Heavy (ORM, auth, admin you won't use) | Excellent | Excellent (Django forms) | Extremely mature; largest django-htmx community | Overkill: you manage a SQLite file Kamailio-flavored, not a Django ORM schema; admin auto-gen doesn't fit packed columns |
| Reflex (ex-Pynecone) / SvelteKit | Heavy: full Node/React (Reflex) or Node build toolchain (SvelteKit) | SPA model, not server-rendered fragments | Client-side state management | Mature but wrong shape | **Reject** — violates "minimal JavaScript / tiny container" |

Notes:
- fasthx / fastapi-htmx packages cut boilerplate (decorators for partial-vs-full rendering) — optional, the raw `HX-Request` header check is 3 lines.
- Kamailio RPC from Python: use plain `httpx` against jsonrpcs/HTTP RPC module; FastAPI async makes concurrent RPC calls (reload + probe) clean; Litestar equally fine.
- Sources: reddit r/htmx backend thread (Jun 2025), testdriven.io FastAPI+htmx guide, recology.info Litestar notes (Oct 2025), Field-TM ADR replacing FastAPI w/ Litestar, htmx.org alternatives essay, Datastar 1.0 coverage (2025), zread.ai HTMX/Alpine/Datastar comparison, HN Litestar thread (Aug 2025).

## Q2 — CSS / design system

**Recommended: PicoCSS (v2) if "least CSS written" dominates; daisyUI+Tailwind (standalone CLI, no Node) if you want richer components and don't mind a class-per-element style.** For this console, PicoCSS is the sweet spot; add daisyUI later only if you outgrow it.

| Option | Weight | Dark mode | Tables / badges / forms | Build step | Notes |
|---|---|---|---|---|---|
| **PicoCSS v2** | ~10 KB min (one CDN/vendored file) | Native (`prefers-color-scheme`, toggleable via `data-theme`) | Semantic tables/inputs styled with **zero classes**; limited component set (cards, dialogs, accordions, dropdowns since v2) | None | Best CSS:work ratio. Semantic HTML stays clean next to hx- attributes. Weakness: fewer "admin-ish" components (no real badge/toast/menu kit) |
| **daisyUI 5 + Tailwind v4** | daisyUI ~40 KB CSS; Tailwind standalone CLI (single binary) or full minified CDN build — cache it once | 30+ themes incl. dark, `data-theme` attr — a server-rendered fragment can flip theme | Excellent: `table table-zebra`, `badge`, `btn`, `form-control`, `modal`, `toast`, `alert`, stats | Tailwind v4 standalone binary avoids Node entirely | Most popular pairing in the htmx community (r/htmx, daisyUI's own htmx docs); richest admin vocabulary; biggest payload |
| Bootstrap 5 | ~25 KB + optional JS | Built-in since 5.3 (`data-bs-theme`) | Full admin toolkit; every example/copilot knows it | None | Safe, boring. Modal/offcanvas JS works but coexists ok with htmx. Used by fasthx-admin reference impl |
| Beer CSS (M3) | Tiny, CDN-first, no build | Yes, dynamic theming | Good: tables, badges/chips, dialogs, fields | None | Reddit hobbyist favorite; Material 3 look is less "network ops console"; smallest community of the four |
| Tabler | Heavier | Yes | **Purpose-built admin template kit** (cards, tables, dropdowns, empty states) | None | Worth a look if the console grows; it's what FastAPI-Admin/Starlette-Admin ship |

Verdict for best out-of-the-box professional look w/ least CSS: **PicoCSS** (write semantic HTML, done; dark mode free). If "professional admin" = dense data grids, badges, toasts, modals: **daisyUI via Tailwind standalone CLI**.

Sources: r/htmx UI-framework threads (Jul, Oct, Nov 2025), daisyui.com/htmx-component-library, LogRocket top CSS frameworks 2025, beercss.com.

## Q3 — UX rules for admin CRUD consoles

Consolidated actionable rules, with sources:

**Validation & errors** (GOV.UK Design System, NN/g, W3C DS, CMS DS, Baymard):
- Always validate server-side (`novalidate` on forms; HTML5 validation off). Client-side only as enhancement.
- Validate on submit by default; don't error fields before the user leaves them. Inline-per-keystroke only where input is genuinely hard (Baymard: recheck errored fields on keystroke so errors vanish the moment input is valid).
- Show **both**: error summary at top (focus moved to it, links to fields, `Error:` prefixed to `<title>`) **and** inline message adjacent to each field — same wording in both. Never summary-only (NN/g: forces memorizing).
- Error text: plain language, imperative, says what to fix, reuses the field label's words; no "valid/invalid", no "please", no jargon/HTTP codes.
- Never clear user input on error; redisplay all values.
- htmx mapping: 422 → re-rendered form partial with summary + inline errors. (GOV.UK validation pattern; NN/g "10 guidelines for reporting errors in forms"; NN/g error rubric; design.cms.gov error validation; Baymard inline validation research.)

**Destructive actions** (prfct.dev destructive-actions pattern, Nubisco dialogs, Infor DS):
- **Undo > confirmation** where possible (toast with Undo for soft deletes). Only irreversible actions get a dialog.
- Confirm dialog must: name the object ("Delete trunk `carrier-b`?"), state the consequence ("in use by 3 routes — calls will fail"), verb-labeled buttons ("Delete trunk"/"Keep trunk", never Yes/No), danger styling only on the confirm button, default focus on Cancel.
- Scale the guard with blast radius: typed-name confirmation for proxy-wide reloads or wiping a table. For this console: **"Reload Kamailio" deserves an explicit confirm** (service-affecting), a row delete gets a one-line `hx-confirm`.
- Keep destructive controls away from frequent ones; danger zone at bottom of settings pages.

**List/detail vs inline editing**:
- Default to list page → dedicated edit form (full or side-panel) for the packed multi-field records here: a comma-joined column exploded into N typed inputs with per-field validation does not belong inline in a `<tr>`.
- Inline row editing only for single-field tweaks (toggle enabled, rename). FastHX-admin reference impl follows this (row actions + dedicated form pages).
- One primary action per page; demote rare/destructive actions behind menus (fluncle admin-shell placement contract).

**Dirty-form navigation guards** (client-side-form.com, mironsoft.de, Stimeo):
- Dirty = deep-compare current vs loaded snapshot, not a boolean set on first input.
- Two exits need two mechanisms: `beforeunload` for tab close/reload (browser-owned generic prompt, register only while dirty, remove when clean), and since htmx swaps don't navigate by default, a custom check for in-app links when you use `hx-push-url` navigation ("Stay / Discard / Save and leave" — three exits, not two).
- Disarm only after confirmed save success, never on request send; session-expiry redirects must bypass the guard.

**Optimistic concurrency (stale-edit)** (RFC 9110 ETag/If-Match writings, client-side-form.com 409 handling — patterns stack-neutral, apply to sqlite):
- Add a `version` INTEGER column (or updated_at hash); render it as hidden input; write with `UPDATE … WHERE id=? AND version=?`; zero rows affected = conflict → return 409 with current record.
- On conflict: show "changed since you opened this" banner, keep the user's input, offer reload/overwrite. For a single-operator console the main risk is two tabs of the same operator or a Kamailio-side reload changing state — a simple "reload latest" flow suffices; per-field 3-way merge is overkill.

**Toasts / async feedback** (Infor notification pattern, badger UX guidelines):
- Toast = transient confirmation of a user action, auto-dismiss ~6 s, includes Undo when reversible. Banner = persistent system condition. Modal = critical/blocking only.
- Always say what happened ("Reloaded 3 dispatcher sets", "Probe: 5/6 destinations responding"), not "Success". Failure toast + form stays open.
- Long-running ops (proxy reload, OPTIONS probe sweep): `hx-indicator` spinner + `hx-disabled-elt`, then toast on completion; or hx-polling/`HX-Trigger` when done.
- `aria-live` region for toast announcements.

**Empty states** (NN/g empty-state article, Waldur/Infor):
- Never render a blank table. Anatomy: relevant icon, one-line title ("No dispatcher endpoints yet"), one-line explanation, primary CTA ("Add first endpoint").
- Distinguish filtered-empty ("No routes match `carrier`" + Clear filters) from first-use-empty.
- Skeleton > spinner for loading, to avoid layout jump.
- Reserve error color/iconography for errors only.

## Q4 — htmx patterns for this app

Table/list refreshes:
- Row action → swap just the row or tbody: `hx-delete="/routes/12" hx-target="closest tr" hx-swap="outerHTML swap:0.3s"` (return 200 with empty body, or swap `delete`/return the updated `<tbody>` when order/filters matter).
- After create/edit, return the refreshed `<tbody id="route-rows">` fragment; use `hx-target="#route-rows" hx-swap="innerHTML"`.
- Keep filter/search state in `hx-vals`/URL with `hx-push-url="true"` so a tbody refresh keeps query state; search boxes: `hx-trigger="input changed delay:300ms" hx-swap="innerHTML"`.

Validation round-trip:
- `hx-post` form → `hx-target="#form-container" hx-swap="outerHTML"`; on 422 return the same form partial with errors + preserved values. Enable 422 swapping:
  `htmx:beforeSwap` handler: `if (evt.detail.xhr.status === 422) { evt.detail.shouldSwap = true; evt.detail.isError = false; }`
  (Note htmx 2 doesn't swap 4xx/5xx by default; htmx 4 adds `hx-status` per-code control.)
- Packed columns: server splits the comma-joined value into N validated fields, renders each with its own error slot; on save, re-join after individual validation. Pydantic model + custom validator does this cleanly.
- After successful save, fire `HX-Trigger: {"showToast": {...}}` response header; a tiny bit of JS in the base layout turns that event into a toast.

Confirms:
- `hx-confirm="Delete trunk 'carrier-b' (3 routes depend on it)?"` for simple cases (native dialog, zero JS).
- For anything service-affecting ("Reload Kamailio proxy"), replace with an htmx-native modal (`htmx:confirm` event + native `<dialog>`, or sweetalert-style per htmx docs) that names consequence explicitly.

Indicators / in-flight state:
- `hx-indicator` targets a spinner; `hx-disabled-elt="this"` prevents double-submit of reload buttons.
- Global top progress: `hx-indicator="body"` pattern with CSS on `.htmx-request`.
- Place the indicator **outside** any `outerHTML`-swapped target or it gets replaced mid-flight.

Status panels (probe results, dispatcher state):
- Out-of-band swaps: `hx-swap-oob="true"` on `<div id="probe-panel">` in a normal response to piggyback status updates with any save/delete.
- **Pitfall (tables)**: OOB `<tr>`/`<td>` can't stand alone in the DOM — wrap in `<template>` tags (htmx docs "Troublesome Tables").
- **Pitfall (nesting)**: nested `hx-swap-oob` inside a main fragment is still processed and yanked out of the DOM — set `htmx.config.allowNestedOobSwaps = false` if you reuse fragments both ways.
- Polling: `hx-trigger="load, every 15s"` for dispatcher/probe state; `HX-Stop-Polling`/286 response to stop when idle. Litestar ships typed helpers for these headers.
- Alternative: Datastar/idiomorph-style server-driven morph; htmx users who dislike request-side hx-target logic run "hx-swap-oob only" (toolofthought.com pattern) so all swap logic lives server-side.

Response headers worth using: `HX-Trigger` (toasts, refresh other panels), `HX-Location` (redirect after create without full reload), `HX-Refresh` (rare), `HX-Push-Url`.

General pitfalls:
- Default request queueing drops overlapping triggers — use `hx-sync` on search inputs.
- htmx swaps full responses if you don't branch on HX-Request — always test both paths.
- Fragments reusing the base template accidentally re-render layout chrome into a target — assert `<!doctype` absence in tests.

## Q5 — Testing tooling & strategy

Tooling list:
- **pytest + FastAPI `TestClient`** (Starlette/httpx-based, in-process, no socket) — the workhorse.
- **Fixture**: `htmx_headers = {"HX-Request": "true"}` (+ `HX-Target` where routes branch on it, e.g. 403 unknown target hardening).
- Fragment tests: send header, assert status, assert `text/html`, assert fragment present/`<!DOCTYPE html>` **absent**, assert hx- attributes emitted; test the same route without the header returns full page.
- Parse fragments with **BeautifulSoup (bs4)** or **selectolax** for structural assertions ("row has 5 `<td>`s", "error `<span id=field-x-error>` present") instead of brittle string matches.
- **pytest-asyncio** only if routes are async; TestClient runs sync regardless.
- 422 path: post bad packed value, assert 422 + error markup + preserved input values.
- RPC calls: fake Kamailio via `respx` (httpx mock) or a tiny fixture ASGI app; assert JSON-RPC payload correctness separately from UI tests.
- sqlite: tmp_path fixture seeding a fresh DB per test; test packed-column pack/unpack as pure functions.
- **Playwright + pytest-playwright** for E2E: full user journeys only ("create dispatcher entry → appears in table → reload proxy → toast shown"), verify no full navigation occurred (listen for `framenavigated`), verify `htmx-request` class appears/disappears (loading states), verify OOB status panel updates. Budget: Playwright ~2–3 s/test vs TestClient ms/test — keep ~5–15 journey tests, everything else request-scoped.
- Concurrency: two "tabs" = two edits against a tmp DB; assert the second save gets 409/412 and conflict UI text.
- CI: docker-less (TestClient in-process); Playwright job with `uvicorn` live server fixture, headless, `--browser chromium` only.

Sources: helpmetest.com "Testing HTMX with FastAPI" (2026, concrete TestClient+Playwright recipes), DjangoCon EU 2025 (Jacob Rief) pytest+Playwright for django-htmx, quran-srs testing-approach.md (HX-Request header testing, TestClient-vs-Playwright rule of thumb), hedron HTMX scaffold testing (HX-Target 403), htmx GitHub discussion #680.
