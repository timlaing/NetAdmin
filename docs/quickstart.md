# Quick start

The shortest path from clone to a logged-in NetAdmin instance using the stock
Docker Compose stack (ISC DHCPD backend).

## 1. Clone and configure

```sh
git clone https://github.com/timlaing/net-admin.git
cd net-admin
cp .env.example .env
```

Edit `.env` — the quickest set that gets you running:

```sh
# Session signing key (any long random value)
NETADMIN_SECRET_KEY=$(openssl rand -hex 32)

# Bearer token shared between the web app and both agents
NETADMIN_AGENT_TOKEN=$(openssl rand -hex 32)

# Argon2id hash of the emergency root password
NETADMIN_ROOT_PASSWORD_HASH=$(python -c 'from argon2 import PasswordHasher; print(PasswordHasher().hash("choose-a-password"))')

# Shared RADIUS secret (keep your existing one if a RADIUS server already runs)
NETADMIN_RADIUS_SECRET=$(openssl rand -hex 16)
```

Everything else has working defaults or is optional — see
[Configuration](configuration.md).

## 2. Provide key material

Certificates for the two agents and the web app live under `pki/` (SANs must
match `dhcp-agent` and `radius-agent`). The TSIG and RADIUS TLS keys seed or
generate themselves on first start — details on the home page
(README → _Key material_).

## 3. Start the stack

```sh
docker compose up --build -d
docker compose ps
```

Wait for every service to be healthy. The web application listens on
**8099**.

## 4. Log in

Open NetAdmin through your HTTPS reverse proxy and authenticate as `root` with
the password you hashed above. RADIUS users (with `Filter-Id` set to
`netadmin-admin` or `netadmin-standard`) log in with their RADIUS credentials.

!!! warning "HTTPS is mandatory"
The session cookie is always `Secure`; plain HTTP login is broken on
purpose.

## Next steps

- [Configuration](configuration.md) — every environment variable, agent switch
  and add-on option.
- [ISC → Kea migration](MIGRATION.md) — runbook for moving an existing DHCP
  deployment to Kea (`docker-compose.kea.yml`).
- [Development](AGENTS.md) — architecture, check commands and repo traps.
