# Backups and restore runbook

This runbook covers the console's online backups and the **stopped-stack**
restore procedure. For the lifecycle semantics that trigger backups, see
`docs/operations.md`; for deployment paths, see `docs/ops/deploy.md`.

## What a backup is

The console uses the SQLite **online backup API**
(`sqlite3.Connection.backup`), never a file copy of a running database. The
result is WAL-safe and consistent even while Kamailio and the console are
writing.

A backup is a set of three files:

| File | Contents |
|---|---|
| `edge-<UTC>-<tag>.sqlite` | consistent snapshot of the Kamailio routing DB |
| `edge-<UTC>-<tag>.sqlite.console.sqlite.bak` | WAL-safe copy of the console sidecar (parked snapshots, intents, audit) |
| `edge-<UTC>-<tag>.sqlite.parked.json` | human-readable dump of the sidecar's `console_parked` rows at that moment |

Stamps include microseconds, so names sort chronologically even for backups
taken in the same second. Tags look like `park:<domain>`,
`restore:<domain>`, `destroy:<domain>` and `wizard-apply:<domain>`.

A backup is taken **before every edge-mutating lifecycle operation** (park,
restore, active destroy) and before wizard apply. Destroying a parked snapshot
only removes the sidecar row; it does not mutate the edge DB. A backup failure aborts the operation before any edge mutation:
fix the path/permissions and retry; never proceed without a backup.

## Where the files live

| Item | Container path | Host path |
|---|---|---|
| Backups | `/var/lib/console/backups/` | `${EDGE_HOME}/console/backups/` |
| Sidecar DB | `/var/lib/console/console.sqlite` | `${EDGE_HOME}/console/console.sqlite` |
| Parked snapshots | table `console_parked` inside the sidecar | same `console.sqlite` |
| Sidecar backup | `edge-*.sqlite.console.sqlite.bak` beside each backup | same directory |
| Parked evidence | `edge-*.sqlite.parked.json` beside each backup | same directory |

`${EDGE_HOME}` is the shared edge workspace root (default `/opt/voip-edge`).
The directory is a bind mount; it must survive container replacement and be
included in host-level backups.

## Rotation

Rotation is **keep-5 globally**, across all purpose tags; there is no
per-tenant or per-purpose retention. After a successful backup the oldest files
beyond the newest five are deleted together with their `.console.sqlite.bak`
and `.parked.json` companions. Failed backups are cleaned up (including a
partially written sidecar copy) and do not count.

Consequences:

- Do not manually keep an unlimited history in this directory; copy anything
  that must be retained long-term to separate storage.
- The pre-operation backup is normally the newest file. If an operation fails,
  that backup still exists and can be used for restore.
- Restoring an older database does **not** delete newer backups. Take a fresh
  backup before re-enabling writes; normal rotation will then apply.

## Restore procedure

> **Never hot-swap `edge.sqlite`.** Kamailio and the console hold the old inode
> open, keep WAL/SHM state, and cache table data in memory. Replacing the file
> underneath a running process loses writes and can corrupt the database. Stop
> the writers first.

### 1. Choose the backup

Pick the newest suitable `edge-*.sqlite`, or the tagged backup taken just
before the failed operation. Prefer a file whose `.parked.json` sibling exists;
it is your evidence for parked homes.

```bash
ls -lt "${EDGE_HOME}/console/backups"/edge-*.sqlite | head
```

Optionally verify the chosen file with SQLite's integrity check. The console
image already contains Python + sqlite3, so no host tooling is required:

```bash
docker run --rm -v "${EDGE_HOME}/console/backups:/b:ro" \
  --entrypoint python voip-edge-console:0.1.11 -c \
  'import sqlite3,sys; c=sqlite3.connect("file:/b/<file>.sqlite?mode=ro", uri=True); print(c.execute("PRAGMA integrity_check").fetchone()[0])'
```

Expected output: `ok`.

### 2. Stop the writers

Stop the **console first**, then Kamailio:

```bash
# Arcane: stop voip-edge-console, then voip-edge.
# Plain Docker:
docker stop voip-edge-console
docker stop voip-edge
```

Confirm neither container is running before touching the file:

```bash
docker ps --filter name=voip-edge --filter name=voip-edge-console
```

### 3. Preserve the current database

Never overwrite the current file without a copy aside. Move the whole SQLite
set to a quarantine directory (same filesystem, so `mv` is atomic):

```bash
set -e
cd "${EDGE_HOME}/kamailio/db"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "quarantine-${STAMP}"
mv edge.sqlite "quarantine-${STAMP}/"
for extra in edge.sqlite-wal edge.sqlite-shm edge.sqlite-journal \
             edge.sqlite-mj*; do
  [ -e "$extra" ] && mv "$extra" "quarantine-${STAMP}/"
done
```

