# Edge Trunk Onboarding and Verification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the onboarding wizard state the required PBX edge trunk, verify both trunk directions automatically, verify PSTN when enabled, and use PBX-neutral wording.

**Architecture:** The wizard stays stateless and descriptor-driven. Step 2 gains a note whose address comes from optional deployment settings. Step 4 keeps the SQLite coherence checklist, adds explicit edge→PBX and PBX→edge items plus manual call items, and adds an on-demand htmx probe that reads Kamailio's loaded dispatcher and trusted tables through the existing RPC client.

**Tech Stack:** FastAPI, Jinja2, htmx, SQLite, Kamailio JSON-RPC, pytest.

**Spec:** `docs/superpowers/specs/2026-11-08-edge-trunk-verification-design.md`

## Global Constraints

- PyPI/network is unavailable; use the repo virtualenv `.venv/bin/python`.
- No new per-table route; wizard routes stay generic.
- No template logic in `console/`; no table logic in `web/`.
- `edge.sqlite` is never replaced or deleted.
- Endpoints only enqueue reloads; the live probe only reads.
- Wording must be PBX-neutral: no voip-stack/HT813 in operator-facing wizard or descriptor help.
- The console stays `workers=1`; probing is synchronous on click only.
- Optional settings: `EDGE_TRUNK_HOST` (default empty) and `EDGE_TRUNK_PORT` (default `5060`).

## Review Focus

- Kamailio unreachable during a live probe: the verify page still renders and the panel says the edge is unreachable.
- `permissions.trustedDump` missing or malformed: the probe reports "not available", never raises.
- Home with no PSTN trunk: the PSTN item is informational and does not make `all_green` false.
- `EDGE_TRUNK_HOST` unset: the note uses generic wording and shows no empty host.
- Dispatcher destination is an FQDN, missing, or in the wrong set: the edge→PBX item fails or names the mismatch.

---

### Task 1: Edge trunk settings, neutral wording, and the step-2 note

**Files:**
- Modify: `console/settings.py`
- Modify: `.env.example`
- Modify: `console/wizard_ui.py`
- Modify: `web/templates/wizard.html`
- Modify: `console/tables.yaml`
- Modify: `console/validators.py`
- Modify: `console/rules/dispatcher.py` (docstring only)
- Modify: `console/rules/tenant.py`
- Test: `tests/api/test_wizard.py`
- Test: `tests/api/test_forms_dispatcher.py`
- Test: `tests/unit/test_rules_dispatcher.py`

**Interfaces:**
- Produces: `Settings.edge_trunk_host: str`, `Settings.edge_trunk_port: int`.
- Produces: `WizardDraft` unchanged; template context keys `edge_trunk_host`, `edge_trunk_port`, `edge_trunk_display`.

- [ ] **Step 1: Write failing wizard note tests**

In `tests/api/test_wizard.py`, add:

```python
def test_step2_shows_pbx_edge_trunk_note(seeded_db, auth_client, monkeypatch):
    from dataclasses import replace as _replace
    monkeypatch.setattr(
        settings_module,
        "settings",
        _replace(
            settings_module.settings,
            db_path=str(seeded_db),
            edge_trunk_host="203.0.113.9",
            edge_trunk_port=5060,
        ),
    )
    page = auth_client.post("/wizard/step1", data=IDENTITY)
    text = " ".join(html.unescape(page.text).split())
    assert "Add the edge trunk on the PBX" in text
    assert "203.0.113.9:5060" in text
    assert "Do not register" in text
    assert "never the tenant's public SIP domain" in text
    assert "PBX LAN IP" in text
    assert "voip-stack" not in text.lower()


def test_step2_note_without_edge_host_is_generic(seeded_db, auth_client, monkeypatch):
    from dataclasses import replace as _replace
    monkeypatch.setattr(
        settings_module,
        "settings",
        _replace(settings_module.settings, db_path=str(seeded_db), edge_trunk_host=""),
    )
    page = auth_client.post("/wizard/step1", data=IDENTITY)
    text = " ".join(html.unescape(page.text).split())
    assert "the edge tunnel address from your deployment" in text
    assert "voip-stack" not in text.lower()
```

