#!/usr/bin/with-contenv bashio
set -euo pipefail
if [[ -r /config/netadmin.env ]]; then
  set -a
  source /config/netadmin.env
  set +a
fi
export NETADMIN_DATA_DIR=/data
export NETADMIN_DNS_SERVER="$(bashio::config 'dns_server')"
export NETADMIN_DNS_ZONES="$(bashio::config 'dns_zones')"
export NETADMIN_DHCP_AGENT_URL="$(bashio::config 'dhcp_agent_url')"
export NETADMIN_RADIUS_AGENT_URL="$(bashio::config 'radius_agent_url')"
exec gunicorn --bind 0.0.0.0:8099 --workers 1 --access-logfile - netadmin.wsgi:app
