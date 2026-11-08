# Backups and restore

The console takes online backups with the SQLite backup API, never a file copy
of a running database. For lifecycle triggers see `docs/operations.md`.

## Backup set

| File | Contents |
|---|---|
| `edge-<UTC>-<tag>.sqlite` | routing DB snapshot |
| `edge-<UTC>-<tag>.sqlite.console.sqlite.bak` | sidecar copy |
| `edge-<UTC>-<tag>.sqlite.parked.json` | parked rows, readable |

Tags: `park:<domain>`, `restore:<domain>`, `destroy:<domain>`,
`wizard-apply:<domain>`. A backup is taken before every edge-mutating
lifecycle operation and before wizard apply. A failure aborts the operation.

Location: `${EDGE_HOME}/console/backups/`. Rotation keeps the newest 5
globally and removes all three companions together.

## Restore

> **Never hot-swap `edge.sqlite`.** Stop both writers first. Kamailio and the
> console hold the old inode open.

1. Pick the newest suitable `edge-*.sqlite`, or the backup taken before the
   failed operation.
2. Stop the console, then Kamailio:
   ```bash
   docker stop voip-edge-console
   docker stop voip-edge
   ```
3. Quarantine the current SQLite set:
   ```bash
   cd "${EDGE_HOME}/kamailio/db"
   STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
   mkdir -p "quarantine-${STAMP}"
   mv edge.sqlite edge.sqlite-wal edge.sqlite-shm edge.sqlite-journal \
      "quarantine-${STAMP}/" 2>/dev/null || true
   ```
4. Copy the backup into place. Keep `root:root 0644`; do not create WAL/SHM.
   ```bash
   cp "${EDGE_HOME}/console/backups/<chosen>.sqlite" edge.sqlite
   ```
5. Start Kamailio, then the console with `CONSOLE_READONLY=1` and
   `CONSOLE_SWEEPER_INTERVAL=0`:
   ```bash
   docker start voip-edge
   docker compose up -d --force-recreate console
   ```
6. Check `/health`, the Homes list, edge status and pending reloads. Inspect
   `/system/audit`; resume or clear any stale lifecycle intent. Then enable
   writes and the normal sweeper.

## Sidecar recovery

If `console.sqlite` is lost, stop the console, quarantine all three sidecar
files, and restore
`edge-<UTC>-<tag>.sqlite.console.sqlite.bak` as `console.sqlite`. The console
migrates forward on start and refuses a newer schema than the image supports.

`.parked.json` is evidence, not an import format. Do not copy it into
`edge.sqlite`.
