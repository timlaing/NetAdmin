#!/usr/bin/env bash
# Smoke-test the NetAdmin Bind service image.
#
# Verifies the key precedence rules end to end: fresh generation with the full
# key printed to the console, a working RFC 2136 dynamic update through that
# key (the same path dhcpd uses), byte-identical reuse when the container is
# recreated without the env secret, seeding from NETADMIN_DNS_TSIG_SECRET, and
# loud refusal of an invalid seed without falling back to generation. The
# image must also ship no packaged rndc.key. Only synthetic material is used.
set -euo pipefail
image="${1:-netadmin-dns:test}"
name="netadmin-dns-smoke-${RANDOM}"
volume="${name}-rndc"
conf="$(mktemp -d)"

cleanup() {
  result=$?
  if [[ "$result" != 0 ]]; then
    docker logs "$name" 2>&1 | grep -E 'entrypoint-dns|begin rndc|end rndc|ERROR' >&2 || true
  fi
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker volume rm "$volume" >/dev/null 2>&1 || true
  rm -rf "$conf"
}
trap cleanup EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }

# grep without -q so the log stream is consumed to EOF: with pipefail an early
# exit would surface docker logs' SIGPIPE as a false mismatch.
log_has() { local pattern="$1"; docker logs "$name" 2>&1 | grep "$pattern" >/dev/null; }

# A production-shaped config: the key include, a static zone for probing and a
# dynamic zone guarded by the same allow-update key named.conf uses in anger.
cat > "$conf/named.conf" <<'EOF'
include "/etc/bind/rndc.key";
options {
	directory "/var/bind";
	listen-on { 127.0.0.1; };
	listen-on-v6 { none; };
	pid-file "/var/run/named/named.pid";
	allow-transfer { none; };
};
zone "localhost" IN { type master; file "pri/localhost.zone"; };
zone "smoke.internal" IN {
	type master;
	file "dyn/smoke.internal.zone";
	allow-update { key "rndc-key"; };
};
EOF
cat > "$conf/smoke.internal.zone" <<'EOF'
$TTL 300
@	IN	SOA	ns.smoke.internal. admin.smoke.internal. (1 3600 600 86400 300 )
	IN	NS	ns.smoke.internal.
ns	IN	A	127.0.0.1
EOF

# The alpine bind package ships a static rndc.key; the image must purge it.
if docker run --rm --entrypoint cat "$image" /etc/bind/rndc.key >/dev/null 2>&1; then
  fail "image ships the packaged rndc.key (must be purged at build time)"
fi

start() { # optional -e assignments are passed through
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker run -d --name "$name" \
    --mount "type=volume,source=$volume,target=/run/rndc" \
    -v "$conf/named.conf:/etc/bind/named.conf:ro" \
    -v "$conf/smoke.internal.zone:/var/bind/dyn/smoke.internal.zone:ro" \
    "$@" \
    "$image" -c /etc/bind/named.conf -4 >/dev/null
}

reset_volume() {
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker volume rm "$volume" >/dev/null 2>&1 || true
  docker volume create "$volume" >/dev/null
}

running() { docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null || echo false; }

wait_log() { # $1 = pattern, $2 = seconds
  local pattern="$1" seconds="$2"
  for _ in $(seq "$seconds"); do
    if log_has "$pattern"; then return 0; fi
    [[ "$(running)" == true ]] || return 1
    sleep 1
  done
  return 1
}

wait_ready() {
  for _ in $(seq 30); do
    if docker exec "$name" nslookup localhost 127.0.0.1 >/dev/null 2>&1; then return 0; fi
    [[ "$(running)" == true ]] || return 1
    sleep 1
  done
  return 1
}

# The full key block the entrypoint prints on every start.
key_from_logs() {
  docker logs "$name" 2>&1 \
    | sed -n '/----- begin rndc-key key -----/,/----- end rndc-key key -----/p'
}

# 1. No env secret: generate once and print the full key to the console.
start
wait_log "generated fresh key" 10 || fail "no generation banner"
wait_ready || fail "named did not become ready"
generated="$(key_from_logs)"
grep -q 'key "rndc-key"' <<<"$generated" || fail "full key not printed to the console"
grep -q 'secret "' <<<"$generated" || fail "printed key is missing its secret"
grep -q 'algorithm hmac-sha256' <<<"$generated" || fail "printed key has the wrong algorithm"

# The key must actually authorise dynamic updates, as dhcpd does.
printf 'server 127.0.0.1\nupdate add host1.smoke.internal. 300 A 10.9.9.9\nsend\n' \
  | docker exec -i "$name" nsupdate -k /run/rndc/rndc.key \
  || fail "nsupdate rejected the generated key"
if ! docker exec "$name" nslookup host1.smoke.internal 127.0.0.1 2>/dev/null | grep 10.9.9.9 >/dev/null; then
  fail "dynamic update is not visible in the zone"
fi

# 2. Recreated without the env secret: reuse, byte for byte.
start
wait_log "reusing existing key" 10 || fail "existing key was not reused"
[[ "$(key_from_logs)" == "$generated" ]] || fail "key changed across a restart"

# 3. Fresh volume with a synthetic env secret: seed from the environment.
reset_volume
seed_secret="$(openssl rand -base64 32 | tr -d '\n')"
start -e "NETADMIN_DNS_TSIG_SECRET=$seed_secret"
wait_log "seeded key from NETADMIN_DNS_TSIG_SECRET" 10 || fail "no seed banner"
seeded="$(key_from_logs)"
grep -F "secret \"$seed_secret\"" <<<"$seeded" >/dev/null || fail "seeded secret not printed"

# 4. The requirement: removing the env secret later must not rotate the key.
start
wait_log "reusing existing key" 10 || fail "seeded key was not reused"
rotated="$(key_from_logs)"
grep -F "secret \"$seed_secret\"" <<<"$rotated" >/dev/null \
  || fail "stored key regenerated after env removal"
wait_ready || fail "named did not start on the reused key"

# 5. Invalid seed: refuse loudly, write nothing, never generate instead.
reset_volume
start -e 'NETADMIN_DNS_TSIG_SECRET=!!!not-base64!!!'
for _ in $(seq 10); do
  [[ "$(running)" == false ]] && break
  sleep 1
done
[[ "$(running)" == false ]] || fail "container kept running with an invalid seed"
[[ "$(docker inspect -f '{{.State.ExitCode}}' "$name")" != 0 ]] || fail "invalid seed exited zero"
log_has "ERROR" || fail "invalid seed produced no error message"
if log_has "generated fresh key"; then
  fail "invalid seed silently fell back to generating a key"
fi
docker run --rm --mount "type=volume,source=$volume,target=/v" --entrypoint sh "$image" \
  -c '[ ! -e /v/rndc.key ]' || fail "invalid seed left a key file behind"

echo "DNS container smoke checks passed"