The old `-wal` and `-shm` files belong to the old database and must **not** be
paired with the restored file. Removing them is required; keeping them can
replay unrelated WAL frames.

### 4. Replace `edge.sqlite` with the chosen backup

Copy the backup into place (copy, not move, so the backup remains available):

```bash
cp "${EDGE_HOME}/console/backups/<chosen>.sqlite" edge.sqlite
```

Keep ownership/mode consistent with what Kamailio expects (the stock stack runs
Kamailio as root): `root:root 0644` is the normal state. If you use the
non-root console recipe, apply the ownership/ACL documented in
`docs/ops/deploy.md` instead. Do not `chmod 777`.

Do not create `-wal`/`-shm` by hand. The first writer creates them; the console
re-asserts `journal_mode=WAL` and `busy_timeout` at startup.

### 5. Start Kamailio first, then the console

```bash
# Arcane: start voip-edge and wait for it to be healthy.
docker start voip-edge
# Set CONSOLE_READONLY=1 and CONSOLE_SWEEPER_INTERVAL=0 in .env, then
# recreate the console so the new environment is actually loaded.
docker compose up -d --force-recreate console
```
`docker start` alone does **not** reload `.env`; a recreate is required.

Starting Kamailio first ensures the DB is opened and cached by the edge before
the console begins serving. Start the console with `CONSOLE_READONLY=1` and
`CONSOLE_SWEEPER_INTERVAL=0` first: the restored sidecar may still contain a
non-DONE lifecycle intent from the failed operation that produced the backup.
Inspect `/system/audit` and `console_intent`; either resume it intentionally or
mark/clear it after verifying the recovered rows. Only then enable writes and
the normal sweeper. Pending reloads are re-issued by the sweeper on its next
pass once writes are enabled.

### 6. Verify

1. `docker exec voip-edge-console curl -fsS http://100.64.0.9:8080/health`
   returns `{"status":"ok"}`.
2. The Homes list reads and rows match the restored snapshot.
3. The status strip says the edge is reachable and shows the expected uptime.
4. Pending reloads clear. If one remains after Kamailio is up, the sweeper will
   re-issue it; restarting the console also runs the sweeper.
5. Save a harmless edit (or wait for the next normal backup) and confirm a new
   `edge-*.sqlite` appears, proving the backup path still works.

### 7. Sidecar recovery (only if the sidecar itself is lost)

`edge.sqlite` and `console.sqlite` are independent. Normally keep the sidecar:
it holds audit rows, park snapshots, lifecycle intents and pending reloads.

If `console.sqlite` is corrupt or missing:

1. Stop the console (Kamailio may stay up).
2. Preserve the broken file aside.
3. Restore the sibling sidecar copy from the chosen backup
   (`edge-<UTC>-<tag>.sqlite.console.sqlite.bak`) or a copy taken by host-level
   backup. The sidecar runs in WAL mode, so quarantine **all three** files
   together — never pair a restored `console.sqlite` with the old `-wal`/`-shm`:
   ```bash
   set -e
   cd "${EDGE_HOME}/console"
   mkdir -p console.sqlite.broken
   mv console.sqlite console.sqlite.broken/
   for extra in console.sqlite-wal console.sqlite-shm console.sqlite-journal \
                console.sqlite-mj*; do
     [ -e "$extra" ] && mv "$extra" console.sqlite.broken/
   done
   cp "backups/edge-<UTC>-<tag>.sqlite.console.sqlite.bak" console.sqlite
   ```
   The console applies forward migrations on startup. It **refuses** a sidecar
   whose `PRAGMA user_version` is newer than the image supports, so deploy the
   matching console image rather than downgrading the data.
4. Start the console and check the Audit & backups page and the parked list.

The `.parked.json` file is **disaster-recovery evidence**, not an import
format. Do not copy it into `edge.sqlite` and do not assume it restores parked
homes automatically; it records what the sidecar held at backup time.

## Why hot-swapping is forbidden

- SQLite identifies a database by inode. Replacing the path leaves the old
  inode open in every running connection.
- The WAL and shared-memory files are tied to the old database; mixing them
  with a restored file can replay invalid frames.
- Kamailio keeps `tenant_registry`, dispatcher, dialplan and trusted data in
  memory. Even a correct file swap needs the reloads (or a restart) before the
  edge uses the restored rows.

If you cannot stop the stack, do not restore. Wait for a maintenance window;
the console is designed to keep serving reads and queueing intent rows in the
meantime.
