#!/usr/bin/env bash
# run-live.sh — the real-Kamailio park/restore integration gate (T8.7).
#
# Boots the edge repo's kamailio binary with WITH_CONSOLE, registers a
# softphone, then drives console.lifecycle.park/restore against that live
# edge.  Unlike the unit/API suites this touches real ports/sockets.
#
#   ./tests/live/run-live.sh
#
# Exit 0 = journey passed and cleanup found no leftover listeners.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
PY="${PYTHON:-$REPO/.venv/bin/python}"

cd "$REPO" || exit 1

echo "== live Kamailio park/restore journey =="
"$PY" -m pytest -o addopts="" -m live tests/live -v
rc=$?

if [ "$rc" -eq 0 ]; then
	echo "PASS: live Kamailio park/restore journey"
else
	echo "FAIL: live Kamailio park/restore journey (exit $rc)"
fi
exit "$rc"
