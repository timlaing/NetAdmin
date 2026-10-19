#!/usr/bin/env bash
# Migrate an ISC dhcpd configuration tree into the NetAdmin-managed Kea layout.
#
# Reads your dhcpd.conf (and every file it includes), folds all host
# reservations into one NetAdmin-owned file, renders a kea4.conf around them,
# and — optionally — converts your dhcpd.leases into a Kea memfile so active
# bindings survive the cutover. Nothing here is specific to one network: every
# path and knob is a flag with a sensible default.
#
# Usage:
#   scripts/migrate-to-kea.sh --config dhcp/config/dhcpd.conf \
#                             --out-dir dhcp/config \
#                             [--leases dhcp/leases/dhcpd.leases] \
#                             [--include-path /etc/dhcp/netadmin-reservations.json] \
#                             [--interfaces eth0] \
#                             [--lifetime 3600] [--max-lifetime 7200] \
#                             [--socket-name kea4-ctrl.sock] [--lease-db /var/lib/kea/dhcp4.leases] \
#                             [--no-d2] [--image netadmin-dhcp-kea:test] [--dry-run] [--no-validate]
#
# Run it from a checkout of this repo. Validation runs the real `kea-dhcp4 -t`
# in the Kea container, so docker (or --no-validate) is required.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

CONFIG="dhcp/config/dhcpd.conf"
OUT_DIR="dhcp/config"
LEASES=""
INCLUDE_PATH="/etc/dhcp/netadmin-reservations.json"
IMAGE="netadmin-dhcp-kea:test"
VALIDATE=1
DRY_RUN=0
PY_ARGS=()

say()  { printf '\n=== %s\n' "$*"; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

usage() { sed -n '3,21p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)         CONFIG="$2"; shift 2 ;;
    --out-dir)        OUT_DIR="$2"; shift 2 ;;
    --leases)         LEASES="$2"; shift 2 ;;
    --include-path)   INCLUDE_PATH="$2"; PY_ARGS+=(--include-path "$2"); shift 2 ;;
    --interfaces)     PY_ARGS+=(--interfaces "$2"); shift 2 ;;
    --lease-db)       PY_ARGS+=(--lease-db "$2"); shift 2 ;;
    --lifetime)       PY_ARGS+=(--lifetime "$2"); shift 2 ;;
    --max-lifetime)   PY_ARGS+=(--max-lifetime "$2"); shift 2 ;;
    --socket-name)    PY_ARGS+=(--socket-name "$2"); shift 2 ;;
    --image)          IMAGE="$2"; shift 2 ;;
    --no-d2)          PY_ARGS+=(--no-d2); shift ;;
    --dry-run)        DRY_RUN=1; PY_ARGS+=(--dry-run); shift ;;
    --no-validate)    VALIDATE=0; shift ;;
    -h|--help)        usage; exit 0 ;;
    *)                die "unknown flag: $1 (try --help)" ;;
  esac
done

say "Preflight"
[[ -f "$CONFIG" ]] || die "config not found: $CONFIG (pass --config)"
if [[ -n "$LEASES" && ! -f "$LEASES" ]]; then
  die "leases file not found: $LEASES (drop --leases to skip conversion)"
fi
command -v python3 >/dev/null || die "python3 not found"

# Build the argument list once so the empty-array case stays safe under `set -u`
# on older bash (macOS ships 3.2).
ARGS=(--config "$CONFIG" --out-dir "$OUT_DIR")
if [[ -n "$LEASES" ]]; then ARGS+=(--leases "$LEASES"); fi
if [[ ${#PY_ARGS[@]} -gt 0 ]]; then ARGS+=("${PY_ARGS[@]}"); fi

# NETADMIN_PYTHON overrides interpreter selection (containers, alternate
# venvs); otherwise: project venv, an installed netadmin, then the repo layout.
if [[ -n "${NETADMIN_PYTHON:-}" ]]; then
  PY="$NETADMIN_PYTHON"
elif [[ -x .venv/bin/python ]]; then
  PY=.venv/bin/python
elif python3 -c 'import netadmin' >/dev/null 2>&1; then
  PY=python3
else
  PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}" PY=python3
fi

say "Generating Kea configuration"
PYTHONPATH="${PYTHONPATH:-}" "$PY" -m netadmin.services.kea_migrate "${ARGS[@]}"

[[ "$DRY_RUN" == 0 ]] || exit 0
[[ "$VALIDATE" == 1 ]] || { say "Skipping daemon validation (--no-validate)"; exit 0; }

say "Validating generated configuration with the real daemon"
KEA_CONF="$OUT_DIR/kea4.conf"
[[ -f "$KEA_CONF" ]] || die "expected $KEA_CONF to exist after generation"
command -v docker >/dev/null 2>&1 || die "docker not found (re-run with --no-validate to skip)"

# The config references absolute container paths, so mount the output directory
# at the include path's parent and check the config from that same view. This
# keeps validation correct for any --include-path the caller chose.
MOUNT_TARGET="$(dirname "$INCLUDE_PATH")"
docker run --rm \
  --mount "type=bind,source=$(cd "$OUT_DIR" && pwd),target=$MOUNT_TARGET,readonly" \
  --entrypoint kea-dhcp4 "$IMAGE" -t "$MOUNT_TARGET/kea4.conf" \
  || die "kea-dhcp4 rejected the generated config"
echo "kea-dhcp4 -t: OK ($IMAGE)"

say "Migration generated successfully"
{
  echo "Wrote into $OUT_DIR:"
  echo "  kea4.conf                  - Kea DHCPv4 config (reservations via include)"
  echo "  netadmin-reservations.json - all folded hosts, NetAdmin-owned at runtime"
  if [[ -n "$LEASES" ]]; then
    echo "  dhcp4.leases               - converted active/released leases"
  fi
  echo
  echo "Next steps:"
  echo "  1. Point the Kea deployment at this directory (docker-compose.kea.yml"
  echo "     mounts ./dhcp/config at /etc/dhcp and the lease dir at /var/lib/kea)."
  echo "  2. Start the stack:  docker compose -f docker-compose.kea.yml up -d"
  echo "  3. Verify a DISCOVER/ACK and a DNS update, then decommission isc at your"
  echo "     leisure (the old containers are kept for rollback)."
}
