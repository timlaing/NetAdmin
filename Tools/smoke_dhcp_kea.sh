#!/usr/bin/env bash
# Smoke-test the NetAdmin Kea DHCP service image.
#
# Verifies the same contract as the ISC image smoke: no TLS material baked
# into the image, the shared-volume key flow (wait -> install -> start),
# loud failure when the key never arrives, plus the Kea specifics: the
# rendered DHCP-DDNS configuration, both daemons alive with responsive
# control sockets, kea-dhcp4 bound on UDP/67, and passing config checks.
# All key material is synthetic throwaway.
set -euo pipefail
image="${1:-netadmin-dhcp-kea:test}"
name="netadmin-dhcp-kea-smoke-${RANDOM}"
volume="${name}-rndc"
cfg="$(mktemp -d)"

cleanup() {
  result=$?
  if [[ "$result" != 0 ]]; then
    docker logs "$name" 2>&1 | grep -E 'entrypoint-kea|ERROR|kea-dhcp|D2' >&2 || true
    docker logs --tail 30 "$name" >&2 || true
  fi
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker volume rm "$volume" >/dev/null 2>&1 || true
  rm -rf "$cfg"
}
trap cleanup EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }

# grep without -q so the log stream is consumed to EOF: with pipefail an early
# exit would surface docker logs' SIGPIPE as a false mismatch.
log_has() { local pattern="$1"; docker logs "$name" 2>&1 | grep "$pattern" >/dev/null; }

running() { docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null || echo false; }

wait_log() { # pattern, seconds
  local pattern="$1" limit="$2"
  for _ in $(seq "$limit"); do
    if log_has "$pattern"; then return 0; fi
    [[ "$(running)" == true ]] || return 1
    sleep 1
  done
  return 1
}

start() { # optional -e assignments are passed through
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker run -d --name "$name" \
    --mount "type=volume,source=$volume,target=/run/rndc" \
    --mount "type=bind,source=$cfg,target=/etc/dhcp" \
    "$@" \
    "$image" >/dev/null
}

# 0. No TLS material, rendered config or lease database may exist in the
#    image layer itself.
if ! docker run --rm --entrypoint sh "$image" -c \
    'test ! -e /etc/dhcp/rndc.key -a ! -e /var/lib/kea/dhcp4.leases'; then
  fail "image ships baked TLS material or lease state"
fi

# Derive a subnet on this host's docker bridge so kea-dhcp4 can bind eth0 on
# any runner instead of hard-coding a private range.
subnet="$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Subnet}}')"
read -r net prefix gw range_start range_end <<<"$(python3 -c "
import ipaddress
import sys

n = ipaddress.ip_network(sys.argv[1])
if n.prefixlen > 28:
    raise SystemExit('bridge subnet too small: ' + sys.argv[1])
base = int(n.network_address)
addr = ipaddress.ip_address
print(n.network_address, n.prefixlen, addr(base + 1), addr(base + 10), addr(base + 11))
" "$subnet")"

# Shaped like the live Kea config will be: the daemon, memfile leases, the
# management control socket, one subnet with a pool and router option.
cat > "$cfg/kea4.conf" <<EOF
{
  "Dhcp4": {
    "interfaces-config": {
      "interfaces": [ "eth0" ],
      "dhcp-socket-type": "udp"
    },
    "lease-database": {
      "type": "memfile",
      "persist": true,
      "name": "/var/lib/kea/dhcp4.leases"
    },
    "valid-lifetime": 600,
    "max-valid-lifetime": 1200,
    "authoritative": true,
    "subnet4": [
      {
        "id": 1,
        "subnet": "$net/$prefix",
        "pools": [ { "pool": "$range_start - $range_end" } ],
        "option-data": [ { "name": "routers", "data": "$gw" } ]
      }
    ],
    "control-sockets": [
      {
        "socket-type": "unix",
        "socket-name": "kea4-ctrl.sock"
      }
    ],
    "loggers": [
      { "name": "*", "severity": "INFO" }
    ]
  }
}
EOF

# 1. No key on the volume: wait without starting either daemon.
start
wait_log "waiting for the dns TSIG key" 15 || fail "no waiting banner"
[[ "$(running)" == true ]] || fail "container exited while waiting for the key"
if log_has "rendered D2 config"; then
  fail "daemons started although the key never arrived"
fi

# 2. Key appears on the shared volume: install, render, start both daemons.
docker run --rm --mount "type=volume,source=$volume,target=/out" --entrypoint sh "$image" -c \
  'printf "key \"rndc-key\" {\n\talgorithm hmac-sha256;\n\tsecret \"c21tb2tlLXNlY3JldC12YWx1ZS1mM3h0dw==\";\n};\n" > /out/rndc.key'
wait_log "installed dns key" 15 || fail "key was never installed"
wait_log "rendered D2 config" 15 || fail "D2 config was never rendered"
wait_log "started kea-dhcp-ddns" 15 || fail "D2 was never started"
[[ -f "$cfg/rndc.key" ]] || fail "key not copied into the bind-mounted /etc/dhcp tree"
# Compared inside the container: the entrypoint writes the key as root, and
# on Linux runners that file is not readable by the unprivileged CI user.
docker exec "$name" cmp /etc/dhcp/rndc.key /run/rndc/rndc.key \
  || fail "installed key differs from the shared key"
docker exec "$name" kea-dhcp-ddns -t /run/kea/kead2.conf >/dev/null 2>&1 \
  || fail "rendered D2 config failed its config check"
wait_log "DHCP4_STARTING" 15 || fail "kea-dhcp4 did not report a start"
[[ "$(running)" == true ]] || fail "container exited after starting"
docker exec "$name" kea-dhcp4 -t /etc/dhcp/kea4.conf >/dev/null 2>&1 \
  || fail "kea-dhcp4 config check failed"
docker exec "$name" sh -c 'ss -lun | grep -q ":67 "' \
  || fail "kea-dhcp4 is not listening on UDP/67"
dhcp4_version="$(printf '{"command":"version-get"}\n' | docker exec -i "$name" nc -U /run/kea/kea4-ctrl.sock)"
echo "$dhcp4_version" | grep -q '"extended"' \
  || fail "kea-dhcp4 control socket did not answer version-get: $dhcp4_version"
d2_version="$(printf '{"command":"version-get"}\n' | docker exec -i "$name" nc -U /run/kea/kea-ddns.sock)"
echo "$d2_version" | grep -q '"extended"' \
  || fail "D2 control socket did not answer version-get: $d2_version"

# 3. Key never arrives: fail loudly before either daemon runs.
docker rm -f "$name" >/dev/null 2>&1 || true
docker volume rm "$volume" >/dev/null 2>&1 || true
docker volume create "$volume" >/dev/null
start -e DHCP_RNDC_WAIT_TIMEOUT=3
for _ in $(seq 15); do
  [[ "$(running)" == false ]] && break
  sleep 1
done
[[ "$(running)" == false ]] || fail "container did not stop when the key never arrived"
[[ "$(docker inspect -f '{{.State.ExitCode}}' "$name")" != 0 ]] || fail "key timeout exited zero"
log_has "ERROR: no key at" || fail "no timeout error message"
if log_has "rendered D2 config"; then
  fail "daemons started although the key never arrived"
fi

echo "Kea DHCP container smoke checks passed"
