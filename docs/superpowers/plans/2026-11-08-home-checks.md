# Home Checks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put a DB-only coherence badge on the Homes list and a real `/homes/<domain>` page with the full checklist, live edge probe, PBX/PSTN targets and registered phones.

**Architecture:** Extract the wizard's check code into `console/checks.py`, add one bulk summary query for the list, and add a home-scoped detail route in `homes_ui`. The detail page renders from SQLite and probes Kamailio only on demand.

**Tech Stack:** FastAPI, Jinja2, htmx, SQLite, Kamailio JSON-RPC, pytest.

**Spec:** `docs/superpowers/specs/2026-11-08-home-checks-design.md`

## Global Constraints

- The Homes list makes no RPC call.
- The detail page renders with Kamailio down; live checks are a separate request.
- `console/tables_ui.py` stays table-agnostic; no home logic there.
- Reuse the wizard check code; do not duplicate rules.
- No descriptor grammar changes.
- Full gate before deploy: `.venv/bin/python -m pytest -q`, `.venv/bin/python -m compileall console`, `./tools/check-privacy.sh --all`.

## Review Focus

- Home with no PSTN trunk: neutral, not a failure.
- Kamailio down: list renders, detail renders, live panel says unreachable.
- Missing trusted source or dispatcher set: badge names it; detail checklist agrees.
- Parked home or missing registry row: detail must not render a phantom home.
- Registered phones filter: only AORs at this domain, never another home's.

---

### Task 1: Extract shared check code

**Files:**
- Create: `console/checks.py`
- Modify: `console/wizard_ui.py`
- Test: `tests/unit/test_checks.py`

**Interfaces:**
- Produces `checks.verify_state(conn, domain) -> (registry, values, source_ip)`.
- Produces `checks.verify_checklist(conn, domain) -> dict`.
- Produces `checks.dispatcher_target_ok`, `checks.trusted_entries`,
  `checks.build_live_checks`.
- `wizard_ui` re-exports the same names so existing imports keep working.

- [ ] **Step 1: Write the import-compatibility test**

```python
def test_wizard_reexports_shared_checks():
    from console import checks, wizard_ui

    assert wizard_ui.verify_checklist is checks.verify_checklist
    assert wizard_ui.build_live_checks is checks.build_live_checks
    assert wizard_ui.trusted_entries is checks.trusted_entries
    assert wizard_ui.dispatcher_target_ok is checks.dispatcher_target_ok
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_checks.py -q`
Expected: FAIL because `console.checks` does not exist.

- [ ] **Step 3: Move the functions**

Move `dispatcher_target_ok`, `trusted_entries`, `_live_targets`,
`_live_detail`, `build_live_checks`, `_verify_state` and `verify_checklist`
from `console/wizard_ui.py` to `console/checks.py`. Update imports:

```python
# console/checks.py
import re
import sqlite3
from typing import Any, Mapping

from . import db, descriptors, validators
```

In `console/wizard_ui.py`:

```python
from .checks import (
    build_live_checks,
    dispatcher_target_ok,
    trusted_entries,
    verify_checklist,
)
```

Keep `edge_trunk_display` in `wizard_ui`.

- [ ] **Step 4: Run the check and wizard suites**

Run: `.venv/bin/python -m pytest tests/unit/test_checks.py tests/unit/test_wizard_plan.py tests/api/test_wizard.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add console/checks.py console/wizard_ui.py tests/unit/test_checks.py
git commit -m "refactor(checks): share onboarding verification code"
```

---

### Task 2: Bulk coherence summaries and Homes badge

**Files:**
- Modify: `console/checks.py`
- Modify: `console/homes_ui.py`
- Modify: `web/templates/homes.html`
- Test: `tests/api/test_homes_list.py`
- Test: `tests/unit/test_checks.py`

**Interfaces:**
- Produces `checks.coherence_summaries(conn, domains) -> dict[str, dict]` with
  `{tone, text, missing}` per domain.
