# VoIP Edge Console — plan v2 (Kamailio edge web admin UI)

**Date:** 2026-11-05 (v2: multi-model review integrated — four-lens cycle 1: SIP engineering, architecture, QA, design)
**Mode:** plan · **Target:** `/root/voip-stack/kamailio-edge/webui/` (new) + one optional `WITH_CONSOLE` ifdef in kamailio.cfg + compose service + docs
**Companions:** `/root/voip-stack/docs/admin-console-stack-research.md`; wireframe: `/root/.opencode/plan/edge-console-wireframe.html` (v2)

---

## 0. Decisions locked with the operator (2026-11-05)

1. One-time voip-edge restart to load `jsonrpcs` is approved (WITH_CONSOLE ifdef).
2. Exposure: Pangolin resource with SSO in front; console in the same compose stack; `network_mode: host`, binds `100.64.0.9:8080`. Console token ON as a second factor regardless.
3. Wire-IP remains a manual wizard step with inline guidance.
4. Online DB backups (SQLite backup API, WAL-safe) before every edge-mutating lifecycle op (park, restore, active destroy), rotate keep-5 (global across all backups, regardless of purpose tag). Parked destroy only removes the sidecar snapshot.

## 1. Goal & non-goals

**Goal:** manage the edge without the CLI: CRUD routing data (Homes, Landlines, Route sets, Number rules, Trusted sources), live state (registered phones, probe state, edge health), reload orchestration — respecting all existing invariants; tenant lifecycle: onboard (wizard), park/restore, destroy.

