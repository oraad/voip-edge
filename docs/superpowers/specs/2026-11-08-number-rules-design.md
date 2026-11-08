# Number rules simplified editor — design

Date: 2026-11-08
Status: draft for approval
Scope: Number rules page, simplified add/edit, advanced table retained

## Intent

Operators need to add emergency numbers, blocked numbers/prefixes and occasional
custom rules without learning Kamailio dialplan regex. The current table stays
for advanced setup and raw edits.

## What the edge actually does

- A home's `dpid` names its **emergency** partition. A match routes the call to
  that home's PSTN set.
- The third `flags` segment names its **blocked destinations** partition. A
  match returns 403 before any trunk decision.
- No other dialplan partition is consulted. Rows in an unreferenced partition
  are stored but have no effect.

The simplified UI must show partition role and warn when a partition is inert.

## Goals

- Three clear tasks: Emergency, Blocked, Custom.
- Dropdowns for partition scope, not raw partition IDs.
- Generate safe regex from simple inputs; preview before saving.
- Keep the full table at `/tables/dialplan` for advanced rows and raw edits.
- Reuse existing validation, write transaction and reload worker.

## Non-goals

- A general regex builder.
- Creating or assigning partitions to homes; link to the home editor instead.
- Editing existing rules in the simple editor; Advanced owns that.
- Changing Kamailio routing semantics.

## Approaches considered

1. **Three tabs on `/number-rules` (chosen).** Emergency, Blocked, Custom each
   have their own form and rule list. Clear mental model; no type dropdown.
2. Single add panel with a type selector. Less navigation, but the form changes
   shape and the mixed list is harder to scan.
3. Guided wizard. Extra steps for a one-row operation; rejected.

## Page layout

`GET /number-rules`

- Header: **Number rules**, short purpose line, **Advanced table** link.
- Explanatory callout: only referenced partitions affect calls.
- Partition summary chips: `Partition 10 · Emergency for Home A + Home B ·
  1 rule`, `Partition 90 · Blocked for Home A · 0 rules`.
- Tabs: **Emergency**, **Blocked**, **Custom**.
- Each tab: add form first, then its rules grouped by partition.

### Emergency tab

- **Applies to**: select of emergency partitions, labelled with the homes that
  use them. No free partition id in the simple form.
- **Emergency numbers**: textarea, one per line or comma-separated.
- **Description**: optional, defaults to `Emergency numbers`.
- Preview: generated `^(112|911|999)$` and a plain-language match summary.
- Save creates one row: `match_op=1`, `match_len=0`, identity substitution,
  selected `dpid`, `pr=1`.
- Existing emergency rules listed as readable numbers, not regex.

### Blocked tab

- **Applies to**: select of blocked-destination partitions.
- **Match**: segmented options:
  - **Exact numbers** — `90001234, 90005678` → `^(90001234|90005678)$`.
  - **Starting with** — `0900, 0901` → `^(0900|0901)`.
  - **Number range** — start and end, same length; generated regex.
  - **Custom pattern** — regex field, with a link to Advanced.
- **Description**.
- Preview: pattern, sample matches and one non-match.
- Range generation is a pure helper with tests; unsupported or oversized
  ranges show an error and suggest starting-with or Advanced.

### Custom tab

- **Partition**: all partitions, labelled `Emergency`, `Blocked`, or `Unused`.
- **Priority**: 1–100.
- **Match pattern**: regex.
- **Description**.
- If the partition is unreferenced, a warning: "This partition is not assigned
  to a home, so this rule will not affect calls."
- Complex existing rules remain editable here only through Advanced.

### Existing rules

- Each row shows readable label, scope, partition, priority and description.
- **Edit** opens `/tables/dialplan/<id>/edit`; the advanced table is the
  editing surface for existing rules.
- **Delete** uses the existing confirmation page.
- Empty states name the next action, e.g. "No emergency numbers yet."

## Data flow

- New `console/number_rules_ui.py` owns the pages.
- A small shared write helper extracts the create path from `forms_ui`:
  validate descriptor fields, run family rules, insert under
  `db.write_transaction`, enqueue `dialplan.reload`, return errors/warnings.
- Generated rows use the existing `dialplan` descriptor; no grammar change.
- Read-only mode disables every create/edit/delete action.

## Validation

- Emergency numbers: digits only, at least 2 and at most 15, deduplicated.
- Prefixes: digits only, same limits.
- Range: equal length, start ≤ end, bounded entry count; result must compile.
- Custom: existing regex validator; unreferenced partition warns, never blocks.
- Duplicate `(dpid, match_exp)` is an error with a link to the existing rule.
- Generated regex is length-capped to keep the form and Kamailio responsive.

## Files

- Create `console/number_rules_ui.py`, `web/templates/number_rules.html`,
  `web/templates/number_rules_form.html`.
- Add a shared create helper in `console/forms_ui.py` or `console/writes.py`.
- Modify navigation to point **Number rules** at `/number-rules`; keep
  `/tables/dialplan` linked as Advanced.
- Tests: pattern/range helpers, page/tabs, all three create flows, validation,
  read-only, advanced fallback, reload enqueue.

## Calls made

- Tabs over a type dropdown: matches three distinct tasks and keeps each form
  short.
- The simple editor adds rules; existing rules are edited in Advanced.
- Emergency and blocked forms select referenced partitions, not raw IDs.
- Custom partitions stay available but warn when inert.
- No automatic creation of partitions or home assignments in v1.
