# Validation matrix (plan §5.2 → §9)

Status: **scaffold**. T4 owns this document's completion. `TODO` means the test
does not exist yet; test names shown are the current traceability, not a claim
that the whole rule is covered.

How to read this:

- **error** blocks the save; **warn** saves and shows a badge; **info** is a
  non-blocking surface; **invariant** is a structural rule with no severity.
- **Unit test** is the validator/codec-level assertion.
- **Form test** is the API/HTML-level assertion (inline error, focused summary,
  preserved values, warning badge, rollback).
- Owner is the T4 sub-card that lands the rule. A few §5.2 clauses belong to
  T5/T6/T8 because they cover delete guards, coherence or lifecycle-only
  behaviour; they stay here so the matrix is complete.

## Global and cross-table

| Rule id | Rule | Severity | Unit test | Form test | Owner |
|---|---|---|---|---|---|
| `V-GLOBAL-PACK-SEPARATORS` | Packed free-text fields reject `,` and `+` (comma is the field separator, plus is the flags separator) | error | `tests/unit/test_pack.py::test_pack_fields_rejects_comma_in_value`, `::test_pack_fields_rejects_plus_in_value`; `tests/unit/test_descriptors.py::test_pack_packed_row_rejects_separators` | TODO | T4.1 |
| `V-GLOBAL-PACK-WHITESPACE` | Packed free-text fields reject leading/trailing whitespace padding | error | TODO | TODO | T4.1 |
| `V-GLOBAL-KEY-CREATE-ONLY` | Keys are create-only; a home rename is park + re-onboard, never an in-place key edit | error | TODO | TODO | T4.2 |
| `V-GLOBAL-CROSS-REFS` | Every write runs the descriptor cross-refs before commit; `on_missing` honour `error`/`warn` | error | TODO | TODO | T4.3 |
| `V-GLOBAL-WRITE-ROLLBACK` | A failed validation or write rolls back the whole transaction; no partial row set | error | TODO | TODO | T4.1 |
| `V-GLOBAL-COHERENCE-LIST` | List page coherence is DB-only: registry ⇄ sets exist ⇄ dpid exists ⇄ trusted tag exists ⇄ landline target; amber badge names the missing piece | info | TODO | TODO | T6 |
| `V-GLOBAL-COHERENCE-DETAIL` | Tenant detail shows the full checklist, RPC-checked, without N+1 polling | info | TODO | TODO | T4.8 / T6 |

## tenant_registry

| Rule id | Rule | Severity | Unit test | Form test | Owner |
|---|---|---|---|---|---|
| `V-REG-DOMAIN-CHARSET` | `tprefix` uses only letters, digits, dot and dash, and stays inside the mtree `char_list` | error | TODO | TODO | T4.2 |
| `V-REG-DOMAIN-SUFFIX` | `tprefix` ends in one of `CONSOLE_ALLOWED_SUFFIXES` | error | TODO | TODO | T4.2 |
| `V-REG-DOMAIN-UNIQUE` | `tprefix` is unique across the registry | error | TODO | TODO | T4.2 |
| `V-REG-TENANTID-FORMAT` | `tenantid` matches `[A-Z0-9]+` | error | TODO | TODO | T4.2 |
| `V-REG-TENANTID-UNIQUE` | `tenantid` is unique across homes | error | TODO | TODO | T4.2 |
| `V-REG-DS-ONNET-EXISTS` | `ds_onnet` exists in `dispatcher.setid` | error | TODO | TODO | T4.3 |
| `V-REG-DS-ONNET-CONVENTION` | `ds_onnet` outside the 10+N convention is warned, not blocked | warn | TODO | TODO | T4.3 |
| `V-REG-DS-PSTN-EXISTS-OR-ZERO` | `ds_pstn` exists in `dispatcher.setid`, or is `0` for a home with no trunk | error | TODO | TODO | T4.3 |
| `V-REG-CLI-DIGITS` | `cli` is digits only | error | TODO | TODO | T4.2 |
| `V-REG-CLI-COLLIDE` | `cli` exactly collides with another home's landline | error | TODO | TODO | T4.4 |
| `V-REG-CLI-PREFIX-OVERLAP` | `cli` prefix-overlaps another home's landline | warn | TODO | TODO | T4.4 |
| `V-REG-LABEL-PACK-SAFE` | `label` contains no `,` or `+` | error | `tests/unit/test_descriptors.py::test_pack_packed_row_rejects_separators` (comma label; validator-level test TODO) | TODO | T4.1 |
| `V-REG-DPID-EXISTS` | `dpid` exists in `dialplan.dpid` | error | TODO | TODO | T4.3 |
| `V-REG-FLAGS-BITS` | `flags` carries `ih_on` + `pstn_on`, each exactly `0` or `1` | error | `tests/unit/test_pack.py::test_parse_flags_rejects_malformed`, `::test_flags_round_trip` (codec only; validator-level test TODO) | TODO | T4.1 |
| `V-REG-BLOCK-DPID-EXISTS` | Optional third flags segment `block_dpid` is an integer that exists in `dialplan.dpid`; absent is allowed | error | TODO | TODO | T4.3 |
| `V-REG-DELETE-LIFECYCLE` | Registry rows have no Delete action; removal is lifecycle-only | invariant | `tests/unit/test_descriptors.py::test_storage_keys_and_reloads` (delete policy only) | TODO | T5 / T8.1 |

## tenant_landline

