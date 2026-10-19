"""One-shot ISC DHCPD to Kea DHCPv4 configuration migration.

Converts a live ISC dhcpd configuration tree (the main config plus every
``include``) into the Kea layout the NetAdmin agent manages at runtime: a
rendered ``kea4.conf`` plus a single folded ``netadmin-reservations.json``
that the agent owns. Optionally converts the ISC lease database into a Kea
memfile CSV so active bindings survive the cutover.

Everything is driven by explicit arguments (no host-specific defaults baked
in), so the tool works for any network. It reuses the proven ISC parsing in
:mod:`netadmin.services.dhcp_files` and the Kea rendering in
:mod:`netadmin.services.kea_files`, and needs only the standard library.

Run it directly (``python -m netadmin.services.kea_migrate``) or through the
``scripts/migrate-to-kea.sh`` wrapper, which adds daemon-side validation.
"""

from __future__ import annotations

import argparse
import csv
import io
import ipaddress
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from netadmin.services import dhcp_files, kea_files

# Value substituted for the ``<?include?>`` directive while the config is a
# plain JSON document; render_config turns it back into Kea extended JSON.
_INCLUDE_MARKER = "@@NETADMIN_INCLUDE@@"
_INCLUDE_KEY = "__include__"

# A lease block closes with ``}`` at column 0; inner statements (failover
# peer state) nest with indentation, so the terminator is newline-brace.
_LEASE_BLOCK = re.compile(r"lease\s+([0-9.]+)\s*\{(.*?)\n\}", re.DOTALL)
_LEASE_STARTS = re.compile(r"starts\s+\d+\s+([0-9/]+)\s+([0-9:]+)\s*;")
_LEASE_ENDS = re.compile(r"ends\s+\d+\s+([0-9/]+)\s+([0-9:]+)\s*;")
_LEASE_STATE = re.compile(r"binding state\s+(\w+)\s*;")
_LEASE_MAC = re.compile(r"hardware ethernet\s+([0-9A-Fa-f:]{17})\s*;")
_LEASE_HOSTNAME = re.compile(r'client-hostname\s+"([^"]*)"\s*;')
_LEASE_CLIENT_ID = re.compile(r"client id\s+([0-9A-Fa-f:.]+)\s*;")

# Kea memfile STATE_* codes (src/lib/dhcpsrv/lease.h) for the ISC binding
# states worth preserving across a cutover.
_STATE_CODES = {"active": "0", "declined": "1", "expired": "2", "released": "3"}
# Only live bindings are migrated; free/expired/abandoned leases are stale and
# will be re-issued fresh by Kea.
_PRESERVED_STATES = {"active", "released"}

# The exact column order Kea 3.0.3's memfile writes (verified against a live
# daemon): trailing user_context and pool_id columns included. Both fqdn flags
# and pool_id are lexical-cast to numbers on load, so they must be digits —
# an empty pool_id makes the daemon discard the row. 11+ columns are required
# (user_context is read unconditionally).
_MEMFILE_HEADER = [
    "address",
    "hwaddr",
    "client_id",
    "valid_lifetime",
    "expire",
    "subnet_id",
    "fqdn_fwd",
    "fqdn_rev",
    "hostname",
    "state",
    "user_context",
    "pool_id",
]


