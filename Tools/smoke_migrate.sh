#!/usr/bin/env bash
# Smoke-test the ISC-to-Kea migration tooling.
#
# Builds a realistic ISC dhcpd tree (global options, an include, a
# shared-network, hosts across files, live and stale leases), then runs
# scripts/migrate-to-kea.sh inside the given image — it carries both netadmin
# (to generate) and kea-dhcp4 (the same 3.0.3 parser/runtime the service
# image ships). The output is then proved against the real daemon:
# kea-dhcp4 -t, a full startup that loads the converted lease database
# without discarding a row, byte-identical re-runs, and loud failure on
# conflicting reservations or a missing include.
set -euo pipefail
image="${1:-netadmin-dhcp-agent:test}"
name="netadmin-migrate-smoke-${RANDOM}"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work="$(mktemp -d)"

cleanup() {
  result=$?
  if [[ "$result" != 0 ]]; then
    docker logs "$name" 2>&1 | grep -E 'ERROR|ROW_ERROR|DHCP4' >&2 || true
    docker logs --tail 30 "$name" >&2 || true
  fi
  docker rm -f "$name" >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }

# Run the user-facing wrapper inside the image. The repo is mounted read-only;
# NETADMIN_PYTHON skips host-venv detection (a macOS venv cannot execute in
# the container). Paths in the ISC tree reference /work, the container view.
run_migration() { # out-name config-path [leases-path]
  local out="$1" config="$2" leases="${3:-}"
  local args=(--config "$config" --out-dir "/work/$out" --no-validate)
  if [[ -n "$leases" ]]; then args+=(--leases "$leases"); fi
  docker run --rm \
    --mount "type=bind,source=$repo_root,target=/repo,readonly" \
    --mount "type=bind,source=$work,target=/work" \
    --workdir /repo \
    --env NETADMIN_PYTHON=python3 \
    --entrypoint bash "$image" scripts/migrate-to-kea.sh "${args[@]}"
}

echo "=== building a realistic ISC configuration tree"
mkdir -p "$work/dhcpd"
cat > "$work/dhcpd/dhcpd.conf" <<'EOF'
default-lease-time 600;
max-lease-time 7200;
authoritative;
option domain-name "lan.example";
option domain-name-servers 192.0.2.1, 192.0.2.2;

include "/work/dhcpd/hosts.conf";

subnet 192.0.2.0 netmask 255.255.255.0 {
  range 192.0.2.100 192.0.2.199;
  option routers 192.0.2.1;
}

shared-network office {
  subnet 198.51.100.0 netmask 255.255.255.0 {
    range 198.51.100.50 198.51.100.99;
    option routers 198.51.100.1;
  }
}

host printer {
  hardware ethernet aa:bb:cc:dd:ee:02;
  fixed-address 198.51.100.20;
}
EOF
cat > "$work/dhcpd/hosts.conf" <<'EOF'
host laptop {
  hardware ethernet aa:bb:cc:dd:ee:01;
  fixed-address 192.0.2.10;
}
host pi {
  hardware ethernet aa:bb:cc:dd:ee:03;
  fixed-address 192.0.2.20;
}
EOF
cat > "$work/dhcpd.leases" <<'EOF'
lease 192.0.2.10 {
  starts 6 2026/08/26 11:00:00;
  ends 6 2026/08/26 12:00:00;
  binding state active;
  hardware ethernet aa:bb:cc:dd:ee:01;
  client-hostname "laptop";
}
lease 198.51.100.20 {
  starts 6 2026/08/26 11:00:00;
  ends 6 2026/08/26 23:00:00;
  binding state released;
  hardware ethernet aa:bb:cc:dd:ee:02;
}
lease 192.0.2.55 {
  starts 6 2026/08/26 11:00:00;
  ends 6 2026/08/26 11:30:00;
  binding state free;
  hardware ethernet aa:bb:cc:dd:ee:99;
}
EOF

echo "=== migrating with scripts/migrate-to-kea.sh"
run_migration out /work/dhcpd/dhcpd.conf /work/dhcpd.leases
out="$work/out"
[[ -f "$out/kea4.conf" ]] || fail "kea4.conf was not written"
[[ -f "$out/netadmin-reservations.json" ]] || fail "reservations file was not written"
[[ -f "$out/dhcp4.leases" ]] || fail "converted leases were not written"

echo "=== generated files carry the whole tree"
conf="$out/kea4.conf"
grep -Fq '<?include "/etc/dhcp/netadmin-reservations.json"?>' "$conf" \
  || fail "reservation include missing from kea4.conf"
