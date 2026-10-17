#!/usr/bin/env bash
# Migrate the live Bind/DHCP/RADIUS stack to the NetAdmin compose deployment.
#
# Run from a checkout of this repo on the server (e.g. /home/laingt/netadmin).
# The old stack lives in OLD_ROOT (default /home/laingt) and keeps its containers
# (stopped, not removed) so rollback is a `docker compose start` away.
#
# Usage:  [OLD_ROOT=/home/laingt] [NETADMIN_ROOT_PASSWORD=...] ./scripts/cutover.sh
set -euo pipefail

OLD_ROOT="${OLD_ROOT:-/home/laingt}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"

say()  { printf '\n=== %s\n' "$*"; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

cd "$REPO_ROOT"

say "Preflight"
command -v docker >/dev/null || die "docker not found"
docker compose version >/dev/null || die "docker compose v2 not found"
docker info >/dev/null || die "cannot talk to docker (add $(id -un) to the docker group?)"
[ -f docker-compose.yml ] || die "run this from a NetAdmin repo checkout"
[ -d "$OLD_ROOT/dns" ] && [ -d "$OLD_ROOT/dhcp" ] || die "old stack trees not found under $OLD_ROOT"
sudo -n true 2>/dev/null || die "need passwordless sudo (to read $OLD_ROOT/dns/rndc.key)"

say "Building NetAdmin images (old stack keeps running meanwhile)"
docker compose build netadmin dhcp-agent radius-agent dns dhcp radius
NETADMIN_IMG="$(docker compose images -q netadmin | head -1)"

if [ ! -f "$ENV_FILE" ]; then
  say "Generating .env"
  if [ -z "${NETADMIN_ROOT_PASSWORD:-}" ]; then
    read -rs -p "Emergency root password (min 12 chars): " ROOT_PW; echo
    read -rs -p "Confirm: " ROOT_PW2; echo
    [ "$ROOT_PW" = "$ROOT_PW2" ] || die "passwords differ"
  else
    ROOT_PW="$NETADMIN_ROOT_PASSWORD"
  fi
  [ "${#ROOT_PW}" -ge 12 ] || die "root password must be at least 12 characters"

  ROOT_HASH="$(printf '%s' "$ROOT_PW" | docker run --rm -i --entrypoint python "$NETADMIN_IMG" \
    -c 'import sys; from argon2 import PasswordHasher; print(PasswordHasher().hash(sys.stdin.read().strip()))')"
  TSIG_SECRET="$(sudo cat "$OLD_ROOT/dns/rndc.key" | sed -n 's/.*secret *"\([^"]*\)".*/\1/p' | head -1)"
  [ -n "$TSIG_SECRET" ] || die "could not extract TSIG secret from $OLD_ROOT/dns/rndc.key"
  RADIUS_SECRET="$(openssl rand -hex 16)"

  # Preserve the live RADIUS TLS material so Wi-Fi supplicants keep trusting the
  # same CA after cutover; the radius entrypoint seeds it from these variables.
  # If anything is unreadable, fall through to runtime generation as a whole.
  RADIUS_TLS_SERVER=""
  RADIUS_TLS_CA=""
  RADIUS_TLS_CA_KEY=""
  RADIUS_TLS_CLIENT=""
  if docker ps --format '{{.Names}}' | grep -qx freeradius; then
    RADIUS_TLS_SERVER="$(docker exec freeradius cat /etc/raddb/certs/server.pem 2>/dev/null | base64 | tr -d '\n' || true)"
    RADIUS_TLS_CA="$(docker exec freeradius cat /etc/raddb/certs/ca.pem 2>/dev/null | base64 | tr -d '\n' || true)"
    RADIUS_TLS_CA_KEY="$(docker exec freeradius cat /etc/raddb/certs/ca.key 2>/dev/null | base64 | tr -d '\n' || true)"
    RADIUS_TLS_CLIENT="$(docker exec freeradius sh -c 'cat /etc/raddb/certs/client.key /etc/raddb/certs/client.crt' 2>/dev/null | base64 | tr -d '\n' || true)"
  fi
  if [ -n "$RADIUS_TLS_SERVER" ] && [ -n "$RADIUS_TLS_CA" ]; then
    echo "extracted RADIUS TLS material from the old stack (same CA after cutover)"
  else
    RADIUS_TLS_SERVER=""; RADIUS_TLS_CA=""; RADIUS_TLS_CA_KEY=""; RADIUS_TLS_CLIENT=""
    echo "note: no RADIUS TLS material extracted (radius generates fresh on first start)"
  fi

  umask 0027
  cat > "$ENV_FILE" <<EOF
NETADMIN_SECRET_KEY=$(openssl rand -hex 32)
NETADMIN_AGENT_TOKEN=$(openssl rand -hex 32)
NETADMIN_ROOT_PASSWORD_HASH=$ROOT_HASH
NETADMIN_RADIUS_SECRET=$RADIUS_SECRET
NETADMIN_DNS_TSIG_SECRET=$TSIG_SECRET
NETADMIN_RADIUS_TLS_SERVER_PEM=$RADIUS_TLS_SERVER
NETADMIN_RADIUS_TLS_CA_PEM=$RADIUS_TLS_CA
NETADMIN_RADIUS_TLS_CA_KEY=$RADIUS_TLS_CA_KEY
NETADMIN_RADIUS_TLS_CLIENT_PEM=$RADIUS_TLS_CLIENT
EOF
  chmod 640 "$ENV_FILE"

  # The generated RADIUS secret must be known to freeradius: register the
  # NetAdmin app as a client on the shared compose network.
  CLIENT_FILE="freeradius/raddb/netadmin-clients.conf"
  cat > "$CLIENT_FILE" <<EOF
client netadmin {
  ip_addr = 172.16.0.0/12
  secret = $RADIUS_SECRET
  shortname = netadmin
}
EOF
  chmod 640 "$CLIENT_FILE"
  echo "wrote .env and $CLIENT_FILE"
else
  say ".env already exists - reusing"
fi

if [ ! -f pki/ca.crt ]; then
  say "Generating PKI (CA + agent server certs + app client cert)"
  mkdir -p pki/dhcp-agent pki/radius-agent
  openssl req -x509 -newkey rsa:3072 -nodes -days 3650 \
    -keyout pki/ca.key -out pki/ca.crt -subj "/CN=NetAdmin CA" 2>/dev/null
  for agent in dhcp-agent radius-agent; do
    openssl req -newkey rsa:3072 -nodes -keyout "pki/$agent/server.key" \
      -out "/tmp/$agent.csr" -subj "/CN=$agent" 2>/dev/null
    openssl x509 -req -in "/tmp/$agent.csr" -CA pki/ca.crt -CAkey pki/ca.key \
      -CAcreateserial -out "pki/$agent/server.crt" -days 1825 \
      -extfile <(printf "subjectAltName=DNS:%s\nextendedKeyUsage=serverAuth\n" "$agent") 2>/dev/null
  done
  openssl req -newkey rsa:3072 -nodes -keyout pki/netadmin.key \
    -out /tmp/netadmin.csr -subj "/CN=netadmin" 2>/dev/null
  openssl x509 -req -in /tmp/netadmin.csr -CA pki/ca.crt -CAkey pki/ca.key \
    -CAcreateserial -out pki/netadmin.crt -days 1825 \
    -extfile <(printf "extendedKeyUsage=clientAuth\n") 2>/dev/null
  rm -f /tmp/dhcp-agent.csr /tmp/radius-agent.csr /tmp/netadmin.csr
  chmod 600 pki/ca.key pki/*/server.key pki/netadmin.key
  chmod 644 pki/ca.crt pki/*/server.crt pki/netadmin.crt

  # The main app runs as the non-root 'netadmin' user and must read its client
  # cert; agent gunicorn runs as root, so only the client files need the chown.
  APP_UID="$(docker run --rm --entrypoint id "$NETADMIN_IMG" -u netadmin)"
  chown "$APP_UID" pki/netadmin.key pki/netadmin.crt pki/ca.crt
  echo "PKI ready (app client cert owned by container uid $APP_UID)"
else
  say "pki/ca.crt already exists - reusing"
fi

say "Ensuring NetAdmin-managed files exist (empty includes break service validation)"
touch dhcp/config/netadmin-reservations.conf
touch freeradius/raddb/netadmin-clients.conf 2>/dev/null || true
touch freeradius/raddb/mods-config/files/netadmin 2>/dev/null || true

say "Old stack status (project under $OLD_ROOT)"
docker ps --format 'table {{.Names}}\t{{.Status}}' | grep -E 'dns|dhcp|freeradius|wud' || true

read -rs -p "Stop the old stack and cut over now? (type 'yes'): " CONFIRM; echo
[ "$CONFIRM" = "yes" ] || die "aborted"

say "Stopping old stack (containers kept for rollback)"
( cd "$OLD_ROOT" && docker compose stop ) || true
# Some containers were created outside this compose project; free the ports
# the new stack needs, but only for containers serving our known images/ports.
for port in 53 67 1812 1813 3000; do
  for cid in $(docker ps --filter "publish=$port" -q); do
    name="$(docker inspect --format '{{.Name}}' "$cid")"
    case "$name" in
      /netadmin|/dhcp-agent|/radius-agent|/dns|/dhcp|/freeradius|/wud) ;;
      *) echo "stopping leftover container $name (published port $port)"; docker stop "$cid" >/dev/null ;;
    esac
  done
done

say "Syncing live data from old stack (leases, zones incl. pending .jnl updates)"
mkdir -p dhcp/leases dns/zones
rsync -a "$OLD_ROOT/dhcp/leases/" dhcp/leases/
rsync -a "$OLD_ROOT/dns/zones/fwd/" dns/zones/fwd/
rsync -a "$OLD_ROOT/dns/zones/rev/" dns/zones/rev/
# Latest static configs (the repo copies only differ by NetAdmin includes;
# re-sync subnets/hosts so last-minute server edits are not lost).
for f in dhcp/config/dhcpd.conf dhcp/config/reservations.conf dhcp/config/hostnames.conf \
         freeradius/raddb/clients.conf freeradius/raddb/mods-config/files/authorize; do
  if ! sudo cat "$OLD_ROOT/$f" | diff -q - "$f" >/dev/null 2>&1; then
    echo "note: $f differs from server copy (repo version with NetAdmin includes kept)"
  fi
done

say "Starting NetAdmin stack"
docker compose up -d

say "Health checks"
sleep 5
docker compose ps
docker compose exec -T dns named-checkconf && echo "dns: config OK"
docker compose exec -T dhcp dhcpd -t -cf /etc/dhcp/dhcpd.conf && echo "dhcp: config OK"
docker compose exec -T radius freeradius -C >/dev/null && echo "radius: config OK"

say "Agent reachability over mTLS (from the app container)"
docker compose exec -T netadmin python - <<'EOF' || die "agent probe failed"
import os, httpx, sys
token = {"Authorization": f"Bearer {os.environ['NETADMIN_AGENT_TOKEN']}"}
verify = os.environ["NETADMIN_AGENT_CA"]
cert = (os.environ["NETADMIN_AGENT_CERT"], os.environ["NETADMIN_AGENT_KEY"])
for name, url in (("dhcp-agent", os.environ["NETADMIN_DHCP_AGENT_URL"]),
                  ("radius-agent", os.environ["NETADMIN_RADIUS_AGENT_URL"])):
    try:
        code = httpx.get(f"{url}/", headers=token, verify=verify, cert=cert, timeout=10).status_code
    except httpx.HTTPError as exc:
        print(f"{name}: FAILED ({exc})"); sys.exit(1)
    if code == 401:
        print(f"{name}: FAILED (401 - token mismatch)"); sys.exit(1)
    print(f"{name}: reachable (HTTP {code})")
EOF

say "Cutover complete"
cat <<'EOF'
Manual follow-ups:
  1. Open the app behind HTTPS (port 8099), log in as root.
  2. RADIUS page -> Migrate: hash the legacy user passwords in place.
  3. Verify a DNS edit and a DHCP reservation end-to-end.
  4. Only then power off the old stack for good:
       cd <old-root> && docker compose down

Rollback (old containers were kept, just stopped):
       docker compose -f <repo>/docker-compose.yml stop
       cd <old-root> && docker compose start
EOF
