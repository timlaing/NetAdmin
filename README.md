# NetAdmin

NetAdmin is a Flask and Bootstrap application for administering Bind 9, ISC DHCPD and
FreeRADIUS while leaving each service as the source of truth. Bind uses AXFR and RFC 2136.
Restricted DHCP and RADIUS agents validate and atomically install file-backed changes.

## Security model

- RADIUS authentication with `Filter-Id` mapped to `netadmin-admin` or `netadmin-standard`.
- Emergency `root` authentication uses an Argon2id hash from the environment.
- Standard users can only change their own password.
- Global CSRF protection, server-side one-hour sessions, strict cookies and audit records.
- Agents do not mount the Docker socket.
- Passwords and service configuration are never stored in NetAdmin's SQLite database.

## Configure and run

1. Copy `.env.example` to `.env` and replace every placeholder.
2. Add a matching RADIUS client and restricted TSIG update permission.
3. Create a private CA plus separate server certificates for both agents and a NetAdmin
   client certificate under `pki/`. Certificate SANs must match `dhcp-agent` and
   `radius-agent`.
4. Restrict private keys to the container user (`0600`) and configuration files containing
   hashes/secrets to `0640`.
5. Run `docker compose up --build -d`.
6. Put the application behind HTTPS and open port 8099 through the reverse proxy.

The cookie is always `Secure`, so plain HTTP is intentionally unsuitable for login.

For Home Assistant, open the add-on through its ingress (sidebar) panel on first run: a
setup wizard generates the secrets, hashes the emergency root password and writes
`/config/netadmin.env` (mode 0640). Restart the add-on to load it. The wizard is only
reachable through ingress and locks itself once configured; the file is never included in
the repository or options schema. To migrate an existing pre-NetAdmin stack, run
`scripts/cutover.sh` from a repo checkout on the server.

## Development

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
ruff check .
ruff format --check .
mypy
pylint netadmin
pytest
```

## CI and published containers

GitHub Actions runs the same checks plus container smoke tests on every push and
pull request (`.github/workflows/ci.yml`, `lint.yml`, `pre-commit.yml`, `codeql.yml`).
The smoke tests boot each image and verify real behaviour: login and Secure
cookies for the app, mTLS plus bearer-token enforcement for the agents, and
ingress-only access plus wizard locking for the add-on (`Tools/smoke_*.sh`).

Pushing a `vX.Y.Z` tag publishes multi-arch images to GHCR after verify and lint
gates pass (`.github/workflows/publish.yml`):

- `ghcr.io/timlaing/netadmin` — web application
- `ghcr.io/timlaing/netadmin-dhcp-agent` — restricted DHCP agent
- `ghcr.io/timlaing/netadmin-radius-agent` — restricted RADIUS agent
- `ghcr.io/timlaing/<arch>-addon-netadmin` — Home Assistant add-on

The tag must match `home-assistant/netadmin/config.yaml`'s `version`. Release
notes are drafted automatically from conventional commits by release-drafter.
