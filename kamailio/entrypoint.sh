#!/bin/sh
# Generate the tenant-domain suffix gate from CONSOLE_ALLOWED_SUFFIXES, then
# start Kamailio. Keeping the regex derived from the same list the console
# validator and onboarding wizard use removes the manual sync step.
set -eu

: "${CONSOLE_ALLOWED_SUFFIXES:?CONSOLE_ALLOWED_SUFFIXES is required}"
config_dir=${KAM_CONFIG_DIR:-/etc/kamailio/voip-edge}
suffixes=$(printf '%s' "$CONSOLE_ALLOWED_SUFFIXES" | tr -d ' ')
[ -n "$suffixes" ] || {
	echo "CONSOLE_ALLOWED_SUFFIXES is empty" >&2
	exit 1
}

set -f
old_ifs=$IFS
IFS=,
regex=""
for suffix in $suffixes; do
	[ -n "$suffix" ] || {
		echo "CONSOLE_ALLOWED_SUFFIXES has an empty entry" >&2
		exit 1
	}
	part=$(printf '%s' "$suffix" | sed 's/\./[.]/g')
	if [ -n "$regex" ]; then
		regex="$regex|$part"
	else
		regex="$part"
	fi
done
IFS=$old_ifs
set +f

cat > "${config_dir}/tenant_suffix.cfg" <<EOF
# Generated at container start from CONSOLE_ALLOWED_SUFFIXES.
#!substdef "!TENANTSUFFIXRE!^[a-z0-9.-]+[.](${regex})!"
EOF

exec kamailio -DD -E -m "${SHM_MEMORY:-256}" -M "${PKG_MEMORY:-64}" \
	-f "${config_dir}/kamailio.cfg" -A WITH_CONSOLE "$@"
