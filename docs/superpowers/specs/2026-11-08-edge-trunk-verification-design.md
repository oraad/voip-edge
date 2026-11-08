# Edge trunk onboarding and verification — design

Date: 2026-11-08
Status: draft for approval
Scope: onboarding wizard, verify checklist, PBX-neutral wording

## Intent

A home is not ready when its rows exist. Its PBX must also have a static SIP
trunk to the edge, and the edge must trust the PBX tunnel source. Operators
need one place that states that requirement, shows the exact edge address,
and proves both directions are loaded in Kamailio before the home is announced.
The wizard must read correctly for any PBX, not only the voip-stack integration.

## Goals

- State the required PBX edge trunk in the wizard, with copyable values when
  the deployment provides them.
- Verify edge → PBX: the on-net dispatcher set exists in SQLite and Kamailio
  loaded it, including the runtime probe state.
- Verify PBX → edge: a trusted source row exists for the home and Kamailio
  loaded the trusted table.
- Verify PSTN only when the home has a trunk: the PSTN dispatcher set is
  present and loaded.
- Say plainly which checks are automatic and which need a manual test call:
  the edge cannot inspect the PBX's internal trunk, carrier or audio.
- Replace voip-stack/HT813 wording in wizard and descriptor help with
  PBX-neutral wording.

## Non-goals

- Editing the PBX configuration from the console.
- Dialing test calls or playing audio.
- Verifying the carrier or the PBX's internal PSTN trunk.
- Polling or changing the registration cache.

## Constraints

- `edge.sqlite` stays DB-only for the base checklist; live probing is a
  separate, on-demand request.
- No new route per table; the wizard remains descriptor-driven and stateless.
- The console stays single-process; the probe reuses the existing RPC client.
- PBX-neutral wording: "PBX", "PBX LAN IP", "edge trunk". No vendor names.
- The edge trunk address is deployment data, never a hard-coded host.

## Approaches considered

1. **DB checks plus live Kamailio probe (chosen).** Step 4 keeps the existing
   checklist and adds an on-demand "Run live checks" panel that calls
   `core.uptime`, `dispatcher.list` and `permissions.trustedDump`. Proves the
   loaded state without slowing the page. Handles an unreachable edge as a
   visible skipped state.
2. DB checks plus manual checklist. Simpler, but cannot prove the reload
   actually loaded and cannot show dispatcher probe state.
3. Send the operator to the Live page. Rejected: splits one verification task
   across two screens and still does not map rows to this home.

## Design

### Configuration

Add two optional settings, read from `.env`:

- `EDGE_TRUNK_HOST` — literal edge tunnel address the PBX must use.
- `EDGE_TRUNK_PORT` — default `5060`.

When `EDGE_TRUNK_HOST` is empty, the wizard shows generic wording ("the edge
tunnel address from your deployment") and no copyable host. The production
`.env` sets the current WGIP so the live console shows the exact values.

### Step 2 — edge trunk note

Add a note panel after the PBX LAN IP field:

- Create a static SIP trunk on the PBX. Do not register.
- Server / outbound proxy: `EDGE_TRUNK_HOST:EDGE_TRUNK_PORT` (or generic).
- Allow incoming calls from the same address.
- Use the literal address, never the tenant's public SIP domain; split-horizon
  DNS would loop calls back into the PBX.
- Keep the PBX's carrier/PSTN trunk separate.

Rename the field and hints:

- "voip-stack host LAN IP" → "PBX LAN IP"
- "voip-stack" in dispatcher descriptions and validator hints → "PBX".

### Step 4 — checklist

Keep the base DB checklist and add explicit direction labels:

- `edge_trunk_in` — "PBX → edge: trusted tunnel source" — trusted row for the
  home, with the source address.
- `edge_trunk_out` — "Edge → PBX: on-net route set" — dispatcher row exists,
  destination is a literal IPv4 TCP URI, and the detail names the target.
- `pstn` — when `ds_pstn != 0`: PSTN set exists and its destination is present.
  When no trunk: informational item, not a failure.

Manual items, rendered separately and never counted as green DB checks:

- Place a call from a phone on the PBX through the edge and confirm audio.
- If the home has PSTN: dial out through the landline trunk from the PBX and
  confirm the carrier answers.

`all_green` keeps meaning "all automatic checks pass".

### Step 4 — live probe

A button posts `GET /wizard/verify/{domain}/live` with htmx and fills a panel:

- Edge health: `core.uptime`.
- Edge → PBX: `dispatcher.list` parsed with the existing
  `build_dispatcher_view`; report the on-net and PSTN rows, state and probe
  token.
- PBX → edge: `permissions.trustedDump`; pass when the home source address
  appears in the dump. If the method is unavailable, say so and fall back to
  the DB check.

Failure states are explicit: edge unreachable, method unsupported, set
missing, or probe not answered. The probe never blocks the normal verify page
and never raises out of the route.

### Files

- `console/settings.py` — edge trunk host/port.
- `console/wizard_ui.py` — neutral labels, note values, checklist, live route.
- `web/templates/wizard.html` — step 2 note.
- `web/templates/partials/wizard_verify.html` — direction items, manual list,
  live panel and button.
- New `web/templates/partials/wizard_live.html` — probe fragment.
- `console/tables.yaml`, `console/validators.py` — neutral help/hints.
- `.env.example`, `CHANGELOG.md`, `docs/operations.md` — config and docs.
- Tests: wizard API (note, manual items, live probe with FakeRPC, unreachable
  edge), validator-hint updates.

## Calls made

- Live probe is on demand, not on page load, so the verify page still renders
  when Kamailio is down.
- `EDGE_TRUNK_HOST` is optional; correctness does not depend on it.
- PSTN is shown as "not configured" rather than failed when the home has no
  trunk.
- Registration-cache polling stays out of scope; the first call test is the
  behavioural proof.
