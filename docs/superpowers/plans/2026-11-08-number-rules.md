# Number Rules Simplified Editor — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `/number-rules` page with Emergency, Blocked and Custom tabs that generates safe dialplan rows, while keeping `/tables/dialplan` for advanced edits.

**Architecture:** A pure helper module builds exact, prefix and range regexes. A new router renders the tabs and writes one validated `dialplan` row per submission inside the existing write transaction, then enqueues `dialplan.reload`. The generic table stays unchanged and linked as Advanced.

**Tech Stack:** FastAPI, Jinja2, htmx, SQLite, Kamailio dialplan, pytest.

**Spec:** `docs/superpowers/specs/2026-11-08-number-rules-design.md`

## Global Constraints

- Only a home's emergency `dpid` and blocked `block_dpid` partitions affect calls; warn on unreferenced custom partitions.
- No descriptor grammar change.
- Use `db.write_transaction` and the existing `ReloadWorker`; never call a reload RPC from a request handler.
- Read-only mode disables every create/edit/delete action.
- Keep `/tables/dialplan` fully functional as Advanced.

## Review Focus

- A blocked short code and a blocked prefix must not collide with an emergency exact match.
- Range generation must be exact: no match outside start..end, every value inside matches.
- No partition assigned: show guidance, not a broken form.
- Duplicate `(dpid, match_exp)` must be blocked with a clear link to the existing rule.
- Read-only mode must not expose a posting form.

---

### Task 1: Pattern helpers

**Files:**
- Create: `console/number_rules.py`
- Test: `tests/unit/test_number_rules.py`

**Interfaces:**
- `parse_entries(text) -> list[str]`
- `exact_pattern(numbers) -> str`
- `prefix_pattern(prefixes) -> str`
- `range_pattern(start, end) -> str`

- [ ] **Step 1: Write failing tests**

```python
def test_exact_pattern_deduplicates_and_anchors():
    assert number_rules.exact_pattern(["112", "911", "112"]) == "^(112|911)$"

def test_prefix_pattern_does_not_anchor_end():
    assert number_rules.prefix_pattern(["0900", "0901"]) == "^(0900|0901)"

def test_range_pattern_matches_only_inside_range():
    pattern = number_rules.range_pattern("1234", "1245")
    assert re.fullmatch(pattern, "1234")
    assert re.fullmatch(pattern, "1245")
    assert not re.fullmatch(pattern, "1233")
    assert not re.fullmatch(pattern, "1246")

def test_range_pattern_keeps_leading_zeroes():
    pattern = number_rules.range_pattern("0900", "0909")
    assert re.fullmatch(pattern, "0905")
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv/bin/python -m pytest tests/unit/test_number_rules.py -q`
Expected: FAIL because the module does not exist.

- [ ] **Step 3: Implement**

`parse_entries` splits on commas, spaces and newlines, trims, rejects non-digits.
`range_pattern` is a fixed-width decimal range-to-regex helper; it must escape
nothing except digits and use `(?:...)` groups. Raise `ValueError` for empty,
mixed-length or reversed ranges.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/unit/test_number_rules.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add console/number_rules.py tests/unit/test_number_rules.py
git commit -m "feat(number-rules): generate exact, prefix and range patterns"
```

---

### Task 2: Number rules page, partitions and tabs

**Files:**
- Create: `console/number_rules_ui.py`
- Create: `web/templates/number_rules.html`
- Create: `tests/api/test_number_rules.py`
- Modify: `console/app.py` (include router)
- Modify: `web/templates/base.html` (nav link `/number-rules`)

**Interfaces:**
- `GET /number-rules?tab=emergency|blocked|custom`
- Partition catalog rows: `{dpid, role, homes, rules}`
  where role is `emergency`, `blocked`, `custom`.

- [ ] **Step 1: Write failing page tests**

```python
def test_number_rules_has_three_tabs_and_advanced_link(seeded_db, auth_client):
    page = auth_client.get("/number-rules")
    text = _text(page.text)
    assert "Emergency" in text
    assert "Blocked" in text
    assert "Custom" in text
    assert 'href="/tables/dialplan"' in page.text

