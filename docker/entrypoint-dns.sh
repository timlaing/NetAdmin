#!/bin/sh
# Provide the Bind TSIG key, then exec named.
#
# Existing key material is never silently replaced. Precedence:
#   1. /run/rndc/rndc.key exists -> reuse unchanged (even if the env secret
#      was removed or edited later)
#   2. NETADMIN_DNS_TSIG_SECRET set         -> seed the key from the environment
#   3. neither                              -> generate once from /dev/urandom
# The key name is the deployment constant "rndc-key": named.conf allow-update
# and dhcpd.conf both hardcode it, so NETADMIN_DNS_TSIG_NAME is not required.
# The full key is printed to the console on every start.
set -eu

SHARED_KEY="/run/rndc/rndc.key"
BOUND_KEY="/etc/bind/rndc.key"
KEY_NAME="rndc-key"
KEY_ALGORITHM="hmac-sha256"

say() { printf '[entrypoint-dns] %s\n' "$*"; }
die() { printf '[entrypoint-dns] ERROR: %s\n' "$*" >&2; exit 1; }

write_key() { # $1 = base64 secret
  mkdir -p "$(dirname "$SHARED_KEY")"
  printf 'key "%s" {\n\talgorithm %s;\n\tsecret "%s";\n};\n' \
    "$KEY_NAME" "$KEY_ALGORITHM" "$1" > "$SHARED_KEY.tmp"
  mv -f "$SHARED_KEY.tmp" "$SHARED_KEY"
  chmod 640 "$SHARED_KEY"
}

if [ -s "$SHARED_KEY" ]; then
  state="reusing existing key from $SHARED_KEY"
  if [ -n "${NETADMIN_DNS_TSIG_SECRET:-}" ]; then
    stored="$(sed -n 's/.*secret *"\([^"]*\)".*/\1/p' "$SHARED_KEY" | head -n1)"
    if [ "$NETADMIN_DNS_TSIG_SECRET" != "$stored" ]; then
      say "WARNING: NETADMIN_DNS_TSIG_SECRET differs from the stored key; keeping the stored key (stop dns and delete $SHARED_KEY to rotate)"
    fi
  fi
elif [ -n "${NETADMIN_DNS_TSIG_SECRET:-}" ]; then
  decoded="$(printf '%s' "$NETADMIN_DNS_TSIG_SECRET" | base64 -d 2>/dev/null)" \
    || die "NETADMIN_DNS_TSIG_SECRET is not valid base64"
  [ -n "$decoded" ] || die "NETADMIN_DNS_TSIG_SECRET decodes to an empty value"
  write_key "$NETADMIN_DNS_TSIG_SECRET"
  state="seeded key from NETADMIN_DNS_TSIG_SECRET"
else
  write_key "$(head -c 32 /dev/urandom | base64 | tr -d '\n')"
  state="generated fresh key"
fi

# named.conf includes /etc/bind/rndc.key; point it at the shared key so the
# running config, named-checkconf and any operator cat all see the same bytes.
rm -f "$BOUND_KEY"
ln -s "$SHARED_KEY" "$BOUND_KEY"

say "$state"
say "full key (printed on every start):"
echo "----- begin $KEY_NAME key -----"
cat "$SHARED_KEY"
echo "----- end $KEY_NAME key -----"

exec named -g "$@"