- `homes_ui.homes` adds `check_tone`, `check_text`, `verify_url` per row.

- [ ] **Step 1: Write failing list tests**

In `tests/api/test_homes_list.py`:

```python
def test_homes_coherence_badge_names_a_missing_trusted_source(
    seeded_db, auth_client
):
    conn = sqlite3.connect(seeded_db)
    conn.execute(
        "DELETE FROM trusted WHERE tag = ?", ("sip.alpha.home.example.net",)
    )
    conn.commit()
    conn.close()

    page = auth_client.get("/homes")
    text = html.unescape(page.text)
    assert "no trusted source" in text
    assert 'href="/homes/sip.alpha.home.example.net"' in page.text
```

Keep a green case asserting `all green` for a complete home.

- [ ] **Step 2: Run and confirm it fails**

Run: `.venv/bin/python -m pytest tests/api/test_homes_list.py -q`
Expected: FAIL because the badge and home link do not exist.

- [ ] **Step 3: Implement bulk summaries**

In `console/checks.py`:

```python
def coherence_summaries(conn, domains):
    """One bulk pass: registry assumptions already hold; name the first gap."""
```

Read `dispatcher` (setid, destination), `trusted` (tag), `tenant_landline`
(tprefix, tvalue) and `dialplan` (dpid) once. For each domain unpack its
registry values from the caller-provided registry rows or a single registry
read. Return:

```python
{"tone": "ok" | "warn", "text": "all green" | "<first gap>", "missing": [...]}
```

PSTN `0` is not a gap. Examples of gap text: `no trusted source`,
`set 11 missing`, `landline row missing`, `dpid 10 missing`.

In `homes_ui.homes`, collect domains, call `coherence_summaries`, and set
`check_tone`, `check_text`, `verify_url=f"/homes/{quote(domain, safe='')}"`.

- [ ] **Step 4: Render the badge and link**

In `web/templates/homes.html`:

```html
<td><span class="badge {{ 'b-ok' if home.check_tone == 'ok' else 'b-warn' }}">{{ home.check_text }}</span></td>
```

Change the domain and `View` links to `{{ home.verify_url }}`, and add a
`Verify` action next to them. Keep Park/Destroy unchanged.

- [ ] **Step 5: Run tests**

Run: `.venv/bin/python -m pytest tests/api/test_homes_list.py tests/unit/test_checks.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add console/checks.py console/homes_ui.py web/templates/homes.html tests/api/test_homes_list.py tests/unit/test_checks.py
git commit -m "feat(homes): show coherence and link to home checks"
```

---

### Task 3: Home detail page and live checks

**Files:**
- Modify: `console/checks.py`
- Modify: `console/homes_ui.py`
- Create: `web/templates/home_detail.html`
- Create: `web/templates/partials/home_live.html`
- Test: `tests/api/test_home_detail.py`

**Interfaces:**
- Produces `GET /homes/{domain}` and `GET /homes/{domain}/live`.
- Produces `checks.build_home_live(get_rpc, domain, source_ip, onnet, pstn)`
  returning the live dict plus `phones`.

- [ ] **Step 1: Write failing API tests**

```python
def test_home_detail_shows_checklist_and_manual_calls(seeded_db, auth_client):
    page = auth_client.get("/homes/sip.alpha.home.example.net")
    text = _text(page.text)
    assert page.status_code == 200
    assert "Coherence checks" in text
    assert "Edge → PBX" in text
    assert "PBX → edge" in text
    assert "Place a call" in text
    assert 'hx-get="/homes/sip.alpha.home.example.net/live"' in page.text


def test_home_detail_404_for_unknown_home(seeded_db, auth_client):
    assert auth_client.get("/homes/sip.missing.example.net").status_code == 404


def test_home_live_filters_phones_to_this_domain(
    seeded_db, auth_client, app, tmp_path
):
    # FakeRPC scripts core.uptime, dispatcher.list, permissions.trustedDump,
    # and htable.dump with alpha and beta AORs.
    # Assert alpha AOR appears and beta AOR does not.
    ...


def test_home_live_reports_unreachable_edge(seeded_db, auth_client, app):
    app.state.rpc = None
    app.state.get_rpc = lambda: None
    page = auth_client.get("/homes/sip.alpha.home.example.net/live")
    assert "Edge unreachable" in page.text


def test_home_detail_requires_login(client):
    response = client.get("/homes/sip.alpha.home.example.net")
    assert response.status_code == 303
```