Also update existing assertions in `tests/api/test_forms_dispatcher.py` and `tests/unit/test_rules_dispatcher.py` from `"use the voip-stack host's LAN IP"` to `"use the PBX host's LAN IP"`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/api/test_wizard.py -k "step2_shows_pbx or step2_note_without" -v`
Expected: FAIL because the settings fields and note do not exist.

- [ ] **Step 3: Add the settings**

In `console/settings.py`, after `tls_live`:

```python
    # Optional onboarding hint: the literal edge tunnel address the home PBX
    # must use for its static trunk. Empty shows generic wording.
    edge_trunk_host: str = os.environ.get("EDGE_TRUNK_HOST", "")
    edge_trunk_port: int = int(os.environ.get("EDGE_TRUNK_PORT", "5060"))
```

In `.env.example`, after `TENANT_ALLOWED_SUFFIXES`:

```dotenv
# Optional: literal edge tunnel address shown in the wizard trunk note.
#EDGE_TRUNK_HOST=100.64.0.9
#EDGE_TRUNK_PORT=5060
```

- [ ] **Step 4: Neutralize operator-facing wording**

In `console/wizard_ui.py`:

- `FIELD_LABELS["lan_ip"] = "PBX LAN IP"`.
- Dispatcher descriptions become `f"{draft.domain} PBX (native routing)"` and `f"{draft.domain} PSTN via PBX (LAN-only)"` in both `plan_writes` and `validate_step`.
- Add a helper:

```python
def edge_trunk_display(host: str, port: int) -> str:
    return f"{host}:{port}" if host else ""
```

In `web/templates/wizard.html` replace the LAN IP label/hint with `PBX LAN IP` and PBX copy, and add after that field:

```html
<div class="tlsnotice" role="note" id="edge-trunk-note">
  <b>Add the edge trunk on the PBX.</b>
  Create one static SIP trunk to this edge. Do not register it.
  {% if edge_trunk_display %}
  <div class="mono">Server / outbound proxy: {{ edge_trunk_display }}</div>
  <div class="mono">Allowed incoming IP: {{ edge_trunk_host }}</div>
  {% else %}
  Use the edge tunnel address from your deployment as the server and
  outbound proxy, and allow that address as an incoming source.
  {% endif %}
  Use the literal tunnel address, never the tenant's public SIP domain:
  split-horizon DNS sends the call back into the PBX. Keep the PBX's
  carrier or PSTN trunk as a separate line.
</div>
```

In `console/tables.yaml`, replace "voip-stack" with "PBX" in the
`ds_onnet` help and the PSTN-gateway help. In `console/validators.py`, replace
`use the voip-stack host's LAN IP` with `use the PBX host's LAN IP`. In
`console/rules/tenant.py`, replace `this home's voip-stack` with `this home's
PBX`. In `console/rules/dispatcher.py`, update the module docstring to say
PBX.

In `wizard_ui.py` `_render`, add to the context:

```python
        "edge_trunk_host": settings_module.settings.edge_trunk_host,
        "edge_trunk_port": settings_module.settings.edge_trunk_port,
        "edge_trunk_display": edge_trunk_display(
            settings_module.settings.edge_trunk_host,
            settings_module.settings.edge_trunk_port,
        ),
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/api/test_wizard.py tests/api/test_forms_dispatcher.py tests/unit/test_rules_dispatcher.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add console/settings.py .env.example console/wizard_ui.py web/templates/wizard.html console/tables.yaml console/validators.py console/rules/dispatcher.py console/rules/tenant.py tests/api/test_wizard.py tests/api/test_forms_dispatcher.py tests/unit/test_rules_dispatcher.py
git commit -m "feat(wizard): state the PBX edge trunk requirement"
```

---

### Task 2: Directional DB checks and manual call items

**Files:**
- Modify: `console/wizard_ui.py` (`verify_checklist`)
- Modify: `web/templates/partials/wizard_verify.html`
- Test: `tests/api/test_wizard.py`

**Interfaces:**
- Consumes: existing `verify_checklist(conn, domain)`.
- Produces: the same dict with `items` (each `{key,label,ok,detail}`), `all_green`, and a new `manual` list of `{key,label,detail}`.

- [ ] **Step 1: Write failing API assertions**

In `tests/api/test_wizard.py`, in the happy-path verify assertions replace the
old `"Route sets present in the dispatcher"` and `"Trusted source mapped to
this home"` assertions with:

