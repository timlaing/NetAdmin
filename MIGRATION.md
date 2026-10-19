# Migrating from ISC dhcpd to Kea — runbook

This is the migration-day procedure for a stack where NetAdmin already manages ISC
dhcpd (`docker-compose.yml`). It converts the live configuration and lease database to
the Kea layout and switches the `dhcp` service over. Everything is driven by
`scripts/migrate-to-kea.sh`; see `--help` for the full flag list.

> Running NetAdmin yet? Use `scripts/cutover.sh` first (pre-NetAdmin migration), then
> this runbook.

Both compose files use the same project and service names, so the two backends are
mutually exclusive by design: `docker compose -f <file> up -d` switches the `dhcp`,
`dhcp-agent` and `netadmin` services to that file's backend and leaves the shared
services (`dns`, `radius`, agents) alone.

## Files involved

| Purpose                                           | ISC (`docker-compose.yml`)          | Kea (`docker-compose.kea.yml`)                                    |
| ------------------------------------------------- | ----------------------------------- | ----------------------------------------------------------------- |
| Service config (mounted at `/etc/dhcp`)           | `dhcp/config/dhcpd.conf` + includes | `dhcp/config/kea4.conf`, `dhcp/config/netadmin-reservations.json` |
| Lease database (mounted at `/var/lib/{dhcp,kea}`) | `dhcp/leases/dhcpd.leases`          | `dhcp/leases/dhcp4.leases`                                        |

The file names do not collide, so both backends' files coexist in `dhcp/` — that is
what makes switching back and forth cheap during the soak.

## 1. Prepare (days before the window)

1. **Back up the live state** (and keep the copy off the machine):

   ```sh
   cp -a dhcp dhcp-backup-$(date +%Y%m%d)
   ```

2. **Stage a dry run** and read what would be produced:

   ```sh
   scripts/migrate-to-kea.sh \
     --config dhcp/config/dhcpd.conf \
     --out-dir staging \
     --leases dhcp/leases/dhcpd.leases \
     --interfaces eth0 \
     --dry-run
   ```

   Drop `--dry-run` to write the files into `staging/` instead of printing them. Match
   the knobs to your network:

   - `--interfaces` — interfaces Kea binds **as the container sees them** (the
     compose deployment's default `eth0` is usually right; host-network setups differ).
   - `--lifetime` / `--max-lifetime` — set these to your ISC
     `default-lease-time` / `max-lease-time` (the defaults are 3600/7200).
   - `--no-d2` — only if your dhcpd.conf never did dynamic DNS updates.
   - Defaults for `--include-path` and `--lease-db` match `docker-compose.kea.yml`
     exactly; change them only if you changed the mounts.

3. **Review the staged output:**

   - `kea4.conf` — every subnet present, `reservations-global: true`, your global
     options inherited into each subnet (subnet-local values win), `dhcp-ddns` block
     present/absent as chosen.
   - `netadmin-reservations.json` — every host declaration from every included file,
     folded in. Compare against your legacy host files; nothing may be missing.
   - `dhcp4.leases` — header plus one row per **active/released** lease. Stale
     (`free`/`declined`) leases are deliberately dropped.

   The wrapper finishes by running the real `kea-dhcp4 -t` against the staged config.

4. **Resolve aborts.** The tool refuses to write anything when it finds a duplicate
   MAC or address across host files, an `include` that points at a missing file, or a
   config with no subnets. Fix the legacy files and re-run until it is clean.

5. **Pre-build the Kea images** so the cutover itself takes seconds:

   ```sh
   docker compose -f docker-compose.kea.yml build
   ```

## 2. Cutover (the window)

DHCP is down for the duration — keep it short.

1. Stop the ISC daemon (leases become quiescent):

   ```sh
   docker compose stop dhcp
   ```

2. Re-run the migration against the now-final lease file:

   ```sh
   scripts/migrate-to-kea.sh \
     --config dhcp/config/dhcpd.conf \
     --out-dir staging \
     --leases dhcp/leases/dhcpd.leases \
     --interfaces eth0
   ```

3. Install the staged output into the mounted paths:

   ```sh
   cp staging/kea4.conf staging/netadmin-reservations.json dhcp/config/
   cp staging/dhcp4.leases dhcp/leases/dhcp4.leases
   ```

4. Start the Kea stack:

   ```sh
   docker compose -f docker-compose.kea.yml up -d
   ```

   This recreates `dhcp` (Kea image), `dhcp-agent` (`DHCP_SERVER=kea`) and `netadmin`
   (`NETADMIN_DHCP_BACKEND=kea`). The `dns` and `radius` containers are untouched.

5. Watch the daemon come up:

   ```sh
   docker logs -f dhcp
   ```

   Expect the rndc-key wait to resolve, then `DHCP4_STARTED` and no `ERROR` lines.
   The entrypoint starts DHCP-DDNS alongside; `kea-dhcp4` is PID 1.

## 3. Verify

- **Renewal:** force a lease renewal on a test client (`dhclient -r && dhclient`, or
  toggle Wi-Fi on a phone) and confirm it keeps its address. Converted active leases
  are honoured; reserved hosts get their `fixed-address`.
- **Leases land in the memfile:**

  ```sh
  grep '^<client-ip>,' dhcp/leases/dhcp4.leases
  ```

- **Dynamic DNS:** renew a host with a hostname and check the forward zone
  (`dig @localhost <name>`), plus `docker logs dhcp | grep -i ddns`.
- **NetAdmin UI:** the DHCP page shows the backend as Kea and lists the folded
  reservations. Edit one — the agent validates with `kea-dhcp4 -t` and reloads the
  daemon over `/run/kea/kea4-ctrl.sock`.
- **Restart survival:** `docker compose restart dhcp`, then confirm another renewal
  works (the lease file persists in `dhcp/leases/`).

## 4. Rollback (any time during the soak)

Switch back exactly the way you switched over — an `up -d`, never a `down`:

```sh
docker compose up -d
```

That restores `dhcp` (isc image), `dhcp-agent` (ISC env) and `netadmin`
(`NETADMIN_DHCP_BACKEND=isc`). Your `dhcpd.leases` was not touched while Kea ran, so
ISC resumes with its last-known leases; clients that obtained a Kea lease in the
meantime get NAKed once and re-DISCOVER (seconds of churn for them only).

> **Never `docker compose -f docker-compose.kea.yml down` to "switch back"** — that
> removes every service the file defines, including `dns` and `radius`. Backend
> switches are always `up -d` with the desired file.

## 5. Soak, then decommission

Keep the ISC images and `dhcp/config/dhcpd*` files for at least one normal lease
cycle (ideally a full `max-lease-time`, longer if you have devices that rarely
renew). Decommissioning the ISC side is a deliberate, separate decision once you are
confident — do not automate it as part of the cutover.

For Home Assistant add-on front ends: set the add-on's `dhcp_backend` option to `kea`
and restart the add-on so the web app tracks the backend; the daemon-side switch is
still the compose procedure above.