**Non-goals:** no multi-user/roles; no kamailio.cfg editing; CLI stays the fallback; no schema changes to Kamailio-owned tables; sqlite only; no media/TLS management (except the wizard's SAN notice, §5.5).

## 2. Technology decisions

**Normative security spec (promoted from review ledger §12):** session = signed cookie `{sub, csrf, amr, exp}`, secret read from explicit `CONSOLE_SECRET` (never derived from `CONSOLE_TOKEN`; unset values become per-process random, which fails closed); absolute expiry 12 h, no sliding refresh; cookie flags `HttpOnly; SameSite=Lax`, `Secure` conditionally on the Pangolin TLS termination (`CONSOLE_SECURE_COOKIE=1`); login rate limit 5/min/IP; `/health` unauthenticated. CSRF = double-submit: per-session value in a non-HttpOnly cookie, sent by a global `htmx:configRequest` listener as `X-CSRF-Token` (header configured once on `body` via `hx-headers`; it lives outside swapped fragments — fragment-safe); middleware rejects unsafe methods on cookie≠header mismatch. Browser UI = session cookie; machine routes (if any) = `Authorization: Bearer` only.
**Locked:** no RPC-debug/passthrough page in v1. A read-only allowlist (`htable.dump/stats`, `dispatcher.list`, `mtree.match`, `dialplan.translate`, `core.version`) is a possible v2 follow-up — never free-form RPC.
**Typography (normative):** 14–15 px body/table text, 20–22 px H1, ≥12 px metadata, ≥32 px interactive targets, normal-case table headers, mono for SIP-shaped strings; PicoCSS default palette contrast asserted in both themes in the T2 smoke. (research-backed; full digests in the research doc)

FastAPI + Jinja2 + **htmx 2** + **PicoCSS v2**; Pydantic validators; **jsonrpcs over unix datagram** (`/run/kamailio/kamailio_rpc.sock`; binrpc/kamcmd rejected — text output + small fixed reply buffers; Siremis rejected — MySQL-only, both generations); sqlite3 stdlib (**WAL + busy_timeout=2000**, never replace the file); tests: pytest + TestClient + pytest-playwright (Python toolchain, no Node) + FakeRPC unix-dgram double + selectolax fragment assertions.

kamailio.cfg addition (the only config delta):
```
#!ifdef WITH_CONSOLE
loadmodule "jsonrpcs.so"
modparam("jsonrpcs", "dgram_socket", "/run/kamailio/kamailio_rpc.sock")
modparam("jsonrpcs", "transport", 4)          # datagram only
modparam("jsonrpcs", "dgram_mode", 0660)
modparam("jsonrpcs", "dgram_group", "console")  # shared group with console container
# (dgram_max_size does not exist in the 6.1.5 packaged module — omitted)
modparam("db_sqlite", "db_set_journal_mode", "DBFILE=WAL")     # exact path via -A
modparam("db_sqlite", "db_set_busy_timeout", "DBFILE=300")
#!endif
```
(`dgram_max_size` is NOT available in the packaged 6.1.5 `jsonrpcs.so` — verified in T8.7 and omitted; the `store_path` JSON-RPC extension remains the documented escape hatch for oversize dumps.)

## 3. Kamailio control-plane constraints (6.1.5, master-checked 2026-11-05)

1. **Latest stable == deployed == 6.1.5** (2026-10-15; no 6.2). Master-branch docs reviewed for forward-compat.
2. **Reload rate limits (corrected in review):** `reload_delta` default 5 s exists on **dialplan**, **permissions**, **and dispatcher**; mtree has **none**. Concurrent same-table reloads carry a documented crash hazard. → global serialized reload worker + per-table 5 s **coalescing throttle** (state machine §5.8). Constants measured on the lab build in T1, config-driven, never assumed.
3. Reload failures = JSON-RPC fault (500+message), old data stays active — surface verbatim; verify-after-apply round trip (`mtree.match`, `dialplan.translate` with plain strings, `dispatcher.list`).
4. `htable.dump`/`htable.delete` confirmed in docs (regcache purge primitive is real; string AOR keys, no `s:` prefix issue). Dump is bucket-shaped — flatten and list the rows; no `htable.stats` count badge in v1 (the table is small).
5. `dispatcher.list` flags: first letter A/I/T/D, second P (probed) / X (not probed). `list full`/latency stats = unverified nicety — **out of v1 scope**.
6. No dry-run RPC exists → validate pre-write in Python (regex compile — PCRE-parity caveat warning), behavioral proof post-reload.
7. sqlite: WAL (file header persists; console re-asserts `journal_mode=WAL` at every startup — `create-db.sh` rebuilds revert the file to DELETE mode), in-place edits only, `BEGIN IMMEDIATE` short transactions, single-writer.
8. **regcache expiry (fixed):** `regcache=>size=12;autoexpire=7200;` and `$shtex(regcache=>$avp(reg_aor)) = $avp(reg_exp) + CACHEGRACE` in the REGISTER 200 path. Entries expire at the registration TTL, on unregister, or on park purge; the 6.1.5 dump does not expose remaining time, so the live panel shows no countdown.

## 4. Architecture

```
Arcane stack (host networking):
 voip-edge (kamailio) ── jsonrpcs dgram sock (/run/kamailio/kamailio_rpc.sock, group console)
      │ reads+reloads edge.sqlite (WAL)
 voip-edge-console ── sqlite3 rw on edge.sqlite · rw on run dir (datagram client)
      FastAPI/uvicorn (workers=1), Jinja2, htmx, PicoCSS
      bind 100.64.0.9:8080 · Pangolin SSO in front · token second factor
```

Modules (`webui/console/`):
- `settings.py` — **all** config via pydantic-settings env vars (single table in §10): token/secret, bind, db path, sidecar dir, socket path, RPC timeout, reload throttle map, allowed domain suffixes, poll interval, readonly mode.
- `descriptors.py` — loads `tables.yaml` (grammar §5.1), startup consistency check vs live DB.
- `db.py` — connection-per-request factory (WAL+bsy per conn); pack/unpack pure functions; content-hash snapshots; transaction-scope `threading.Lock` (writes only; reads never lock).
- `rpc.py` — datagram JSON-RPC client (timeout, id sequence, typed fault mapping).
- `reloads.py` — **dedicated background thread** consuming a `queue.Queue`: owns serialization, per-table throttle clock, fault capture, and pending-intent persistence (§5.6). Endpoints only `put()`.
- `lifecycle.py` — park/restore/destroy state machines (§5.3).
- `backups.py` — online sqlite backup (backup API), rotation (keep-5, global), purpose tags; also dumps `console_parked` rows into each backup (parked tenants must not die with a sidecar loss).
- `audit.py` + sidecar ownership: `sidecar.py` owns `console.sqlite` — idempotent `CREATE TABLE IF NOT EXISTS` + `user_version` pragma migrations at startup (bump + function per change; no external tool).
- `web/` — routes + templates. **Invariant:** zero table-specific logic in `web/`; everything table-shaped is expressible in the descriptor.

**Error taxonomy (exception → HTTP → surface):** `ValidationError` → 422 → inline form errors; `ConflictError` → 409 → banner with Keep-mine/Load-latest; `KamailioUnavailable` → pending-reload intent + persistent banner; `SidecarUnavailable` → read-only banner + destructive ops refused; `DBBusy` → retry indicator; `ReloadError` (RPC fault) → toast naming the failed table + verbatim message. Uniform FastAPI exception handlers; UI routes return only HTML; JSON routes never HTML; no `response_model` on UI routes.

**Concurrency (final):** sync `def` endpoints in the threadpool; no asyncio in the request path; writes hold the module-level `threading.Lock` per transaction; reload/RPC background thread never touches edge.sqlite.

## 5. Data model & behaviors

### 5.1 tables.yaml grammar v1 (closed spec — no scripting in YAML)

```
descriptor:
  table: name
  storage: packed | columns          # packed: key+comma-tvalue; columns: real cols, auto id PK
  key: column                        # packed tables only; create-only (rename = park + re-onboard)
  fields: [{ name, column | pack_position, type, validator, severity: error|warn,
             help, widget: text|number|checkbox|select|textarea,
             readonly: bool, locked: bool, derived: rule-name, options_query: table.field,
             show_in_list: bool, form_order: int }]
  reload: { rpc: name, params: [...], throttle_seconds: n }   # per descriptor
  delete_policy: blocked_if_refs | lifecycle             # tenants = lifecycle only (§5.3)
  cross_refs: [{ field, ref: table.field, on_missing: error|warn }]
version: 1
```
Types (closed set): `str, int, enum01, ipv4, sip_uri, regex, digits, domain, label`. Validators: **named Python callables** referenced by string — never inline regex in YAML. Field order = list/form order defaults. At startup: `PRAGMA table_info` each declared table + check the `version` bookkeeping rows (dispatcher=4, dialplan=2, trusted=6) — mismatch ⇒ banner + writes refused for that table.

### 5.2 Validation matrix (rule → test traceability via §9 matrix; severities: **error** blocks save, **warn** saves with badge)

Global: packed free-text fields reject `,`/`+`/whitespace-pads; keys are **create-only** (tenant rename = park + re-onboard); every write runs cross-refs; rollback on failure.

- **tenant_registry** — tprefix: domain chars (letters/digits/dot/dash — must also be within mtree char_list), allowed suffix list, unique; tenantid `[A-Z0-9]+` unique; ds_onnet must exist in dispatcher (**error**) — convention-nudge 10+N as warn; ds_pstn exists or 0 (**error**); cli digits, exact-collision with another tenant's landline (**error**), prefix-overlap (**warn**); label no `,`/`+`; dpid must exist in dialplan (**error**); flags `ih_on+pstn_on` ∈{0,1} (+ block_dpid int that exists in dialplan — **error**; absent allowed). Delete action hidden — lifecycle only (§5.5).
- **tenant_landline** — digits ≥3; longest-prefix conflict: cross-tenant shadowing (**error**, interruption page naming both rows), same-tenant shadowing allowed with explicit checkbox; tenantid must exist (**error**); ds_onnet **derived** read-only from tenant; target identifier chars; ih_on enum01.
- **dispatcher** — destination sip_uri: **literal IPv4 host required** (FQDN rejected with hint "use the voip-stack host's LAN IP"), port int, `transport=` mandatory ∈ tcp/udp (default tcp); **warn if udp**: text must say *"OPTIONS pings stay green while INVITEs fragment and die in the tunnel"*; never infer REGISTER behavior from this param (relay forces UDP anyway); setid referenced by tenant ⇒ info badge; delete of referenced set ⇒ **error** naming tenants.
- **dialplan** — match_exp compiles in Python re (**error**); PCRE-only constructs ⇒ **warn** "approximate check"; match_op locked 1; subst/repl locked identity (display: "match-only by design"); post-reload behavioral proof: optional sample → `dialplan.translate` result in toast. Presets: "country profile" and "block partition" (operator-chosen partition ids; no patterns are seeded).
- **trusted** — src_ip ipv4; non-CGNAT/private ⇒ **error with explicit override** (documented generic non-tunnel path must stay possible); utility-alias-shaped (100.96.128.0/24 heuristic) ⇒ **warn** "use the wire-observed native IP"; proto locked `any`; tag = select from existing tenant domains (**options_query**); duplicate src_ip across tags ⇒ **error**.

**Coherence check** (DB-only on list page; RPC-checked on tenant detail — no N+1 polling): registry ⇄ sets exist ⇄ dpid exists ⇄ trusted tag exists ⇄ landline target → badge green / amber named-missing on list; full checklist panel on detail.

### 5.3 Tenant lifecycle (park/restore/destroy)

**Park sequence (order reviewed & corrected):** intent STARTED → BACKED_UP → SNAPSHOTTED (JSON into `console_parked`, same console.sqlite txn as intent row) → ROWS_DELETED → **RELOADED (batch: tenantdom + landline + dispatcher + trusted — mtree tree-args single-form)** → PURGED (dump regcache → `htable.delete` every key ending `@<domain>` → dump-verify zero, bounded retry) → DONE. (Reload **before** purge: new registrations of the domain must be rejected at the mtree level before the purge runs — closes the re-register race. A REGISTER in flight during the mtree reload is the only residue; it ages out at its autoexpire TTL, with no delayed re-sweep.) Sidecar unreadable ⇒ refuse before touching edge.sqlite. Crash ⇒ sweeper (startup + every 30 s) resumes oldest non-DONE intent through the same orchestrator; a failed step records `last_error` and the sweeper retries (Audit shows it; no Retry button).

**Restore:** BACKED_UP → COLLISION_CHECKED (domain / tenantid / landline digits / set ids / src_ip — each collision class blocks, naming the colliding row) → ROWS_INSERTED (guarded INSERT, never OR REPLACE) → RELOADED → coherence green → DONE.

**Destroy:** active tenant ⇒ same sequence as park (incl. regcache purge) but **typed confirmation** (type the full domain) + snapshot discarded on success; parked ⇒ normal consequence-naming confirm, deletes snapshot only. Shared dialplan dpids never deleted; info line if the dpid is now unreferenced. List UI: Status badge (Active/Parked textual+colored), Parked section separate columns (Domain, Parked on, Landline, Snapshot size, Actions Restore/Destroy).

### 5.4 Lifecycle copy nuance (acceptance text): park/destroy notices say "calls already in progress finish; new requests get 403/404" — verified **manually in the lab** (call up, park, call completes), not by console tests.

### 5.5 Onboarding wizard (flagship)

Steps: 1 Identity (domain, landline, ring group, label) → 2 Network (voip-stack LAN IP; wire-observed native IP with inline one-liner; TLS-optional) → 3 Review & apply (a tagged online backup is taken first; consequences panel; writes **5 rows**: registry, landline, 2× dispatcher, trusted — 4 without PSTN) → 4 Verify (coherence checklist + optional "watch for a registration" live pane). Rollback on any failure with summary. Validation errors inside wizard: summary focused, links to steps/fields, values preserved, stepper position kept. **If WITH_TLS is live (env `CONSOLE_TLS_LIVE=1`):** review screen carries a mandatory-step notice — append `sip.<c>.home.example.net` to `ACME_DOMAINS` + one-shot force-reissue (README onboarding step 4), because the wildcard does not cover two-label tenant domains. ds_pstn=0 path = 4 rows, PSTN set skipped, noted in review.

## 5.6 Pending reloads (persistence): reload intents (`table,enqueued_at`) live in `console.sqlite`; the RPC + params (e.g. the mtree tree name) are re-derived from the descriptor at sweep time — table key IS the tree name for packed tables; the sweeper re-issues them (the capability probe is advisory and runs after); a console restart while Kamailio is down loses nothing.

## 5.7 Durable intent state machines: as §5.3 (states enumerated per op; every transition an idempotent step; intent id = uuid idempotency key; fault-injection test points: between every consecutive state pair).

## 5.8 Reload throttle state machine: per table — writes land in DB immediately; reload **enqueued**; the throttle timer starts at the **first** save in a window; later saves coalesce but do NOT extend the timer; the worker fires at t≥measured delta (N writes in window ⇒ exactly 1 RPC per table). Deferred ⇒ toast "reload scheduled shortly"; fault ⇒ toast with verbatim Kamailio message; verify mismatch ⇒ warning banner.

## 6. Feedback vocabulary & status surfaces (design contract)

- Reload outcomes vocabulary: **scheduled / applied / verified / failed** — toasts name what: "Saved beta — reloaded & verified: homes, route sets." No naked "Success".
- **Global status region** (every page, above content): edge health (core.uptime — "edge reachable · uptime 6 d" vs "edge unreachable"), pending-reload count, read-only mode, sidecar failure, DB-busy retry, capability mismatch. Auto-refreshing fragments preserve scroll/focus; no per-refresh announcements (aria-live only on change).
- Badge tokens normalized: dispatcher A=green, T/I=amber, D=red; probe suffix "· 38 ms" only when measured; "TX · no answer" is wrong wording → "last probe: no answer". Health shown in the status strip, not as a badge row.
- Copy: primary noun is **Home** (tenant only in help/audit); dialplan jargon (`dpid`, `1+1+90`) never in primary labels — help text instead. Button labels action-shaped ("Apply and reload routing"), DB mechanics live in the consequence panel.
- Empty states: first-use CTA → wizard; filtered-empty → "No results match + Clear filters".
- Forms: labels above, one-line schema-derived help under every field, errors imperative ("Choose an existing PSTN route set, or create set 99 first") + focused summary (tabindex=−1) + inline `aria-invalid`/`aria-describedby`; one label per checkbox; never placeholder-as-label; values preserved on error; key fields disabled on edit ("create-only" hint text).
- Dialogs: native `<dialog>`/aria-modal, named object+consequence, focus trap, Esc, focus return, default focus Cancel; Park = single warning-styled action (not Cancel-as-primary); Destroy-active = typed confirm with explicit label + keyboard submit; Destroy-parked = normal confirm.
- a11y contract: focus→page heading on navigation, fragment swaps preserve focus unless context changed, toasts `role=status aria-live=polite` (reload faults `role=alert`), `aria-current=page|step`, visible focus styling; PicoCSS default palette contrast is asserted in both light/dark in the T2 smoke.

## 7. Scenarios & edge cases (acceptance-critical; uniquely numbered)

1. Onboard wizard happy path (5 rows, one txn, batch reload, green checklist); no-PSTN variant (4 rows).
2. Wizard fault-injection: forced failure at **each** insert in turn ⇒ zero rows remain.
3. Stale edit/two tabs: content-hash 409 banner, Keep-mine/Load-latest.
4. Kamailio down: writes allowed ⇒ **pending-reload intents persist in sidecar**, banner until cleared; health badge in global status region.
5. DB locked: `locked_db` fixture ⇒ retry indicator shown; lock released ⇒ write succeeds.
6. CLI edits the DB meanwhile: list views re-read every load; audit page states "console-recorded writes only; external edits possible".
7. Regex syntax ⇒ pre-write error; PCRE-only ⇒ warning badge.
8. In-use delete ⇒ named-by consequence dialog, blocked.
9. regcache live panel: AOR/flow/transport (no countdown; the dump has no expiry field); list the dumped rows, no stats badge in v1.
10. Registered-phones panel on tenant detail (fragment) — covered.
11. Park mid-call: **lab-verified manually** (not console-tested).
12. Park: purge gate = dump-verify zero keys (bounded retries); a residual in-flight REGISTER is accepted (no delayed re-sweep is scheduled).
13. Restore collision: 5 classes tested.
14. Sidecar unreadable: destructive ops refused; browsing unaffected.
15. Crash between park steps: sweeper resume; fault-injected between every state pair.
16. Backup rotation enforced incl. after failed ops; backups listed in Audit page with purpose tags.
17. Domain `tprefix` charset constraint (mtree char_list alignment).
18. udp destination on tunnel ⇒ warn with MTU text; validator never infers REGISTER transport from destination transport.
19. trusted non-CGNAT override path documented in help text.
20. htmx fragment safety: every route asserts full-page vs fragment (crawl test).

## 8. Task breakdown — TDD outer→inner (estimates reviewed, honest)

| T | Task | Key Red focus | Est |
|---|---|---|---|
| T0 | Descriptor engine + pack/unpack | **golden vectors** (literal tvalue strings from synthetic seeds, both directions; absent block_dpid segment case) | 0.5 d |
| T1 | RPC client + reload worker + capability probes | FakeRPC dgram fake: timeouts, faults, bucket flatten; injected-clock throttle test (N saves ⇒ 1 RPC/table at t≥delta); kamailio-down typed error; **scheduled lab verifications**: htable.delete signature, measured reload_delta per table, dgram 256 KB vs kernel wmem | 1 d |
| T2 | App skeleton + host-lab harness | boot: token gate, CSRF plumbing (double-submit, hx-headers on body), health page, layout (PicoCSS + vendored assets); host-based lab (run-lab.sh-style: real kamailio+console on loopback, tmp DB); WAL-coexistence assertion (kamailio loaded + console writes in place + reload reflected, no "database is locked" in log); compose smoke only for packaging | 2 d |
| T3 | Read-only table browser (generic proof) | all 5 tables browsable with zero per-table code; empty states; fragment-vs-full assertions | 1 d |
| T4 | Create/edit + validation (the meat) | §5.2 matrix implemented; **rule→test matrix doc is a deliverable**; per-scenario reds incl. warnings-save path, landline interruption page, wizard-less flows | 3 d |
| T5 | Row delete + guards | named-by dialog; blocked; hx-delete row removal | 0.5 d |
| T6 | Live dashboard + detail fragments | flag mapping corpus; polling fragment; tenant-detail phones fragment; health in status strip | 1 d |
| T7 | Onboarding wizard | E2E apply + green checklist; per-insert fault injection; TLS-SAN notice when CONSOLE_TLS_LIVE | 1.5 d |
| T8 | Lifecycle park/restore/destroy | state machines, fault-injection resume tests between every consecutive state pair, regcache purge logic (unit) **+ ONE real-kamailio lab journey** (register uac phone → park → 403/404, pinging stops counted from fake logs, restore → green); backup rotation; typed-confirm Playwright journey | 3 d |
| T9 | Hardening + observability | CSRF tests, login rate limit, READONLY mode (all mutating routes gated + buttons disabled + banner — it is a cross-cutting feature, not a one-liner), capability probes surfaced, structured logs + audit, throttle burst test | 1.5 d |
| T10 | Packaging + docs | compose service (host net, rw run-dir mount, `${EDGE_HOME}/console/` sidecar mount, group story for dgram socket, mem/cpu limits, restart policy, healthcheck = console liveness only, kamailio state is body-field), **AGENTS.md created**, README section, DEPLOY pointer, schema.md convention fix (stale `voip.<c>.internal` story), env-var table, backup-restore runbook, Pangolin SSO resource steps | 1 d |

**Total ≈ 16 working days.** 
### Fine-grained TDD cards for the two multi-day tasks

**T4 sub-cards** (each: scenario Red → Green → next):
1. Tenant create happy path (write + throttle enqueued + toast names reloaded tables)
2. Key/suffix/uniqueness family (domain charset/mtree char_list, allowed suffixes, tenantid, key-immutability on edit)
3. Cross-ref errors (missing set / missing dpid / block_dpid-not-in-dialplan; inline+summary, values preserved)
4. Landline longest-prefix (cross-tenant block page; same-tenant checkbox allow)
5. Dispatcher URI family (FQDN reject w/ LAN-IP hint; udp warn with MTU text; setid free-info; in-use delete guard)
6. Trusted IP family (non-CGNAT overridable error; alias-shape warn; duplicate src_ip error; tag = options_query)
7. Dialplan (regex compile error; PCRE-parity warn; locked identity subst/repl; presets; post-save translate proof)
8. Stale-edit 409 (Keep-mine / Load-latest)

**T8 sub-cards:**
1. Snapshot completeness (4 tables, JSON fixture both directions)
2. Transactional removal + backup-before + rotation
3. Park reload order (reload-first invariant) + purge + dump-verify zero gate (bounded retries)
4. Durable-intent resume — fault injection between every state pair
5. Restore + 5 collision classes
6. Destroy gates (typed confirm for active; normal confirm when parked) + shared-dpid survival
7. Real-Kamailio lab journey (uac phone → park → REGISTER 403 + INVITE 404 → OPTIONS ping count drops to zero within settle window → restore → coherence green)

### Test surface (QA-approved layout)
```
webui/tests/
  unit/    test_descriptors, test_pack (golden vectors), test_validators_<5 tables>,
           test_rpc, test_reloads (injected clock), test_lifecycle, test_backups, test_audit
  api/     test_auth_csrf, test_routes_crawl (route × {plain, HX-Request} doctype asserts),
           test_tables_<5>, test_forms_<5>, test_live_fragments, test_lifecycle_api
  live/    test_lab_reload_all_tables, test_lab_park_purge, test_lab_wizard_calls_work
  e2e/     pytest-playwright journeys (10, named below)
  fixtures/ golden tvalue vectors, canned RPC dumps, flag-mapping corpus
```
Journeys (exactly these): smoke-auth-nav · create-tenant-happy · create-tenant-validation-errors · landline-prefix-interruption · stale-edit-409 · delete-blocked-consequence · wizard-apply · park-restore (with lab REGISTER→403) · destroy-typed-confirm (button gating) · kamailio-down-banner. Everything else = TestClient/selectolax. Flake rules: injected clocks only; `CONSOLE_POLL_MS` in the lab (500 ms; no `wait_for_timeout`); settle-window ≥2× ping interval for absence assertions; per-test tmp DB copies; journeys never parallel.

## 9. Rule→test traceability

T4 started `docs/validation-matrix.md`: rules have ids (e.g. `V-REG-CLI-COLLIDE`) and current test/owner links, but several rows remain explicit `TODO` gaps (notably coherence/live checks). Completing the matrix is tracked as a follow-up rather than claimed green. §7 scenario → owning task is listed above.

## 10. Env/config table, packaging, docs, rollout

Env (single surface, defaults sane): `CONSOLE_TOKEN`, `CONSOLE_SECRET` (unset/blank → random per process; never derived), `CONSOLE_BIND=100.64.0.9`, `CONSOLE_PORT=8080`, `CONSOLE_DB_PATH`, `CONSOLE_SIDECAR_DIR=${EDGE_HOME}/console`, `CONSOLE_RPC_SOCK=/run/kamailio/kamailio_rpc.sock`, `CONSOLE_RPC_TIMEOUT_MS=2000`, `CONSOLE_THROTTLE_TENANTDOM=…` (per-table map, measured), `CONSOLE_ALLOWED_SUFFIXES=home.example.net`, `CONSOLE_POLL_MS=15000`, `CONSOLE_READONLY=0`, `CONSOLE_TLS_LIVE=1`.

Compose service: `network_mode: host`; volumes `db/` rw, `${EDGE_RUN}` rw, `${EDGE_HOME}/console` rw; group story: jsonrpcs `dgram_group` = a group the console container runs with (or console root + documented note); tables.yaml baked into image (pinned with code); vendored htmx/PicoCSS (no runtime fetch); `mem_limit: 256m, cpus: 0.5`; `restart: unless-stopped`; **healthcheck tests the console process only** (`/health` 200 even when Kamailio is down — its status is a body field, never the exit code; otherwise Docker restart-loops the console exactly when the operator needs the banner).

Rollout: (1) lab; (2) voip-edge redeploy with WITH_CONSOLE (approved blip), console container up, READONLY=1 first session, Pangolin SSO resource; (3) writes on; onboard beta/gamma through the wizard = acceptance of record.

## 11. Parallel execution plan (subagent lanes)

**Dependency matrix (TDD order respected):**

| Task | Depends on | Blocks |
|---|---|---|
| T0 descriptors/pack | — | T3, T4, T7, T8 |
| T1 rpc/reloads/probes | — | T6, T7, T8, T9 |
| T2 skeleton+harness+auth | — | all UI lanes (T3–T8) |
| T3 read browser | T0, T2 | T4 |
| T4.1 tenant happy path | T3 (+T1 throttle) | T4.2–T4.8 (pattern) |
| T4.2–T4.8 families | T4.1 | T7 (validators reused), T9 |
| T5 delete guards | T3 (+T4 pattern) | — |
| T6 live dashboard | T1, T2 | (tenant-detail fragment feeds T4 detail page) |
| T7 wizard | T4 complete, T1 | acceptance of record |
| T8.1–T8.6 lifecycle units | T0, T1, T2 (backups, sidecar) | T8.7, T9 |
| T8.7 lab journey | T8 units done, singleton lab | gate for T9 |
| T9 hardening/obs | A+B lanes landed | T10 |
| T10 packaging/docs | T2 (compose), everything else | rollout |

**Phase plan:**

- **Phase 0 — contract freeze (3 parallel lanes, ~2 d):** T0 ∥ T1 ∥ T2. Freeze review before anyone continues: descriptor grammar signatures, `rpc.py`/`reloads.py` signatures, sidecar schema (`sidecar.py` v1), `settings.py` env list, template base, conftest fixtures. Nobody codes past this without the freeze.
- **Phase 1 (3 lanes):**
  - **A** (data editing): T3 → T4.1 solo; then T4.2–T4.8 fan out — one table family per agent (max 3–4 parallel), T5 after T4.1.
  - **B** (runtime): T6 → T8.1–T8.6 (lifecycle is cohesive — one agent writes `lifecycle.py`, others only its tests if staffing allows).
  - **C** (docs): T10 docs half (README/AGENTS.md/schema.md fix + validation-matrix scaffold).
- **Phase 2:** T7 (needs T4 complete), T8.7 runs alone (singleton lab), then T9.
- **Phase 3 — integration & acceptance:** merges in lane order A→B→C with an integration pass after each; rollout.

**Ownership matrix (shared files, one owner per phase):** `web/routes.py` lane A; `templates/base.html` lane A (B uses `web/live.py` + own templates); `tables.yaml` lane A; `conftest.py`/fixtures phase-0 owner then append-only; `lifecycle.py`,`backups.py` lane B; `settings.py` phase-0 owner (append-only after freeze); compose/docs lane C.

**Hard rules:** one worktree+branch per lane, merged A→B→C after each phase; fake/unit/api tests parallel-safe (tmp DB copies, injected clocks); **live + e2e lab is a singleton** — a file lock in the harness serializes it across agents; nobody "temporarily implements" another lane's interface.

**Effect:** sequential ≈16 d → parallel ≈ **9 calendar days** (3 lanes + ~1.5 d integration), assuming agents available. Diminishing returns beyond 4 lanes — shared-file churn dominates.

## 12. Senior-review ledger (cycle 1 resolutions)

**SIP lens:** regcache-no-autoexpire bug surfaced → §3.8 + v1 panel drops countdown + follow-up config fix logged; purge AFTER reload (order corrected, race documented); reload_delta correction (dispatcher too, mtree none); dispatcher udp warning text locked; wizard = 5 rows; trusted CGNAT rule → overridable; TLS-SAN mandatory wizard notice; domain tprefix charset; WAL re-assert on startup + docs; 262144-byte datagram lab-verified; dialplan.translate plain strings; binrpc-cap claim uncited.

**Architecture lens:** added settings/lifecycle/backups/sidecar modules; error taxonomy table; queue-thread reload model + workers=1 hard constraint + transaction-scope write lock + conn-per-request; durable-intent state machine spec; pending-reload persistence; backup rotation spec (+console_parked inside backups); capability startup probe; CSRF double-submit mechanics; session cookie spec (12 h absolute, SameSite=Lax, conditional Secure, 5/min/IP login limit); RPC-debug = read-only allowlist decision; HTML-only UI routes rule; host-networking bind decision; healthcheck semantics; env table; obs in T9; readonly spec'd; estimates corrected.

**QA lens:** golden vectors; coalescing-throttle state machine with injected clock; pytest-playwright (no Node); kamailio-down + locked_db + fault-injection owners; 11b demoted to manual lab check; dump-verify unit-vs-lab split; toast wording aligned; rule→test matrix; list-level coherence = DB-only decision; key-immutability rule+test; detail phones fragment test; typed-confirm journey; uptime=core.uptime; latency out (wireframe adjusted); §6 renumbered; host-harness over compose; flake prescriptions; journey list fixed; route-crawl test; "cheap" claims corrected.

**Design lens:** state-inventory → surfaces (§6 status region now specified); audit/backups page spec'd; lifecycle matrix corrected (parked destroy = normal confirm; active destroy = typed; restore states added); wizard full-state set; imperative error wording; action hierarchy fixed (Park as warning action, Edit primary on detail); feedback vocabulary + normalized badge tokens; IA restructure (Homes landing — Overview dropped; Live second; Routing-data group; Audit & backups last; "Route sets" name); typography spec (14–15 px body, 20–22 px H1, ≥12 px meta, ≥32 px targets, normal-case headers); real thead/tbody + parked-table columns + full identifiers; a11y contract; "Home" as primary noun; prototype chrome excluded from acceptance. → Wireframe updated to v2 accordingly (see companion HTML).

**Cycle 2 (verification):** run an independent resolution audit against this v2 before build starts.
