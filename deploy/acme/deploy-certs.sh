#!/bin/sh
set -eu
# acme.sh --reloadcmd target: promote the staged pair atomically.
#
# We cannot atomically replace two files; instead each release is a directory
# and the live pair is two stable symlinks through a single "current" symlink.
# Flipping "current" (rename of a symlink) is atomic, so Kamailio never sees a
# new certificate with an old key, even on a manual `tls.reload`.
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

# Stable symlinks consumed by kamailio/tls.cfg (paths never change).
ln -sfn "releases/$TAG" "$LIVE/.current.new"
mv -Tf "$LIVE/.current.new" "$LIVE/current"
ln -sfn current/sip.crt "$LIVE/sip.crt"
ln -sfn current/sip.key "$LIVE/sip.key"

# Keep the five newest releases; never delete the one just activated.
ls -1dt "$RELEASES"/* 2>/dev/null | tail -n +6 | while IFS= read -r old; do
    [ "$(readlink -f "$old")" = "$(readlink -f "$LIVE/current")" ] || rm -rf -- "$old"
done

touch "$LIVE/.renewed"
echo "installed certificate release $TAG; reload marker updated"
