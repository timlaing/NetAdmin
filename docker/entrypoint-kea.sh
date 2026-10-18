#!/bin/sh
# Wait for the DNS service's TSIG key on the shared volume, install it where
# deployments expect it, render the Kea DHCP-DDNS configuration with the key's
# algorithm and secret, start kea-dhcp-ddns, then exec kea-dhcp4.
#
# The key is copied into the mounted /etc/dhcp tree (the live dhcp/config
# directory) so a bind-format copy exists for anything that still expects the
# ISC layout; the rendered D2 config stays under /run/kea because it embeds
# the secret. The dns service is also declared as a compose health dependency;
# the wait loop here covers restarts where dns is briefly slower than dhcp.
set -eu

SHARED_KEY="/run/rndc/rndc.key"
TARGET_KEY="/etc/dhcp/rndc.key"
D2_TEMPLATE="${KEA_DDNS_TEMPLATE:-/etc/kea/kead2.conf.in}"
D2_CONFIG="/run/kea/kead2.conf"
SOCKET_DIR="/run/kea"
WAIT_TIMEOUT="${DHCP_RNDC_WAIT_TIMEOUT:-60}"

say() { printf '[entrypoint-kea] %s\n' "$*"; }
die() { printf '[entrypoint-kea] ERROR: %s\n' "$*" >&2; exit 1; }

waited=0
while [ ! -s "$SHARED_KEY" ]; do
  if [ "$waited" -ge "$WAIT_TIMEOUT" ]; then
    die "no key at $SHARED_KEY after ${WAIT_TIMEOUT}s (is the dns service up and healthy?)"
  fi
  if [ "$waited" -eq 0 ]; then
    say "waiting for the dns TSIG key at $SHARED_KEY ..."
  fi
  sleep 1
  waited=$((waited + 1))
done

secret="$(sed -n 's/^[[:space:]]*secret[[:space:]]*"\([^"]*\)".*/\1/p' "$SHARED_KEY" | head -n 1)"
algorithm="$(sed -n 's/^[[:space:]]*algorithm[[:space:]]*\([^;]*\);.*/\1/p' "$SHARED_KEY" | head -n 1 \
  | awk '{print toupper($0)}')"
[ -n "$secret" ] || die "$SHARED_KEY has no TSIG secret"
[ -n "$algorithm" ] || die "$SHARED_KEY has no TSIG algorithm"

cp "$SHARED_KEY" "$TARGET_KEY.tmp"
chmod 640 "$TARGET_KEY.tmp"
mv -f "$TARGET_KEY.tmp" "$TARGET_KEY"
say "installed dns key into $TARGET_KEY (waited ${waited}s)"

install -d -m 0750 "$SOCKET_DIR"
sed -e "s|@ALGO@|$algorithm|" -e "s|@SECRET@|$secret|" "$D2_TEMPLATE" > "$D2_CONFIG.tmp"
if grep -q '@[A-Z]*@' "$D2_CONFIG.tmp"; then
  rm -f "$D2_CONFIG.tmp"
  die "$D2_TEMPLATE has unsubstituted placeholders"
fi
chmod 640 "$D2_CONFIG.tmp"
mv -f "$D2_CONFIG.tmp" "$D2_CONFIG"
say "rendered D2 config with $algorithm TSIG key into $D2_CONFIG"

kea-dhcp-ddns -c "$D2_CONFIG" &
say "started kea-dhcp-ddns"

# PID 1 is kea-dhcp4 so `docker stop` shuts the DHCP server down cleanly;
# docker's cgroup teardown stops the background D2 process with the container.
exec kea-dhcp4 -c "${KEA4_CONFIG:-/etc/dhcp/kea4.conf}"
