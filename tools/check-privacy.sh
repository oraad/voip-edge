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

MODE=staged
if [ "${1:-}" = "--all" ]; then
    MODE=all
    # Tracked-but-deleted files have no worktree content and cannot leak.
    FILES=$(git ls-files -co --exclude-standard | while IFS= read -r f; do
        [ -e "$f" ] && printf '%s\n' "$f"
    done)
else
    FILES=$(git diff --cached --name-only --diff-filter=ACMR)
fi
[ -n "$FILES" ] || exit 0

read_file() { # read_file <path>: staged mode reads the INDEX blob, not the worktree
    if [ "$MODE" = "all" ]; then cat -- "$1"; else git show ":$1" 2>/dev/null; fi
}

fail=0
deny="tools/private-deny.txt"

# --- 1. file gate ---------------------------------------------------------
TMPLIST=$(mktemp)
trap 'rm -f "$TMPLIST"' EXIT
printf '%s\n' "$FILES" > "$TMPLIST"
while IFS= read -r f; do
    case "$(printf '%s' "$f" | tr '[:upper:]' '[:lower:]')" in
        *.console.sqlite.bak|*.parked.json)
            echo "privacy: refusing backup/snapshot companion: $f" >&2; fail=1 ;;
        *.sqlite.tmp|*.console.sqlite.bak.tmp|*.parked.json.tmp)
            echo "privacy: refusing partial backup: $f" >&2; fail=1 ;;
        *.sqlite*|*.db*|*.bak)
            echo "privacy: refusing database/backup file: $f" >&2; fail=1 ;;
        *.key|*.pem|*.crt|*.cer|*.p12|*.pfx|*.jks|*.keystore|*privkey*|*fullchain*)
            echo "privacy: refusing key/cert material: $f" >&2; fail=1 ;;
        *tsig*|*TSIG*|*/tls/*|tls/*|acme/*|*/acme/data/*|*/certs/*|certs/*|*console-data/*|*/backups/*|backups/*)
            echo "privacy: refusing secret/state path: $f" >&2; fail=1 ;;
        .env.example|*/.env.example)
            : ;;  # reviewed templates only; other .env.example files are blocked
        .env|.env.*|*.env|*.env.*|*/.env|*/.env.*|.envrc|*/.envrc)
            echo "privacy: refusing env file: $f" >&2; fail=1 ;;
    esac
done < "$TMPLIST"

# --- 2. content gate ------------------------------------------------------
if [ -f "$deny" ]; then
    while IFS= read -r token; do
        case "$token" in ''|\#*) continue ;; esac
        hits=$(printf '%s\n' "$FILES" | while IFS= read -r f; do
            read_file "$f" 2>/dev/null | grep -IilF -- "$token" /dev/stdin 2>/dev/null | sed "s|/dev/stdin|$f|"
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
    # The checker itself contains the literal patterns below; never flag it.
    case "$f" in tools/check-privacy.sh) continue ;; esac
    read_file "$f" 2>/dev/null | grep -qE 'BEGIN (RSA |EC |OPENSSH |ENCRYPTED |DSA |PGP )?PRIVATE KEY|BEGIN PGP PRIVATE KEY BLOCK' && echo "$f"
    # DNS-01 key *values* are caught by the private deny-list; matching the
    # variable names here produced false positives in documentation.
done | sort -u | tr '\n' ' ')
if [ -n "$gen" ]; then
    echo "privacy: private key / DNS-01 credential material found in: $gen" >&2
    fail=1
fi

[ "$fail" = 0 ] || { echo "privacy: commit blocked (see docs/privacy.md)" >&2; exit 1; }
exit 0
