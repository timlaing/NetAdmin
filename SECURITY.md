# Security Policy

## Reporting a Vulnerability

Report security issues privately through
[GitHub security advisories](https://github.com/timlaing/net-admin/security/advisories/new).
Do not open a public issue for security reports.

Include the affected component (web app, DHCP agent, RADIUS agent, add-on), the
version or commit, reproduction steps and the expected impact. You should
receive an acknowledgement within a few days.

## Scope Notes

NetAdmin administers privileged network services. When reporting, note that:

- TSIG secrets, RADIUS shared secrets and password hashes are supplied through
  environment variables and are never stored in SQLite.
- The DHCP and RADIUS agents enforce mTLS plus a bearer token, and only accept
  writes that validate against the live service configuration.
- The web application never writes service configuration itself; all privileged
  file writes happen inside the agents.
