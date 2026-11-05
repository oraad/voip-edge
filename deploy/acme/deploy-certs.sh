#!/bin/sh
set -eu
# acme.sh --reloadcmd target: promote the staged pair as one release.
#
# We cannot atomically replace two files; instead each release is a directory
# and the live pair is two stable symlinks through a single "current" symlink.
# Both paths therefore always resolve inside the same release, so Kamailio
# never sees a new certificate with an old key.  The symlink is replaced with
# busybox-safe `ln -sfn` (the acme image has no GNU `mv -T`); the brief
# unlink/symlink window is harmless because Kamailio only reloads after this
# hook returns.
STAGE=/tls/.staging
LIVE=/tls
RELEASES="$LIVE/releases"
TAG=$(date -u +%Y%m%dT%H%M%SZ)

[ -s "$STAGE/sip.crt" ] || { echo "missing $STAGE/sip.crt" >&2; exit 1; }
[ -s "$STAGE/sip.key" ] || { echo "missing $STAGE/sip.key" >&2; exit 1; }

mkdir -p "$RELEASES/$TAG"
cp "$STAGE/sip.crt" "$RELEASES/$TAG/sip.crt"
cp "$STAGE/sip.key" "$RELEASES/$TAG/sip.key"
chmod 644 "$RELEASES/$TAG/sip.crt"
chmod 600 "$RELEASES/$TAG/sip.key"

# Optional: give a non-root Kamailio ownership of the release (set KAM_UID and
# KAM_GID in the acme container).  Root-owned 0600 is correct for the default
# root Kamailio container.
if [ -n "${KAM_UID:-}" ] && [ -n "${KAM_GID:-}" ]; then
    chown "$KAM_UID:$KAM_GID" "$RELEASES/$TAG/sip.crt" "$RELEASES/$TAG/sip.key"
fi

# Stable symlinks consumed by kamailio/tls.cfg (paths never change).
ln -sfn "releases/$TAG" "$LIVE/current"
ln -sfn current/sip.crt "$LIVE/sip.crt"
ln -sfn current/sip.key "$LIVE/sip.key"

# Keep the five newest releases; never delete the one just activated.
ls -1dt "$RELEASES"/* 2>/dev/null | tail -n +6 | while IFS= read -r old; do
    [ "$(readlink -f "$old")" = "$(readlink -f "$LIVE/current")" ] || rm -rf -- "$old"
done

touch "$LIVE/.renewed"
echo "installed certificate release $TAG; reload marker updated"
