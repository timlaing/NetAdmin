#!/usr/bin/env bash
# Smoke-test the NetAdmin FreeRADIUS service image.
#
# Verifies that the image ships no TLS material, that material is generated at
# runtime into the certs volume and only ever logged as fingerprints (never as
# private keys), reuse across restarts, environment seeding with validation
# (including the optional CA key and client material), loud failure on corrupt
# or partial material without silent regeneration, and a passing `radiusd -XC`
# config check. All certificates are synthetic throwaways created here.
set -euo pipefail
image="${1:-netadmin-radius:test}"
name="netadmin-radius-smoke-${RANDOM}"
volume="${name}-certs"
work="$(mktemp -d)"

cleanup() {
  result=$?
  if [[ "$result" != 0 ]]; then
    docker logs "$name" 2>&1 | grep -E 'entrypoint-radius|ERROR|Failed|Instantiation' >&2 || true
    docker logs --tail 30 "$name" >&2 || true
  fi
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker volume rm "$volume" >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }

# grep without -q so the log stream is consumed to EOF: with pipefail an early
# exit would surface docker logs' SIGPIPE as a false mismatch.
log_has() { local pattern="$1"; docker logs "$name" 2>&1 | grep "$pattern" >/dev/null; }

# 0. No TLS material may exist in the image layer itself.
if ! docker run --rm --entrypoint sh "$image" -c \
    'test ! -e /etc/raddb/certs/server.pem -a ! -e /etc/raddb/certs/ca.pem'; then
  fail "image ships baked TLS material"
fi

running() { docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null || echo false; }

start() { # optional -e assignments are passed through
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker run -d --name "$name" \
    --mount "type=volume,source=$volume,target=/etc/raddb/certs" \
    "$@" \
    "$image" -X >/dev/null
}

reset_volume() {
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker volume rm "$volume" >/dev/null 2>&1 || true
  docker volume create "$volume" >/dev/null
}

wait_ready() {
  for _ in $(seq 120); do
    if log_has "Ready to process requests"; then return 0; fi
    [[ "$(running)" == true ]] || return 1
    sleep 1
  done
  return 1
}

fp_lines() { docker logs "$name" 2>&1 | grep -E '^    (server|ca|client)\.pem +sha256' || true; }

assert_no_keys() {
  if log_has "PRIVATE KEY"; then
    fail "private key material leaked into the logs"
  fi
}

b64() { local file="$1"; base64 < "$file" | tr -d '\n'; }

# 1. Empty volume, no seed: generate at runtime, log fingerprints only.
start
wait_ready || fail "radiusd did not start after generation"
log_has "generated fresh TLS material" || fail "no generation banner"
log_has "TLS material fingerprints" || fail "no fingerprint banner"
[[ -n "$(fp_lines)" ]] || fail "no fingerprints logged"
assert_no_keys
docker exec "$name" radiusd -XC >/dev/null 2>&1 || fail "radiusd config check failed"
generated_fp="$(fp_lines)"

# 2. Recreated on the same volume: reuse, fingerprints identical.
start
wait_ready || fail "radiusd did not restart on existing material"
log_has "reusing existing TLS material" || fail "no reuse banner"
[[ "$(fp_lines)" == "$generated_fp" ]] || fail "fingerprints changed on reuse"
assert_no_keys

# 3. Fresh volume seeded from environment: synthetic CA, server (encrypted key
#    at the stock bootstrap password), optional CA key and client material.
openssl genrsa -out "$work/ca.key" 2048 2>/dev/null
openssl req -x509 -new -nodes -key "$work/ca.key" -sha256 -days 2 \
  -subj "/CN=NetAdmin smoke CA" -out "$work/ca.crt" 2>/dev/null
