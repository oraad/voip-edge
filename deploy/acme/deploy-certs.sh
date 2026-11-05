#!/bin/sh
set -eu
# acme.sh --reloadcmd target: atomic cert deploy into the live directory,
# then the marker for the cert-reloader (which watches .renewed, not the
# cert mtime — so Kamailio never reloads a half-written pair).

STAGE=/tls/.staging
LIVE=/tls

[ -s "$STAGE/sip.crt" ] || { echo "missing $STAGE/sip.crt" >&2; exit 1; }
[ -s "$STAGE/sip.key" ] || { echo "missing $STAGE/sip.key" >&2; exit 1; }

cp "$STAGE/sip.crt" "$LIVE/sip.crt.new"
cp "$STAGE/sip.key" "$LIVE/sip.key.new"
chmod 644 "$LIVE/sip.crt.new"
chmod 600 "$LIVE/sip.key.new"

mv -f "$LIVE/sip.crt.new" "$LIVE/sip.crt"
mv -f "$LIVE/sip.key.new" "$LIVE/sip.key"

touch "$LIVE/.renewed"
echo "installed certificate and key; reload marker updated"