grep -Fq '"reservations-global": true' "$conf" || fail "reservations-global missing"
grep -Fq '"domain-name-servers"' "$conf" || fail "global options not inherited into subnets"
grep -Fq '"subnet": "192.0.2.0/24"' "$conf" || fail "first subnet missing"
grep -Fq '"subnet": "198.51.100.0/24"' "$conf" || fail "shared-network subnet missing"
for host in laptop pi printer; do
  grep -Fq "\"hostname\": \"$host\"" "$out/netadmin-reservations.json" \
    || fail "reservation $host not folded into the managed set"
done
[[ "$(grep -c '"hostname"' "$out/netadmin-reservations.json")" == 3 ]] \
  || fail "expected exactly 3 folded reservations"

leases="$out/dhcp4.leases"
head -1 "$leases" | grep -Fxq \
  'address,hwaddr,client_id,valid_lifetime,expire,subnet_id,fqdn_fwd,fqdn_rev,hostname,state,user_context,pool_id' \
  || fail "memfile header does not match Kea 3.0.3"
[[ "$(grep -c . "$leases")" == 3 ]] || fail "expected header + 2 converted leases"
grep -q '^192.0.2.10,' "$leases" || fail "active lease not converted"
if grep -q '^192.0.2.55,' "$leases"; then fail "stale free lease must be dropped"; fi
[[ "$(tail -n +2 "$leases" | grep -c ',0$')" == 2 ]] \
  || fail "every row must end with a numeric pool_id"

echo "=== kea-dhcp4 -t accepts the generated configuration"
docker run --rm \
  --mount "type=bind,source=$out,target=/etc/dhcp,readonly" \
  --entrypoint kea-dhcp4 "$image" -t /etc/dhcp/kea4.conf \
  || fail "kea-dhcp4 rejected the generated config"

echo "=== daemon starts and loads the converted lease database"
docker run -d --name "$name" \
  --mount "type=bind,source=$out,target=/etc/dhcp" \
  --mount "type=bind,source=$leases,target=/var/lib/kea/dhcp4.leases" \
  --entrypoint sh "$image" -c \
  'install -d -m 0750 /run/kea && exec kea-dhcp4 -c /etc/dhcp/kea4.conf' >/dev/null
started=0
for _ in $(seq 20); do
  if docker logs "$name" 2>&1 | grep -q DHCP4_STARTED; then started=1; break; fi
  running="$(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null || echo false)"
  [[ "$running" == true ]] || break
  sleep 1
done
[[ "$started" == 1 ]] || fail "kea-dhcp4 did not start on the generated config"
logs="$(docker logs "$name" 2>&1 || true)"
docker rm -f "$name" >/dev/null
if printf '%s\n' "$logs" | grep -q ROW_ERROR; then
  printf '%s\n' "$logs" | grep ROW_ERROR >&2
  fail "the daemon discarded a converted lease row"
fi
if printf '%s\n' "$logs" | grep -q ' ERROR '; then
  printf '%s\n' "$logs" | grep ' ERROR ' >&2
  fail "the daemon logged errors during startup"
fi

echo "=== a second run reproduces byte-identical output"
run_migration out2 /work/dhcpd/dhcpd.conf /work/dhcpd.leases
diff -r "$out" "$work/out2" >/dev/null || fail "migration output is not deterministic"

echo "=== conflicting reservations are rejected"
cat > "$work/conflict.conf" <<'EOF'
subnet 192.0.2.0 netmask 255.255.255.0 { range 192.0.2.100 192.0.2.199; }
host a { hardware ethernet aa:bb:cc:dd:ee:01; fixed-address 192.0.2.10; }
host b { hardware ethernet aa:bb:cc:dd:ee:01; fixed-address 192.0.2.11; }
EOF
if run_migration conflict /work/conflict.conf; then
  fail "duplicate-MAC migration should have exited non-zero"
fi
[[ ! -e "$work/conflict" ]] || fail "failed migration must not leave output behind"

echo "=== a missing include is rejected"
cat > "$work/broken.conf" <<'EOF'
include "/work/dhcpd/gone.conf";
subnet 192.0.2.0 netmask 255.255.255.0 { range 192.0.2.100 192.0.2.199; }
EOF
if run_migration broken /work/broken.conf; then
  fail "migration with a missing include should have exited non-zero"
fi

echo "PASS: migration tooling"