```python
    assert "PBX \u2192 edge" in green
    assert "Edge \u2192 PBX" in green
    assert "Place a call from a phone on the PBX" in green
    assert "dial out through the landline trunk" in green
```

In the no-trunk test add:

```python
    assert "No PSTN trunk configured" in page
    assert "dial out through the landline trunk" not in page
```

The no-trunk test currently builds `page` from the review response; change it
to `verify_page = " ".join(html.unescape(verify.text).split())` and assert
there.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/api/test_wizard.py -q`
Expected: FAIL on the new strings.

- [ ] **Step 3: Extend `verify_checklist`**

In `console/wizard_ui.py`:

- Rename the trusted item key to `edge_trunk_in`, label `"PBX → edge: trusted tunnel source"`.
- Replace the generic `"sets"` item with two items:
  - `edge_trunk_out`, label `"Edge → PBX: on-net route set"`. Pass when the
    `ds_onnet` set has at least one dispatcher row and every row for that set
    starts with `sip:` and contains a literal IPv4 host. Detail names the
    destination or the reason it failed.
  - `pstn`, label `"PSTN trunk route set"`. When `ds_pstn` is `0`/missing,
    `ok=True` and detail `"No PSTN trunk configured"`. Otherwise pass when the
    set has a dispatcher row; detail names the destination.
- Keep `registry`, `landline`, `dpid`.
- Add:

```python
    manual = [
        {
            "key": "call_out",
            "label": "Place a call from a phone on the PBX through the edge",
            "detail": "Confirm the call connects in both directions with audio.",
        }
    ]
    if pstn not in (None, 0):
        manual.append(
            {
                "key": "call_pstn",
                "label": "Dial out through the landline trunk",
                "detail": "Confirm the carrier answers; the edge cannot test the PBX's internal trunk.",
            }
        )
```

- Return `{"items": items, "all_green": all(...), "manual": manual}`.

Use one small helper for the literal-IPv4 destination check:

```python
def dispatcher_target_ok(destination: str) -> bool:
    # sip:<ipv4>:<port>;transport=tcp, no user part, no FQDN
```

- [ ] **Step 4: Render direction labels and manual items**

In `web/templates/partials/wizard_verify.html`, keep the existing item loop and
add below it:

```html
{% if checklist["manual"] %}
<div class="warnsummary" role="status">
  <b>Manual checks — the edge cannot see inside the PBX</b>
  <ul>
    {% for item in checklist["manual"] %}
    <li><b>{{ item.label }}</b> — {{ item.detail }}</li>
    {% endfor %}
  </ul>
</div>
{% endif %}
```

Do not include manual items in `all_green`.

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/api/test_wizard.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add console/wizard_ui.py web/templates/partials/wizard_verify.html tests/api/test_wizard.py
git commit -m "feat(wizard): verify edge trunk directions and manual calls"
```

---

### Task 3: On-demand live Kamailio probe

**Files:**
- Modify: `console/app.py` (expose the existing RPC getter)
- Modify: `console/wizard_ui.py`
- Create: `web/templates/partials/wizard_live.html`
- Modify: `web/templates/partials/wizard_verify.html`
- Test: `tests/api/test_wizard.py`
- Test: `tests/unit/test_wizard_plan.py`

**Interfaces:**
- Consumes: `console.live.build_dispatcher_view`, existing `Settings.edge_trunk_*`.
- Produces: `trusted_entries(result: Any) -> list[dict[str, str]]` (keys `ip`, `tag`).
- Produces: `build_live_checks(get_rpc, domain, source_ip, onnet_set, pstn_set) -> dict` with keys `reachable`, `uptime`, `items` (`{key,label,ok,detail}`), `errors`.
- Produces: `GET /wizard/verify/{domain}/live` returning the partial.

- [ ] **Step 1: Write failing tests**

In `tests/unit/test_wizard_plan.py` add:

