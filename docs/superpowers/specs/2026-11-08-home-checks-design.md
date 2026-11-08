# Home checks — design

Date: 2026-11-08
Status: draft for approval
Scope: Homes list, home detail, shared check helpers

## Intent

After onboarding, an operator needs one durable place per home to see whether
the tenant rows agree, whether the edge loaded the PBX and trusted state, and
what still needs a manual call. The Homes list must name the failing piece
without opening every home.

## Goals

- Replace the placeholder `Coherence` column with a DB-only badge.
- Add a real home detail page at `/homes/<domain>`.
- Put the full checklist, live edge probe and registered phones on that page.
- Keep the live probe: the list is DB-only, the detail page probes Kamailio on
  demand. The wizard verify page and its probe also remain.
- Keep the raw registry row reachable through the generic table browser.
- Reuse the wizard's check code; do not duplicate rules.

## Non-goals

- New validation rules or descriptor grammar changes.
- Automatic PSTN or audio calls.
- Editing routing rows from the detail page beyond the existing Edit link.
- A fleet-wide checks page; Homes already is that view.

## Approaches considered

1. **Row link only.** Add `Verify` beside `View`. Cheapest, but leaves the
   coherence column dead and gives no room for phones or other checks.
2. **Checks inside the generic table detail.** Fits the current click path but
   forces home-specific logic into `tables_ui`, which is table-agnostic.
3. **List badge plus real home detail (chosen).** Matches `docs/plan.md` §115
   and the wireframe; keeps home logic in `homes_ui`.

## Design

### Shared checks module

Move the check code out of `wizard_ui` into `console/checks.py`:

- `verify_state(conn, domain)` — registry values + trusted source.
- `verify_checklist(conn, domain)` — current DB checklist.
- `dispatcher_target_ok`, `trusted_entries`, `build_live_checks`.
- `coherence_summaries(conn, domains)` — one bulk pass for the list.
- Re-export the public names from `wizard_ui` so existing tests/imports keep
  working.

`coherence_summaries` reads dispatcher, trusted, landline and dialplan once,
then derives each home's state. It returns `{tone, text, missing}`:

- `ok` / `all green`.
- `warn` / first missing item, such as `no trusted source` or `set 11 missing`.
- PSTN absent is neutral, never a failure.

### Homes list

- `Coherence` renders a badge from `coherence_summaries`.
- Domain and `View` link to `/homes/<domain>`.
- Actions: `Verify`, then the existing `Park…` / `Destroy…`.
- The raw row link stays inside the detail page.

### Home detail

`GET /homes/<domain>` in `homes_ui`, authenticated:

- Head: domain, landline, route sets, active/parked state.
- Summary: edge control, checks status, route-set count.
- `Checks` panel: the same checklist items as the wizard verify page.
- `Run live checks` button: on-demand fragment with dispatcher, trusted and
  registered-phone rows for this domain.
- `PBX trunk` panel: on-net/PSTN targets and the trusted source, with the
  static trunk reminder.
- `Manual checks`: call through the edge; landline/PSTN call when enabled.
- Actions: `Edit`, `View raw registry row`, `Park…`, `Destroy…`.

Live probe routes:

- `GET /homes/<domain>/live` returns a home-specific partial.
- Reuse `build_live_checks`; add a `regcache` read filtered to
  `@<domain>` for the phones table.
- RPC failure renders `Edge unreachable`, never a 500.

### Where other checks live

| Check | List | Detail | Live probe |
|---|---|---|---|
| Registry row exists | badge | checklist | — |
| On-net set exists | badge | checklist | loaded/active |
| PSTN set exists | neutral/badge | checklist | loaded/active |
| Trusted source | badge | checklist | loaded |
| Landline target | badge | checklist | — |
| Emergency dialplan | badge | checklist | — |
| Registered phones | — | count | AOR/flow/transport |
| Pending reload/error | — | reload banner | — |
| Manual PBX/PSTN call | — | manual list | — |

## Files

- Create `console/checks.py`.
- Modify `console/wizard_ui.py` to import and re-export checks.
- Modify `console/homes_ui.py` with detail and live routes.
- Modify `web/templates/homes.html`.
- Create `web/templates/home_detail.html`.
- Create `web/templates/partials/home_live.html`.
- Tests: `tests/unit/test_checks.py`, `tests/api/test_home_detail.py`,
  update `tests/api/test_homes_list.py`.

## Calls made

- The list stays DB-only; no RPC on page load.
- The detail page renders with the edge down; live checks are a second click.
- No new descriptor capability; the detail page is home-specific, not a
  generic table feature.
- Registered phones are filtered from `htable.dump` by domain; no new RPC.