def collect(config: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Parse an ISC config tree into ``(subnets, reservations)``.

    Subnets keep the ISC shape (``network``, ``netmask``, ``ranges``,
    ``options``); reservations carry ``name``, ``mac``, ``address`` and the
    ``source`` file they were declared in.
    """
    subnets: list[dict[str, Any]] = []
    hosts: list[dict[str, str]] = []
    global_opts: dict[str, str] = {}
    if not config.is_file():
        raise FileNotFoundError(f"ISC config not found: {config}")
    for path in dhcp_files.discover_files(config):
        text = path.read_text(encoding="utf-8")
        # The agent tolerates missing includes at runtime; a migration must not,
        # or a broken path would silently drop subnets or reservations.
        for include in dhcp_files.INCLUDE.findall(text):
            target = Path(include) if Path(include).is_absolute() else path.parent / include
            if not target.is_file():
                raise FileNotFoundError(f"include not found: {include} (referenced by {path})")
        subnets.extend(dhcp_files.subnets(text))
        hosts.extend(dhcp_files.reservations(text, str(path)))
        for name, value in dhcp_files.global_options(text).items():
            global_opts.setdefault(name, value)
    # ISC applies top-level options to every subnet unless the subnet
    # overrides them; Kea needs them per-subnet, so seed them here.
    for subnet in subnets:
        subnet["options"] = {**global_opts, **subnet["options"]}
    return subnets, hosts


def fold(hosts: list[dict[str, str]]) -> list[dict[str, str]]:
    """Dedupe reservations by name (first wins) and sort deterministically."""
    seen: dict[str, dict[str, str]] = {}
    for host in hosts:
        seen.setdefault(host["name"], host)
    return sorted(seen.values(), key=lambda item: item["name"])


def conflicts(items: list[dict[str, str]]) -> list[str]:
    """Report duplicate MAC or address assignments across distinct names.

    The agent rejects these at runtime, so a migration that carried them over
    would produce a reservation set the daemon refuses. Operators resolve them
    before the cutover.
    """
    problems: list[str] = []
    by_mac: dict[str, str] = {}
    by_address: dict[str, str] = {}
    for item in items:
        name, mac, address = item["name"], item["mac"], item["address"]
        if mac in by_mac and by_mac[mac] != name:
            problems.append(f"MAC {mac} shared by {by_mac[mac]} and {name}")
        if address in by_address and by_address[address] != name:
            problems.append(f"address {address} shared by {by_address[address]} and {name}")
        by_mac.setdefault(mac, name)
        by_address.setdefault(address, name)
    return problems


def build_config(
    subnets: list[dict[str, Any]],
    *,
    interfaces: list[str],
    lease_db: str,
    lifetime: int,
    max_lifetime: int,
    enable_d2: bool,
    socket_name: str,
) -> dict[str, Any]:
    """Assemble the Kea ``Dhcp4`` configuration for a set of ISC subnets.

    Reservations are deliberately left to the folded include file (the agent
    manages that single global set), so ``subnet4`` entries carry only pools
    and options and ``reservations-global`` is set.
    """
    subnet4 = [
        {
            "id": index + 1,
            "subnet": str(ipaddress.ip_network(f"{item['network']}/{item['netmask']}")),
            "pools": [{"pool": f"{start} - {end}"} for start, end in item["ranges"]],
            "option-data": [
                {"name": name, "data": value.strip('"')}
                for name, value in sorted(item["options"].items())
            ],
        }
        for index, item in enumerate(subnets)
    ]
    dhcp4: dict[str, Any] = {
        "interfaces-config": {"interfaces": interfaces, "dhcp-socket-type": "udp"},
        "lease-database": {"type": "memfile", "persist": True, "name": lease_db},
        "reservations-global": True,
        "valid-lifetime": lifetime,
        "max-valid-lifetime": max_lifetime,
        "authoritative": True,
    }
    if enable_d2:
        dhcp4["dhcp-ddns"] = {"enable-updates": True, "ncr-protocol": "UDP", "ncr-format": "JSON"}
    dhcp4["subnet4"] = subnet4
    dhcp4[_INCLUDE_KEY] = _INCLUDE_MARKER
    dhcp4["control-sockets"] = [{"socket-type": "unix", "socket-name": socket_name}]
    dhcp4["loggers"] = [{"name": "*", "severity": "INFO"}]
    return {"Dhcp4": dhcp4}


def render_config(config: dict[str, Any], include_path: str) -> str:
    """Render the config as Kea extended JSON with the include spliced in.

    Kea's ``<?include "file"?>`` splices a bare member into the surrounding
    object, so the marker member becomes the include directive (the trailing
    comma json.dumps emits keeps it a valid non-final member).
    """
    text = json.dumps(config, indent=2)
    directive = f'<?include "{include_path}"?>'
    return text.replace(f'"{_INCLUDE_KEY}": "{_INCLUDE_MARKER}"', directive) + "\n"


def subnet_id_for(address: str, subnets: list[dict[str, Any]]) -> int:
    """Return the 1-based subnet id whose CIDR contains address, else 0."""
    target = ipaddress.IPv4Address(address)
    for index, item in enumerate(subnets):
        network = ipaddress.ip_network(f"{item['network']}/{item['netmask']}")
        if target in network:
            return index + 1
    return 0


def _epoch(date: str, clock: str) -> int:
    """ISC lease timestamp (local wall-clock) to a unix epoch second."""
    moment = datetime.strptime(f"{date} {clock}", "%Y/%m/%d %H:%M:%S")
    return int(time.mktime(moment.timetuple()))


def convert_leases(isc_text: str, subnets: list[dict[str, Any]]) -> str:
    """Convert an ISC dhcpd lease database to a Kea memfile CSV.

    Only currently-bound leases (``active`` and ``released``) are carried over
    with their Kea ``STATE_*`` code; stale states are dropped so Kea re-issues
    them fresh. ``starts``/``ends`` are read as local wall-clock time, matching
    how dhcpd writes them, so run this on a host in the old server's timezone.
    """
    rows: list[list[str]] = []
    for address, body in _LEASE_BLOCK.findall(isc_text):
        state_match = _LEASE_STATE.search(body)
        mac_match = _LEASE_MAC.search(body)
        ends_match = _LEASE_ENDS.search(body)
        starts_match = _LEASE_STARTS.search(body)
        if not (state_match and mac_match and ends_match and starts_match):
            continue  # incomplete record — let Kea re-issue it fresh
        state = state_match.group(1)
        if state not in _PRESERVED_STATES:
            continue
        expire = _epoch(*ends_match.groups())
        started = _epoch(*starts_match.groups())
        hostname_match = _LEASE_HOSTNAME.search(body)
        client_id_match = _LEASE_CLIENT_ID.search(body)
        rows.append(
            [
                address,
                mac_match.group(1).lower(),
                client_id_match.group(1).lower() if client_id_match else "",
                str(max(expire - started, 0)),
                str(expire),
                str(subnet_id_for(address, subnets)),
                "0",
                "0",
                hostname_match.group(1) if hostname_match else "",
                _STATE_CODES[state],
                "",  # user_context (string, may be empty)
                "0",  # pool_id (numeric — empty would be discarded on load)
            ]
        )
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(_MEMFILE_HEADER)
    writer.writerows(rows)
    return buffer.getvalue()


def migrate(
    config: Path,
    *,
    include_path: str,
    interfaces: list[str],
    lease_db: str,
    lifetime: int,
    max_lifetime: int,
    enable_d2: bool,
    socket_name: str,
) -> tuple[str, str, list[dict[str, str]]]:
    """Run the full config migration.

    Returns ``(kea4_conf_text, reservations_text, folded_hosts)``. Raises
    ``ValueError`` when folded reservations carry duplicate MAC or address
    assignments, since the agent would refuse that set at runtime.
    """
    subnets, hosts = collect(config)
    folded = fold(hosts)
    clashes = conflicts(folded)
    if clashes:
        raise ValueError(
            "Conflicting reservations must be resolved first:\n  " + "\n  ".join(clashes)
        )
    if not subnets:
        raise ValueError(f"no subnets found in {config} or its includes")
    config_dict = build_config(
        subnets,
        interfaces=interfaces,
        lease_db=lease_db,
        lifetime=lifetime,
        max_lifetime=max_lifetime,
        enable_d2=enable_d2,
        socket_name=socket_name,
    )
    return (
        render_config(config_dict, include_path),
        kea_files.render_reservations(folded),
        folded,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kea_migrate",
        description="Convert an ISC dhcpd config tree into the NetAdmin-managed Kea layout.",
    )
    parser.add_argument("--config", required=True, type=Path, help="path to dhcpd.conf")
    parser.add_argument(
        "--out-dir",
        required=True,
        type=Path,
        help="directory to write kea4.conf and netadmin-reservations.json into",
    )
    parser.add_argument(
        "--leases",
        type=Path,
        help="optional path to dhcpd.leases to convert into a Kea memfile",
    )
    parser.add_argument(
        "--include-path",
        default="/etc/dhcp/netadmin-reservations.json",
        help="reservation file path as Kea will see it inside its container",
    )
    parser.add_argument(
        "--interfaces",
        default="eth0",
        help="comma-separated interfaces for Kea to bind (default: eth0)",
    )
    parser.add_argument(
        "--lease-db",
        default="/var/lib/kea/dhcp4.leases",
        help="memfile lease database path Kea will use",
    )
    parser.add_argument("--lifetime", type=int, default=3600, help="valid-lifetime seconds")
    parser.add_argument("--max-lifetime", type=int, default=7200, help="max-valid-lifetime seconds")
    parser.add_argument("--socket-name", default="kea4-ctrl.sock", help="control socket file name")
    parser.add_argument(
        "--no-d2",
        action="store_true",
        help="omit the dhcp-ddns block (no dynamic DNS updates)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the generated files to stdout instead of writing them",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns a process exit code."""
    args = _parser().parse_args(argv)
    interfaces = [name.strip() for name in args.interfaces.split(",") if name.strip()]
    try:
        config_text, reservations_text, folded = migrate(
            args.config,
            include_path=args.include_path,
            interfaces=interfaces,
            lease_db=args.lease_db,
            lifetime=args.lifetime,
            max_lifetime=args.max_lifetime,
            enable_d2=not args.no_d2,
            socket_name=args.socket_name,
        )
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    subnets, _ = collect(args.config)
    print(f"subnets: {len(subnets)}  reservations: {len(folded)}", file=sys.stderr)
    for clash in conflicts(folded):
        print(f"warning: {clash}", file=sys.stderr)

    leases_text = ""
    if args.leases:
        leases_text = convert_leases(args.leases.read_text(encoding="utf-8"), subnets)
        active = leases_text.count("\n") - 1
        print(f"leases converted: {active}", file=sys.stderr)

    if args.dry_run:
        print("=== kea4.conf ===")
        print(config_text, end="")
        print("=== netadmin-reservations.json ===")
        print(reservations_text, end="")
        if leases_text:
            print("=== dhcp4.leases ===")
            print(leases_text, end="")
        return 0

    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "kea4.conf").write_text(config_text, encoding="utf-8")
    (args.out_dir / "netadmin-reservations.json").write_text(reservations_text, encoding="utf-8")
    print(f"wrote {args.out_dir / 'kea4.conf'}", file=sys.stderr)
    print(f"wrote {args.out_dir / 'netadmin-reservations.json'}", file=sys.stderr)
    if args.leases:
        (args.out_dir / "dhcp4.leases").write_text(leases_text, encoding="utf-8")
        print(f"wrote {args.out_dir / 'dhcp4.leases'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
