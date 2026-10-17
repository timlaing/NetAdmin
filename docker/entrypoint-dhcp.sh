#!/bin/sh
# Wait for the DNS service's TSIG key on the shared volume, install it where
# dhcpd.conf's include expects it, then exec dhcpd.
#
# The key is copied into the mounted /etc/dhcp tree (the live dhcp/config
# directory) so the dhcp agent's `dhcpd -t` validation sees the same include.
# The dns service is also declared as a compose health dependency; the wait
# loop here covers restarts where dns is briefly slower than dhcp.
set -eu

SHARED_KEY="/run/rndc/rndc.key"
TARGET_KEY="/etc/dhcp/rndc.key"
WAIT_TIMEOUT="${DHCP_RNDC_WAIT_TIMEOUT:-60}"

say() { printf '[entrypoint-dhcp] %s\n' "$*"; }
die() { printf '[entrypoint-dhcp] ERROR: %s\n' "$*" >&2; exit 1; }

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

grep -q 'secret "' "$SHARED_KEY" || die "$SHARED_KEY has no TSIG secret"
cp "$SHARED_KEY" "$TARGET_KEY.tmp"
chmod 640 "$TARGET_KEY.tmp"
mv -f "$TARGET_KEY.tmp" "$TARGET_KEY"
say "installed dns key into $TARGET_KEY (waited ${waited}s)"

exec dhcpd -f "$@"
