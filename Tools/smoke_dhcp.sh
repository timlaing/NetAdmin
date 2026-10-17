#!/usr/bin/env bash
# Smoke-test the NetAdmin DHCP service image.
#
# Verifies the shared-volume key flow: while no key exists the container waits
# without starting dhcpd, once the key appears it is installed into the
# bind-mounted /etc/dhcp tree (the file the dhcp agent validates), dhcpd then
# starts and the config passes `dhcpd -t`, and a key that never arrives fails
# the container loudly. Synthetic key material only.
set -euo pipefail
image="${1:-netadmin-dhcp:test}"
name="netadmin-dhcp-smoke-${RANDOM}"
volume="${name}-rndc"
cfg="$(mktemp -d)"

cleanup() {
  result=$?
  if [[ "$result" != 0 ]]; then
    docker logs "$name" 2>&1 | grep -E 'entrypoint-dhcp|ERROR|Listening|Can.t open' >&2 || true
  fi
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker volume rm "$volume" >/dev/null 2>&1 || true
  rm -rf "$cfg"
}
trap cleanup EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }

# grep without -q so the log stream is consumed to EOF: with pipefail an early
# exit would surface docker logs' SIGPIPE as a false mismatch.
log_has() { docker logs "$name" 2>&1 | grep "$1" >/dev/null; }

# Derive a subnet on this host's docker bridge so dhcpd can bind eth0 on any
# runner instead of hard-coding a private range.
subnet="$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Subnet}}')"
read -r net mask gw range_start range_end <<<"$(python3 -c "
import ipaddress
import sys

n = ipaddress.ip_network(sys.argv[1])
if n.prefixlen > 28:
    raise SystemExit('bridge subnet too small: ' + sys.argv[1])
base = int(n.network_address)
addr = ipaddress.ip_address
print(n.network_address, n.netmask, addr(base + 1), addr(base + 10), addr(base + 11))
" "$subnet")"

# Shaped like the live dhcpd.conf: the key include plus a DDNS zone stanza
# that references the key by its deployment name.
cat > "$cfg/dhcpd.conf" <<EOF
ddns-update-style interim;
deny client-updates;
do-forward-updates on;
include "/etc/dhcp/rndc.key";
default-lease-time 600;
max-lease-time 1200;
authoritative;
log-facility local7;
subnet $net netmask $mask {
  range $range_start $range_end;
  option routers $gw;
  zone smoke.internal. { primary 127.0.0.1; key "rndc-key"; }
}
EOF

start() { # $1 = seconds the entrypoint may wait for the key
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker run -d --name "$name" \
    --mount "type=volume,source=$volume,target=/run/rndc,readonly" \
    -v "$cfg:/etc/dhcp" \
    -e "DHCP_RNDC_WAIT_TIMEOUT=$1" \
    "$image" -4 -d -cf /etc/dhcp/dhcpd.conf eth0 -lf /var/lib/dhcp/dhcpd.leases >/dev/null
}

running() { docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null || echo false; }

wait_log() { # $1 = pattern, $2 = seconds
  for _ in $(seq "$2"); do
    if log_has "$1"; then return 0; fi
    [[ "$(running)" == true ]] || return 0
    sleep 1
  done
  return 1
}

docker volume create "$volume" >/dev/null

# 1. No key yet: wait, do not start dhcpd, do not install anything.
start 30
wait_log "waiting for the dns TSIG key" 10 || fail "no wait banner"
[[ "$(running)" == true ]] || fail "container exited while waiting for the key"
[[ ! -f "$cfg/rndc.key" ]] || fail "a key was installed before one existed"
if log_has "Listening on"; then
  fail "dhcpd started before the key arrived"
fi

# 2. Key appears on the shared volume: install and start dhcpd.
docker run --rm --mount "type=volume,source=$volume,target=/out" --entrypoint sh "$image" -c \
  'printf "key \"rndc-key\" {\n\talgorithm hmac-sha256;\n\tsecret \"c21tb2tlLXNlY3JldC12YWx1ZS1mM3h0dw==\";\n};\n" > /out/rndc.key'
wait_log "installed dns key" 15 || fail "key was never installed"
[[ -f "$cfg/rndc.key" ]] || fail "key not copied into the bind-mounted /etc/dhcp tree"
# Compared inside the container: the entrypoint writes the key as root, and
# on Linux runners that file is not readable by the unprivileged CI user.
docker exec "$name" grep -q 'secret "c21tb2tlLXNlY3JldC12YWx1ZS1mM3h0dw=="' /etc/dhcp/rndc.key \
  || fail "installed key has the wrong contents"
docker exec "$name" cmp /etc/dhcp/rndc.key /run/rndc/rndc.key \
  || fail "installed key differs from the shared key"
wait_log "Listening on" 15 || fail "dhcpd did not start listening"
docker exec "$name" dhcpd -t -cf /etc/dhcp/dhcpd.conf >/dev/null 2>&1 \
  || fail "dhcpd config check failed"
[[ "$(running)" == true ]] || fail "dhcpd exited after starting"

# 3. Key never arrives: fail loudly before dhcpd runs.
docker rm -f "$name" >/dev/null 2>&1 || true
docker volume rm "$volume" >/dev/null 2>&1 || true
docker volume create "$volume" >/dev/null
start 3
for _ in $(seq 15); do
  [[ "$(running)" == false ]] && break
  sleep 1
done
[[ "$(running)" == false ]] || fail "container did not stop when the key never arrived"
[[ "$(docker inspect -f '{{.State.ExitCode}}' "$name")" != 0 ]] || fail "key timeout exited zero"
log_has "ERROR: no key at" || fail "no timeout error message"
if log_has "Listening on"; then
  fail "dhcpd started although the key never arrived"
fi

echo "DHCP container smoke checks passed"
