#!/usr/bin/env bash
# acme-deploy-hook.sh — sync acme.sh-issued certs into Kamailio's TLS dir.
#
# acme.sh runs in a container in this deployment; it renews into its /acme.sh
# volume. Run this hook on the Kamailio host on a timer (or via docker exec /
# a shared volume watch) after each renewal. Idempotent: only reloads when a
# file actually changed.
#
#   CERT_DIR=/mnt/acme.sh/home.example.net_ec384   # acme.sh volume path on this host
#   ./acme-deploy-hook.sh
set -euo pipefail

CERT_DIR="${CERT_DIR:?set CERT_DIR to the acme.sh output dir (fullchain.cer + *.key)}"
DOM="${DOM:-home.example.net}"        # acme.sh uses "-ecc"/_ec* suffixes per key type
DEST="${DEST:-/etc/kamailio/tls}"

mkdir -p "$DEST"

# locate the newest fullchain + key in CERT_DIR (handles _ecc suffix etc.)
FULL="$(ls -t "$CERT_DIR"/*fullchain*.cer "$CERT_DIR"/fullchain.cer 2>/dev/null | head -1 || true)"
KEY="$(ls -t "$CERT_DIR"/*.key 2>/dev/null | grep -v ca | head -1 || true)"
[ -n "$FULL" ] && [ -n "$KEY" ] || { echo "no cert/key found in $CERT_DIR" >&2; exit 1; }

changed=0
install_if_changed() {
	if ! cmp -s "$1" "$2" 2>/dev/null; then
		install -m600 -o kamailio -g kamailio "$1" "$2" 2>/dev/null \
			|| install -m600 "$1" "$2"
		changed=1
	fi
}
install_if_changed "$FULL" "$DEST/sip.crt"
install_if_changed "$KEY"  "$DEST/sip.key"

if [ "$changed" = 1 ]; then
	if docker inspect voip-edge >/dev/null 2>&1; then
		docker exec voip-edge kamcmd tls.reload || docker restart voip-edge
		echo "certs updated, voip-edge container TLS reloaded"
	elif command -v kamcmd >/dev/null && kamcmd tls.reload >/dev/null 2>&1; then
		echo "certs updated, kamailio TLS reloaded (tls.reload)"
	else
		systemctl reload kamailio 2>/dev/null || systemctl restart kamailio
		echo "certs updated, kamailio reloaded/restarted"
	fi
else
	echo "certs unchanged"
fi