openssl genrsa -out "$work/server.key" 2048 2>/dev/null
openssl req -new -key "$work/server.key" -subj "/CN=smoke-server" -out "$work/server.csr" 2>/dev/null
openssl x509 -req -in "$work/server.csr" -CA "$work/ca.crt" -CAkey "$work/ca.key" \
  -CAcreateserial -days 2 -sha256 -out "$work/server.crt" 2>/dev/null
openssl pkcs8 -topk8 -v2 aes-256-cbc -passout pass:whatever \
  -in "$work/server.key" -out "$work/server.encrypted" 2>/dev/null \
  || fail "cannot create an encrypted test key"
cat "$work/server.crt" "$work/server.encrypted" > "$work/server.pem"
openssl genrsa -out "$work/client.key" 2048 2>/dev/null
openssl req -new -key "$work/client.key" -subj "/CN=smoke-client" -out "$work/client.csr" 2>/dev/null
openssl x509 -req -in "$work/client.csr" -CA "$work/ca.crt" -CAkey "$work/ca.key" \
  -CAcreateserial -days 2 -sha256 -out "$work/client.crt" 2>/dev/null
cat "$work/client.key" "$work/client.crt" > "$work/client.pem"

reset_volume
start \
  -e "NETADMIN_RADIUS_TLS_SERVER_PEM=$(b64 "$work/server.pem")" \
  -e "NETADMIN_RADIUS_TLS_CA_PEM=$(b64 "$work/ca.crt")" \
  -e "NETADMIN_RADIUS_TLS_CA_KEY=$(b64 "$work/ca.key")" \
  -e "NETADMIN_RADIUS_TLS_CLIENT_PEM=$(b64 "$work/client.pem")"
wait_ready || fail "radiusd did not start on seeded material"
log_has "seeded TLS material from environment" || fail "no seed banner"
expected_fp="$(openssl x509 -in "$work/server.crt" -noout -fingerprint -sha256 | cut -d= -f2-)"
fp_lines | grep -Fi "$expected_fp" >/dev/null || fail "logged fingerprint does not match the seeded certificate"
docker exec "$name" test -s /etc/raddb/certs/ca.key || fail "optional CA key was not installed"
docker exec "$name" test -s /etc/raddb/certs/client.pem || fail "optional client material was not installed"
assert_no_keys
docker exec "$name" radiusd -XC >/dev/null 2>&1 || fail "config check failed on seeded material"

# 4. Corrupt seed: fail loudly, install nothing, never generate instead.
reset_volume
start \
  -e "NETADMIN_RADIUS_TLS_SERVER_PEM=$(printf 'this is not a certificate' | base64 | tr -d '\n')" \
  -e "NETADMIN_RADIUS_TLS_CA_PEM=$(b64 "$work/ca.crt")"
for _ in $(seq 15); do
  [[ "$(running)" == false ]] && break
  sleep 1
done
[[ "$(running)" == false ]] || fail "container kept running on a corrupt seed"
[[ "$(docker inspect -f '{{.State.ExitCode}}' "$name")" != 0 ]] || fail "corrupt seed exited zero"
log_has "ERROR: SERVER_PEM: no X.509 certificate found" || fail "missing corruption error"
if log_has "generated fresh TLS material"; then
  fail "corrupt seed silently regenerated the material"
fi
docker run --rm --mount "type=volume,source=$volume,target=/v" --entrypoint sh "$image" \
  -c '[ ! -e /v/server.pem -a ! -e /v/ca.pem ]' || fail "corrupt seed left material behind"

# 5. Partial material: refuse to start rather than regenerate around it.
reset_volume
docker run --rm --mount "type=volume,source=$volume,target=/v" --entrypoint sh "$image" \
  -c 'printf x > /v/server.pem'
start
for _ in $(seq 15); do
  [[ "$(running)" == false ]] && break
  sleep 1
done
[[ "$(running)" == false ]] || fail "container kept running with partial material"
log_has "ERROR: partial TLS material" || fail "missing partial-material error"

echo "RADIUS container smoke checks passed"
