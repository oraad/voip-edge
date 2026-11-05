#!/usr/bin/env bash
# create-db.sh — build the edge's sqlite DB from seed.sql
#
# Why not kamdbctl? kamdbctl only creates the STANDARD Kamailio module tables
# (and only empty ones). tenant_registry/tenant_landline are custom packed
# mtree layouts, and the tenant/emergency seed rows have to be written anyway
# — so one SQL script does schema+data in a single reproducible step. The
# DDL for the standard tables here matches kamdbctl's (incl. the `version`
# table rows the modules check at startup).
#
#   ./create-db.sh [db-path]              (default: ./edge.sqlite next to seed.sql)
#
#   DOCKER=1 ./create-db.sh               no sqlite3 on the host: run the build
#                                         inside a throwaway Alpine container
#                                         (image pulled once; needs the seed dir
#                                         + db dir mounted — same file result).
#
# IMPORTANT: never rm -f an existing DB — a running Kamailio holds the OLD
# inode open and keeps reading stale data after re-seed. This script rebuilds
# IN PLACE (drop+create tables, same inode), safe with a running edge. The
# resulting file path must match the kamailio DBURL (#!ifndef DBURL).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DB="${1:-$HERE/edge.sqlite}"

sqlt() { # sqlite client, host or throwaway container
	if [ "${DOCKER:-0}" = 1 ]; then
		docker run --rm -i -v "$HERE":/sql -v "$(dirname "$DB")":/db alpine \
			sh -c "apk add -q sqlite >/dev/null && sqlite3 /db/$(basename "$DB")"
	else
		sqlite3 "$DB"
	fi
}

if [ -f "$DB" ]; then
	sqlt <<'EOSQL'
PRAGMA journal_mode=DELETE;
DROP TABLE IF EXISTS address;
DROP TABLE IF EXISTS dialplan;
DROP TABLE IF EXISTS dispatcher;
DROP TABLE IF EXISTS tenant_landline;
DROP TABLE IF EXISTS tenant_registry;
DROP TABLE IF EXISTS trusted;
DROP TABLE IF EXISTS version;
EOSQL
	echo "rebuilt in place (same inode): $DB"
else
	echo "creating $DB"
fi

sqlt < "$HERE/seed.sql"
echo "tables:"
sqlt <<'EOSQL'
.tables
EOSQL
