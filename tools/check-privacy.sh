#!/bin/sh
# check-privacy.sh — refuse to commit secrets or deployment-specific data.
#
# Two layers:
#   1. file-type/name gate: live databases, TLS keys/certs, acme state, env files.
#   2. content gate: an UNTRACKED private deny-list (tools/private-deny.txt,
#      gitignored) plus generic high-risk patterns. The deny-list itself must
#      never be committed — that is the whole point.
#
# Usage: tools/check-privacy.sh            # scan staged files (pre-commit)
#        tools/check-privacy.sh --all      # scan the whole tree
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"

if [ "${1:-}" = "--all" ]; then
    FILES=$(git ls-files)
else
    FILES=$(git diff --cached --name-only --diff-filter=ACMR)
fi
[ -n "$FILES" ] || exit 0

fail=0
deny="tools/private-deny.txt"

# --- 1. file gate ---------------------------------------------------------
for f in $FILES; do
    case "$f" in
        *.sqlite|*.sqlite-wal|*.sqlite-shm|*.db)
            echo "privacy: refusing database file: $f" >&2; fail=1 ;;
        *.key|*.pem|*.crt|*.cer|*privkey*|*fullchain*)
            echo "privacy: refusing key/cert material: $f" >&2; fail=1 ;;
        *technitium_tsig*|*/tls/*|*/acme/*|*console-data/*|*backups/*)
            echo "privacy: refusing secret/state path: $f" >&2; fail=1 ;;
        .env|*.env|*/.env)
            echo "privacy: refusing env file: $f" >&2; fail=1 ;;
    esac
done

# --- 2. content gate ------------------------------------------------------
if [ -f "$deny" ]; then
    while IFS= read -r token; do
        case "$token" in ''|\#*) continue ;; esac
        hits=$(printf '%s\n' "$FILES" | while IFS= read -r f; do
            [ -f "$f" ] && grep -IilF -- "$token" "$f" 2>/dev/null
        done | sort -u | tr '\n' ' ')
        if [ -n "$hits" ]; then
            echo "privacy: deny-listed value found in: $hits" >&2
            echo "         (value itself is not printed; check tools/private-deny.txt)" >&2
            fail=1
        fi
    done < "$deny"
fi

# generic high-risk patterns that must never appear in tracked text
gen=$(printf '%s\n' "$FILES" | while IFS= read -r f; do
    [ -f "$f" ] || continue
    grep -IlE 'BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY' "$f" 2>/dev/null
    grep -IlE 'RFC2136_(SECRET|KEY)|Technitium_Token=|NSUPDATE_KEY=' "$f" 2>/dev/null
done | sort -u | tr '\n' ' ')
if [ -n "$gen" ]; then
    echo "privacy: private key / DNS-01 credential material found in: $gen" >&2
    fail=1
fi

[ "$fail" = 0 ] || { echo "privacy: commit blocked (see docs/privacy.md)" >&2; exit 1; }
exit 0
