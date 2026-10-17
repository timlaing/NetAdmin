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
3. Run `docker compose up --build -d`.
4. Put the application behind HTTPS and open port 8099 through the reverse proxy.

The cookie is always `Secure`, so plain HTTP is intentionally unsuitable for login.

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
