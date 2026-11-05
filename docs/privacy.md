# Privacy rules for this repository

This repo is shared/deployable, while the edge it configures carries real
tenant data. The rules below keep the two separate.

## What must never be committed

- Live databases: `kamailio/db/*.sqlite*` (the `db/edge.sqlite` in the edge
  deployment holds tenants, registrations-adjacent state and phone numbers).
- TLS material: `*.key`, `*.crt`, `*.cer`, `*.pem`, acme state, TSIG keys.
- Runtime state: `console-data/`, `backups/`, `tls/`, `acme/`.
- Environment files with tokens (`*.env`, `.env`).
- Anything listed in **your local** `tools/private-deny.txt` — the file is
  gitignored because it contains the values it protects.

## How it is enforced

1. `.gitignore` blocks the artifact types.
2. `tools/check-privacy.sh` runs from `.githooks/pre-commit`
   (enable once with `git config core.hooksPath .githooks`):
   - refuses sensitive file types/paths,
   - greps staged files for literals from `tools/private-deny.txt`,
   - refuses private-key blocks; DNS-01 credential *values* are caught only by
     the local deny-list.
3. Before publishing a clone, run `tools/check-privacy.sh --all` and also:
   ```sh
   git log -p --all | grep -iE '<your tenant domain>|<your landline>|BEGIN .*PRIVATE KEY'
   ```
   History is data too — this repository's history has been rewritten to
   purge deployment identifiers; treat any future leak the same way.

## Placeholder conventions used in the docs

| Kind | Placeholder |
|---|---|
| tenant domain | `sip.<client>.home.example.net` |
| tenant names | `alpha`, `beta`, `gamma` |
| landlines | `01111111`, `02222222`, `03333333` |
| edge public IP | `203.0.113.10` (TEST-NET-3) |
| overlay / tunnel | `100.64.0.9`, `100.64.10.x` (CGNAT example range) |
| home LAN | `10.0.0.10` |
| phones | `198.51.100.x` (TEST-NET-2) |
| e-mail | `admin@example.net` |

Keep these conventions when writing docs, examples, fixtures and tests.
