#!/bin/sh
# watch-certs.sh — runs in the OFFICIAL kamailio image (has kamcmd), shares
# Kamailio's RPC control socket via the run volume, and calls
# `kamcmd tls.reload` whenever acme.sh installs renewed certificates.
#
# Contract with the acme service (containerized acme.sh, daemon cron):
#   acme.sh --install-cert ... --fullchain-file /tls/.staging/fullchain.cer
#           --key-file /tls/.staging/privkey.key
#           --reloadcmd '/bin/sh /deploy-certs.sh'
# and deploy-certs.sh promotes staging → live atomically, then bumps
# /tls/.renewed. This watcher reacts to the marker, never half-written pairs.
#
# Mounted paths: /certs  = $EDGE_HOME/tls (ro) ;  /run/kamailio = $EDGE_RUN
set -u

CERTDIR=${CERTDIR:-/certs}
MARKER="$CERTDIR/.renewed"
CERT="$CERTDIR/sip.crt"
KKEY="$CERTDIR/sip.key"
SOCK=unix:/run/kamailio/kamailio_ctl

log() { echo "$(date -u +%FT%TZ) $*"; }

reload_once() {
	if [ ! -s "$CERT" ] || [ ! -s "$KKEY" ]; then
		log "certificate or key missing ($CERTDIR)" >&2
		return 1
	fi
	if [ -S /run/kamailio/kamailio_ctl ]; then
		if kamcmd -s "$SOCK" tls.reload 2>/dev/null; then
			log "tls.reload: certificates reloaded"
			sleep 60        # debounce bursts of acme writes
			return 0
		fi
		log "tls.reload failed" >&2
		return 1
	fi
	log "waiting for $SOCK" >&2
	return 1
}

# Wait for Kamailio to create the control socket, then reload once so a
# certificate that already exists when Kamailio starts is picked up.
i=0
while [ "$i" -lt 30 ]; do
	[ -S /run/kamailio/kamailio_ctl ] && break
	i=$((i + 1))
	sleep 2
done
reload_once || true

last=""
[ -f "$MARKER" ] && last=$(stat -c '%Y:%s' "$MARKER")

while :; do
	sleep 30
	[ -f "$MARKER" ] || continue
	cur=$(stat -c '%Y:%s' "$MARKER")
	if [ "$cur" != "$last" ]; then
		# the marker is touched only after fullchain.cer + privkey.key are in
		reload_once || true
		last="$cur"
	fi
done