def test_number_rules_labels_referenced_partitions(seeded_db, auth_client):
    page = auth_client.get("/number-rules")
    text = _text(page.text)
    assert "Emergency for" in text
    assert "Blocked for" in text
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv/bin/python -m pytest tests/api/test_number_rules.py -q`
Expected: FAIL with 404.

- [ ] **Step 3: Build the catalog and page**

In `number_rules_ui.py`:

- Read `tenant_registry`; map `dpid` to emergency homes and the third
  `flags` segment to blocked homes.
- Add existing `dialplan` dpids as `custom` when not already mapped.
- Count rules per partition.
- Render tabs, partition summary, add forms and existing rules.
- Link Advanced to `/tables/dialplan`.

The template uses the existing `panel`, `field`, `fieldset`, `hint`,
`checkline` and `btn` classes.

- [ ] **Step 4: Wire the router and nav**

`app.include_router(number_rules_ui.router)` and change the nav href to
`/number-rules`; keep `nav="number-rules"`.

- [ ] **Step 5: Run tests**

Run: `.venv/bin/python -m pytest tests/api/test_number_rules.py tests/api/test_routes_crawl.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add console/number_rules_ui.py web/templates/number_rules.html console/app.py web/templates/base.html tests/api/test_number_rules.py
git commit -m "feat(number-rules): add purpose tabs and partition context"
```

---

### Task 3: Create emergency, blocked and custom rules

**Files:**
- Modify: `console/number_rules_ui.py`
- Modify: `web/templates/number_rules.html`
- Test: `tests/api/test_number_rules.py`

**Interfaces:**
- `POST /number-rules/emergency`
- `POST /number-rules/blocked`
- `POST /number-rules/custom`

Each returns 303 on save with `?tab=...&saved=...`; 422 re-renders the tab with
errors and preserved values.

- [ ] **Step 1: Write failing tests**

```python
def test_add_emergency_numbers_writes_one_row(seeded_db, auth_client, app):
    worker = RecordingWorker()
    app.state.worker = worker
    response = auth_client.post(
        "/number-rules/emergency",
        data={"dpid": "10", "numbers": "112\n911", "description": "Emergency"},
    )
    assert response.status_code == 303
    rows = _dialplan(seeded_db)
    assert rows == [(10, 1, "^(112|911)$", "Emergency")]
    _assert_reload(worker, "dialplan")

def test_add_blocked_prefix_writes_unanchored_end(seeded_db, auth_client):
    response = auth_client.post(
        "/number-rules/blocked",
        data={"dpid": "90", "mode": "prefix", "numbers": "0900\n0901"},
    )
    assert response.status_code == 303
    assert _dialplan(seeded_db)[0][2] == "^(0900|0901)"

def test_duplicate_rule_is_rejected(seeded_db, auth_client):
    # add once, then repeat; assert 422 and only one row
    ...

def test_custom_unreferenced_partition_warns_but_saves(seeded_db, auth_client):
    # dpid 55 unused; assert warning text and one row

def test_readonly_blocks_number_rule_writes(seeded_db, auth_client, monkeypatch):
    # settings.readonly=True; POST returns 403 and no row
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv/bin/python -m pytest tests/api/test_number_rules.py -q`
Expected: FAIL with 404/405.

- [ ] **Step 3: Implement validation and writes**

- Reject read-only POSTs with 403.
- Emergency: `dpid` must be an emergency partition; numbers digits,
  2–15 each, deduplicated; generated exact pattern.
- Blocked: exact, prefix or range; range uses `range_pattern`; custom pattern
  validates through `regex_compiles`.
- Custom: partition, priority 1–100, pattern, description; warn if the
  partition is unreferenced.
- Validate all descriptor fields via `validators.validate_field`.
- Reject duplicate `(dpid, match_exp)` with a link to the existing row.
- Insert one row inside `db.write_transaction`; enqueue `dialplan.reload`
  through `app.state.get_worker`.

- [ ] **Step 4: Render errors, warnings and saved state**

Keep the selected tab, show field errors next to inputs, show warnings above
the form, and show a success toast/line after redirect.

- [ ] **Step 5: Run tests**

Run: `.venv/bin/python -m pytest tests/api/test_number_rules.py tests/unit/test_number_rules.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add console/number_rules_ui.py web/templates/number_rules.html tests/api/test_number_rules.py
git commit -m "feat(number-rules): add simplified emergency and blocked forms"
```

---

### Task 4: Docs, full gate, deploy

**Files:**
- Modify: `CHANGELOG.md`, `docs/operations.md`
- Deploy console image

- [ ] **Step 1: Document**

Explain the three tabs, the referenced-partition rule, and that Advanced/
`/tables/dialplan` remains for edits and complex regex.

- [ ] **Step 2: Full gate**

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall console
./tools/check-privacy.sh --all
```

- [ ] **Step 3: Commit**

```bash
git add CHANGELOG.md docs/operations.md docs/superpowers
git commit -m "docs: describe the simplified number rules editor"
```

- [ ] **Step 4: Deploy and verify**

- Build and deploy the next console image.
- Verify `/number-rules` tabs, partition chips, add flows, advanced link,
  read-only behavior, and `/tables/dialplan`.