| Rule id | Rule | Severity | Unit test | Form test | Owner |
|---|---|---|---|---|---|
| `V-LAND-DIGITS-MIN3` | `tprefix` is digits with at least 3 characters | error | TODO | TODO | T4.4 |
| `V-LAND-PREFIX-CROSS-TENANT` | Longest-prefix cross-tenant shadowing is blocked; the interruption page names both rows | error | TODO | TODO | T4.4 |
| `V-LAND-PREFIX-SAME-TENANT` | Same-tenant shadowing is allowed only with the explicit confirmation checkbox | error | TODO | TODO | T4.4 |
| `V-LAND-TENANT-EXISTS` | `tenantid` exists in `tenant_registry` | error | TODO | TODO | T4.3 |
| `V-LAND-DS-DERIVED` | `ds_onnet` is derived read-only from the owning home | invariant | `tests/unit/test_descriptors.py::test_tenant_landline_has_four_packed_positions` (descriptor declaration) | TODO | T4.4 |
| `V-LAND-TARGET-CHARS` | `target` uses the target-identifier character set and is pack-safe | error | TODO | TODO | T4.2 |
| `V-LAND-IH-ENUM01` | `ih_on` is `enum01` (`0` or `1`) | error | TODO | TODO | T4.1 |

## dispatcher

| Rule id | Rule | Severity | Unit test | Form test | Owner |
|---|---|---|---|---|---|
| `V-DISP-DEST-IPV4` | Destination host is a literal IPv4 address; an FQDN is rejected with the "use the voip-stack host's LAN IP" hint | error | TODO | TODO | T4.5 |
| `V-DISP-DEST-PORT` | Destination port is an integer | error | TODO | TODO | T4.5 |
| `V-DISP-TRANSPORT` | Destination carries `transport=` in `tcp`/`udp`; omitted is accepted as TCP with a warning | warn | TODO | TODO | T4.5 |
| `V-DISP-UDP-WARN` | UDP transport warns with the MTU text: "OPTIONS pings stay green while INVITEs fragment and die in the tunnel" | warn | TODO | TODO | T4.5 |
| `V-DISP-NO-REGISTER-INFER` | Never infer REGISTER transport from the destination transport; the relay forces UDP anyway | invariant | TODO | TODO | T4.5 |
| `V-DISP-SETID-IN-USE` | A `setid` referenced by a home shows an info badge | info | TODO | TODO | T4.5 |
| `V-DISP-DELETE-BLOCKED` | Deleting a referenced set is blocked, naming the tenants that use it | error | TODO | TODO | T5 |

## dialplan

| Rule id | Rule | Severity | Unit test | Form test | Owner |
|---|---|---|---|---|---|
| `V-DP-MATCH-COMPILES` | `match_exp` compiles under Python `re` before any write | error | TODO | TODO | T4.7 |
| `V-DP-PCRE-PARITY` | PCRE-only constructs produce the "approximate check" warning instead of a false rejection | warn | TODO | TODO | T4.7 |
| `V-DP-MATCH-OP-LOCKED` | `match_op` is locked to `1` | invariant | TODO | TODO | T4.7 |
| `V-DP-SUBST-IDENTITY` | `subst_exp` is locked to the identity `^(.*)$` ("match-only by design") | invariant | TODO | TODO | T4.7 |
| `V-DP-REPL-IDENTITY` | `repl_exp` is locked to the identity `\1` ("match-only by design") | invariant | TODO | TODO | T4.7 |
| `V-DP-TRANSLATE-PROOF` | Optional sample after save is proven through `dialplan.translate` and shown in the toast | info | TODO | TODO | T4.7 |
| `V-DP-PRESET-COUNTRY` | "Country profile" preset creates an operator-chosen emergency partition (none seeded) | info | TODO | TODO | T4.7 |
| `V-DP-PRESET-BLOCK` | "Block partition" preset creates an operator-chosen block partition (none seeded) | info | TODO | TODO | T4.7 |

## trusted

| Rule id | Rule | Severity | Unit test | Form test | Owner |
|---|---|---|---|---|---|
| `V-TRUST-SRC-IPV4` | `src_ip` is a valid IPv4 address | error | TODO | TODO | T4.6 |
| `V-TRUST-NON-PRIVATE-OVERRIDE` | Non-CGNAT/private addresses are rejected with an explicit override; the generic non-tunnel path must remain possible | error | TODO | TODO | T4.6 |
| `V-TRUST-ALIAS-SHAPE` | A `100.96.128.0/24` utility-alias-shaped address warns "use the wire-observed native IP" | warn | TODO | TODO | T4.6 |
| `V-TRUST-PROTO-ANY` | `proto` is locked to `any` | invariant | TODO | TODO | T4.6 |
| `V-TRUST-TAG-EXISTS` | `tag` is selected from existing tenant domains via `options_query` | error | TODO | TODO | T4.6 |
| `V-TRUST-DUP-SRC-IP` | The same `src_ip` cannot appear across tags | error | TODO | TODO | T4.6 |

## Warnings-save rules

Every `warn` row in the tables above. These saves must succeed and show the
badge; none may be treated as a hard error.

| Rule id | Warning text / behaviour |
|---|---|
| `V-REG-DS-ONNET-CONVENTION` | `ds_onnet` is not `10+N`; show the convention nudge, keep the save |
| `V-REG-CLI-PREFIX-OVERLAP` | The home's CLI prefix-overlaps another home's landline |
| `V-DISP-UDP-WARN` | Destination uses UDP on the tunnel: "OPTIONS pings stay green while INVITEs fragment and die in the tunnel" |
| `V-DP-PCRE-PARITY` | Pattern uses PCRE-only syntax: "approximate check" |
| `V-TRUST-ALIAS-SHAPE` | Address looks like the `100.96.128.0/24` utility alias: "use the wire-observed native IP" |

Descriptor cross-refs may also declare `on_missing: warn`; no current table
uses that option. If one is added, add it to this section and to the matrix.