```python
def test_trusted_entries_reads_text_and_structured_dumps():
    text = "table: 33\nitem: {\n ip: 100.64.10.20\n tag: sip.delta.home.example.net\n}"
    assert wizard_ui.trusted_entries(text) == [
        {"ip": "100.64.10.20", "tag": "sip.delta.home.example.net"}
    ]
    structured = [{"ip": "100.64.10.20", "tag": "sip.delta.home.example.net"}]
    assert wizard_ui.trusted_entries(structured) == [
        {"ip": "100.64.10.20", "tag": "sip.delta.home.example.net"}
    ]


def test_dispatcher_target_ok_rejects_fqdn_and_accepts_ipv4():
    assert wizard_ui.dispatcher_target_ok("sip:10.0.6.20:5060;transport=tcp")
    assert not wizard_ui.dispatcher_target_ok("sip:pbx.lan:5060;transport=tcp")
```

In `tests/api/test_wizard.py` add the live probe test against the seeded
alpha home (sets 11/31, trusted source `100.64.10.10`). Inject a `FakeRPC`
through `app.state.rpc`; no wizard apply is needed:

```python
def test_live_verify_probes_dispatcher_and_trusted_table(
    seeded_db, auth_client, app, tmp_path
):
    with FakeRPC(tmp_path / "rpc.sock") as fake:
        fake.script("core.uptime", {"uptime": "1 hour"})
        fake.script("dispatcher.list", {"NRSETS": 2, "RECORDS": [
            {"SET": {"ID": 11, "TARGETS": [{"DEST": {
                "URI": "sip:10.0.0.10:5060;transport=tcp",
                "FLAGS": "AP", "PRIORITY": 0}}]}},
            {"SET": {"ID": 31, "TARGETS": [{"DEST": {
                "URI": "sip:10.0.0.10:5060;transport=tcp",
                "FLAGS": "AP", "PRIORITY": 0}}]}},
        ]})
        fake.script("permissions.trustedDump", {"table": 33, "item": {
            "ip": "100.64.10.10",
            "tag": "sip.alpha.home.example.net",
        }})
        app.state.rpc = UnixDatagramRPC(fake.sock_path, timeout_ms=500)
        page = auth_client.get(
            "/wizard/verify/sip.alpha.home.example.net/live"
        )
    text = " ".join(html.unescape(page.text).split())
    assert page.status_code == 200
    assert "Edge reachable" in text
    assert "set 11" in text
    assert "set 31" in text
    assert "100.64.10.10" in text
    assert "loaded" in text


def test_live_verify_reports_unreachable_edge(seeded_db, auth_client, app):
    app.state.rpc = None
    page = auth_client.get(
        "/wizard/verify/sip.alpha.home.example.net/live"
    )
    assert page.status_code == 200
    assert "Edge unreachable" in page.text
```

Import `UnixDatagramRPC` from `console.rpc` and `FakeRPC` from
`tests.fixtures.fakerpc` at the top of the test module.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_wizard_plan.py -k "trusted_entries or dispatcher_target" tests/api/test_wizard.py -k "live_verify" -v`
Expected: FAIL with missing names/route.

- [ ] **Step 3: Expose the RPC getter**

In `console/app.py`, after `_get_rpc` is defined, add:

```python
    app.state.get_rpc = _get_rpc
```

This reuses the existing lazy client; no second RPC object.

- [ ] **Step 4: Implement the probe**

In `console/wizard_ui.py`:

```python
def trusted_entries(result: Any) -> list[dict[str, str]]:
    """Normalise permissions.trustedDump text or structured output."""
```

- Text: `re.findall(r"ip:\s*(\S+).*?tag:\s*(\S+)", text, re.S)`.
- Dict: accept `{"item": {...}}`, `{"ip": ..., "tag": ...}` or a list of
  either; unwrap nested `item` once.
- Skip entries without both keys.

```python
def dispatcher_target_ok(destination: str) -> bool:
    """True for sip:<literal IPv4>[:port][;transport=...]."""