- [ ] **Step 2: Run and confirm it fails**

Run: `.venv/bin/python -m pytest tests/api/test_home_detail.py -q`
Expected: FAIL with 404.

- [ ] **Step 3: Implement the live helper and routes**

In `console/checks.py`:

```python
def build_home_live(get_rpc, domain, source_ip, onnet_set, pstn_set):
    live = build_live_checks(get_rpc, domain, source_ip, onnet_set, pstn_set)
    live["phones"] = []
    if live["reachable"]:
        ...
    return live
```

Use `console.live.build_regcache_view` and keep only rows whose `domain`
equals `domain`.

In `console/homes_ui.py`:

- `GET /homes/{domain}`: read `verify_state`, `verify_checklist`; 404 when no
  registry row. Render `home_detail.html` with domain, values, checklist,
  cancel/park/destroy URLs.
- `GET /homes/{domain}/live`: call `build_home_live`; render
  `partials/home_live.html`. Prefer `app.state.rpc`, fall back to
  `app.state.get_rpc`, same as the wizard route.

- [ ] **Step 4: Build the templates**

`home_detail.html`:

- Page head: domain, landline, route sets, `Active`.
- Summary strip: check status, edge mode, route-set count.
- `Coherence checks` panel: the checklist items, same semantics as the
  wizard verify page.
- `Live edge checks` panel: `Run live checks` button with
  `hx-disabled-elt="this"`.
- `PBX trunk` panel: on-net and PSTN destinations plus trusted source.
- `Manual checks` panel: call through the edge; PSTN call when enabled.
- Actions: Edit, View raw registry row, Park…, Destroy…

`partials/home_live.html`:

- Edge reachable/unreachable.
- Live check items with `OK` / `Missing` / `Unavailable` / `Not configured`.
- Registered phones table: AOR, flow, transport.
- Re-run button.

- [ ] **Step 5: Run the home and wizard suites**

Run: `.venv/bin/python -m pytest tests/api/test_home_detail.py tests/api/test_homes_list.py tests/api/test_wizard.py tests/unit/test_checks.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add console/checks.py console/homes_ui.py web/templates/home_detail.html web/templates/partials/home_live.html tests/api/test_home_detail.py
git commit -m "feat(homes): add home detail checks and live probe"
```

---

### Task 4: Docs, full gate, deploy

**Files:**
- Modify: `CHANGELOG.md`
- Modify: `docs/operations.md`
- Deploy console image

- [ ] **Step 1: Document the new page**

Add a short `docs/operations.md` section: Homes list coherence is DB-only;
`/homes/<domain>` shows the checklist and live probe; `/wizard/verify/<domain>`
remains for the onboarding step.

- [ ] **Step 2: Run the full gate**

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall console
./tools/check-privacy.sh --all
```

Expected: all exit 0.

- [ ] **Step 3: Commit**

```bash
git add CHANGELOG.md docs/operations.md docs/superpowers
git commit -m "docs: describe home checks"
```

- [ ] **Step 4: Deploy and verify**

- Build `voip-edge-console:0.1.11-rpcfs-ui14`.
- Deploy with `CONSOLE_IMAGE=...ui14`.
- Verify `/homes` badge, `/homes/<domain>`, live probe, phones, `/health`.
