#!/bin/sh
set -eu

# acme.sh container entrypoint (RFC 2136 / nsupdate against Technitium DNS).
# Mounted by compose at /run.sh; companion deploy-certs.sh at /deploy-certs.sh
# Required env:
#   ACME_EMAIL
#   ACME_DOMAINS        comma-separated SAN list (wildcard first)
#   ACME_CERT_NAME      must be the FIRST domain in ACME_DOMAINS
#   NSUPDATE_SERVER     Technitium IP/hostname reachable from the VPS
#
# Optional:
#   ACME_SERVER         letsencrypt (default) or letsencrypt_test
#   ACME_KEYLENGTH      2048 (default) or ec-256
#   ACME_DNSSLEEP       60 (default)
#   ACME_DNS_PROVIDER   dns_nsupdate (default) or dns_technitium
#   ACME_FORCE_ISSUE    1 to force a reissue on the next start
#   NSUPDATE_SERVER_PORT 53
#   NSUPDATE_ZONE       zone Technitium allows dynamic updates for
#   NSUPDATE_KEY        /run/secrets/technitium_tsig  (compose secret)
#   NSUPDATE_OPT
#   Technitium_Server, Technitium_Token   (only for dns_technitium)

: "${ACME_EMAIL:?ACME_EMAIL is required}"
: "${ACME_DOMAINS:?ACME_DOMAINS is required}"
: "${ACME_CERT_NAME:?ACME_CERT_NAME is required}"

ACME_SERVER="${ACME_SERVER:-letsencrypt}"
ACME_KEYLENGTH="${ACME_KEYLENGTH:-2048}"
ACME_DNSSLEEP="${ACME_DNSSLEEP:-60}"
ACME_DNS_PROVIDER="${ACME_DNS_PROVIDER:-dns_nsupdate}"
ACME_FORCE_ISSUE="${ACME_FORCE_ISSUE:-0}"

if [ "$ACME_DNS_PROVIDER" = "dns_nsupdate" ]; then
  : "${NSUPDATE_SERVER:?NSUPDATE_SERVER is required for dns_nsupdate}"
fi

# Build the -d list without word-splitting on spaces and without globbing
# (critical with wildcard domains).
set -f
OLDIFS=$IFS
IFS=','
set --
for domain in $ACME_DOMAINS; do
  domain=$(printf '%s' "$domain" | tr -d ' ')
  [ -n "$domain" ] || continue
  set -- "$@" -d "$domain"
done
IFS=$OLDIFS
set +f

CERT_DIR="/acme.sh/${ACME_CERT_NAME}"
if [ -d "${CERT_DIR}_ecc" ]; then
  CERT_DIR="${CERT_DIR}_ecc"
fi

issue_once() {
  force_arg=""
  [ "$ACME_FORCE_ISSUE" = "1" ] && force_arg="--force"

  acme.sh --issue \
    --dns "$ACME_DNS_PROVIDER" \
    --server "$ACME_SERVER" \
    --accountemail "$ACME_EMAIL" \
    --keylength "$ACME_KEYLENGTH" \
    --dnssleep "$ACME_DNSSLEEP" \
    $force_arg \
    "$@"
}

if [ ! -s "${CERT_DIR}/fullchain.cer" ] || [ "$ACME_FORCE_ISSUE" = "1" ]; then
  until issue_once "$@"; do
    echo "certificate issuance failed; retrying in 1 hour" >&2
    sleep 3600
  done
fi

# Write into a staging directory first, then deploy both files and touch the
# marker; the cert-reloader only reloads after the marker appears, so Kamailio
# never sees a new certificate with an old private key.
mkdir -p /tls/.staging
acme.sh --install-cert -d "$ACME_CERT_NAME" \
  --key-file /tls/.staging/sip.key \
  --fullchain-file /tls/.staging/sip.crt \
  --reloadcmd "/bin/sh /deploy-certs.sh"

# acme.sh renews automatically when the certificate is near expiry.
while :; do
  acme.sh --cron || echo "acme.sh --cron failed; will retry" >&2
  sleep 12h
done