```

Reuse `ipaddress.IPv4Address`; reject an `@` user part and non-`sip:` scheme.

```python
def build_live_checks(
    get_rpc: Any,
    domain: str,
    source_ip: str,
    onnet_set: int | None,
    pstn_set: int | None,
) -> dict[str, Any]:
```

- Resolve the client with `get_rpc()` inside `try/except`; `None` or an
  exception yields `reachable=False`.
- Call `core.uptime`; then `dispatcher.list` and `permissions.trustedDump`.
- Catch every exception per call and append a readable `errors` entry.
- Use `build_dispatcher_view` for dispatcher rows; group by set id.
- `items`:
  - `edge_trunk_out`: rows for `onnet_set` exist and every target passes
    `dispatcher_target_ok`; detail lists `set N · <uri> · state/probe`.
  - `pstn`: if `pstn_set` is `0`/`None`, `ok=True`, detail
    `"No PSTN trunk configured"`; else rows exist and are valid.
  - `edge_trunk_in`: the trusted dump contains `source_ip` and `domain`.
- If an RPC call fails, that item is `ok=False` with the failure detail; the
  route still returns 200.

Add the route:

```python
@router.get("/wizard/verify/{domain}/live", response_class=HTMLResponse)
def wizard_verify_live(
    request: Request,
    domain: str,
    conn: sqlite3.Connection | None = Depends(db.get_read_db),
):
```

- Login required, same as the verify route.
- Read the registry row to get `ds_onnet`, `ds_pstn`, and the trusted source
  (`SELECT src_ip FROM trusted WHERE tag = ? ORDER BY id LIMIT 1`).
- Getter resolution: `app.state.get_rpc` when present, else
  `lambda: app.state.rpc`.
- Render `partials/wizard_live.html` with `live=...`, `domain=domain`.

- [ ] **Step 5: Add the panel and template**

In `wizard_verify.html`, after the checklist:

```html
<div class="panel" id="live-checks">
  <b>Live edge checks</b>
  <p class="meta">Reads the loaded dispatcher and trusted tables from Kamailio.</p>
  <button class="btn" type="button"
          hx-get="/wizard/verify/{{ draft.domain | urlencode }}/live"
          hx-target="#live-checks" hx-swap="innerHTML">Run live checks</button>
</div>
```

`partials/wizard_live.html` renders:

- `Edge reachable` or `Edge unreachable` with uptime.
- One line per `live.items`: `OK`/`Missing` + label + detail.
- Each `live.errors` line as a warning.
- Keep the button so the check can be re-run.

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/python -m pytest tests/unit/test_wizard_plan.py tests/api/test_wizard.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add console/app.py console/wizard_ui.py web/templates/partials/wizard_verify.html web/templates/partials/wizard_live.html tests/api/test_wizard.py tests/unit/test_wizard_plan.py
git commit -m "feat(wizard): probe loaded edge trunk state on demand"
```

---

### Task 4: Docs, full gate, deployment, and live verification

**Files:**
- Modify: `CHANGELOG.md`
- Modify: `docs/operations.md`
- Modify deployed `/projects/voip-edge/.env` (Arcane workspace; not git)
- Rebuild and redeploy console image

**Interfaces:**
- Consumes: all previous tasks.

- [ ] **Step 1: Document the settings**

In `docs/operations.md` env table add:

```markdown
| `EDGE_TRUNK_HOST` | optional literal edge tunnel address shown in the wizard trunk note |
| `EDGE_TRUNK_PORT` | edge trunk port shown in the note; default `5060` |
```

In `CHANGELOG.md`, under `### Changed`, add the wizard note, directional
verification, manual PSTN checks, and the neutral PBX wording.

- [ ] **Step 2: Run the full gate**

Run:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall console
./tools/check-privacy.sh --all
```

Expected: pytest passes (baseline grows by the new tests), compileall exit 0,
privacy exit 0.

- [ ] **Step 3: Commit**

```bash
git add CHANGELOG.md docs/operations.md docs/superpowers
git commit -m "docs: describe edge trunk onboarding verification"
```

- [ ] **Step 4: Deploy the console**

- Add `EDGE_TRUNK_HOST=100.91.0.9` and `EDGE_TRUNK_PORT=5060` to the deployed
  `.env` file.
- Build `voip-edge-console:0.1.11-rpcfs-ui11` with the changed console and
  template files.
- Point `CONSOLE_IMAGE` at `ui11` and recreate the console through Arcane.
- Do not change Kamailio.

- [ ] **Step 5: Verify live**

- `/health` returns 200.
- The wizard step 2 shows `100.91.0.9:5060`.
- The verify page for the live home shows both direction items and the manual
  items.
- The live probe shows the dispatcher sets, their probe state, and the trusted
  source loaded.
- Confirm the existing home still shows `Edge control: Online`.

- [ ] **Step 6: Record the deployment**

Update the project memory entry with the new image tag and the optional
`EDGE_TRUNK_HOST` setting.
