"""Restricted ISC DHCPD configuration agent."""

from __future__ import annotations

import ipaddress
import os
import re
import subprocess  # nosec B404
from pathlib import Path
from typing import Any

from flask import Flask, jsonify

from netadmin.agents.common import (
    create_agent,
    json_object,
    validated_transaction,
)
from netadmin.services.dhcp_files import (
    discover_files,
    kea_export,
    remove_reservation,
    subnets,
    update_subnet,
)
from netadmin.services.dhcp_files import (
    reservations as file_reservations,
)

HOST_PATTERN = re.compile(r"host\s+([A-Za-z0-9_-]+)\s*\{(.*?)\}", re.DOTALL)
MAC_PATTERN = re.compile(r"hardware\s+ethernet\s+([0-9A-Fa-f:]{17})\s*;")
IP_PATTERN = re.compile(r"fixed-address\s+([0-9.]+)\s*;")
LEASE_PATTERN = re.compile(r"lease\s+([0-9.]+)\s*\{(.*?)\}", re.DOTALL)


def parse_reservations(text: str) -> list[dict[str, str]]:
    """Parse the supported ISC host reservation subset."""
    result = []
    for name, body in HOST_PATTERN.findall(text):
        mac = MAC_PATTERN.search(body)
        address = IP_PATTERN.search(body)
        if mac and address:
            result.append({"name": name, "mac": mac.group(1).lower(), "address": address.group(1)})
    return result


def render_reservations(items: list[dict[str, str]]) -> str:
    """Render deterministic NetAdmin-managed reservations."""
    blocks = [
        f"host {item['name']} {{\n  hardware ethernet {item['mac']};\n"
        f"  fixed-address {item['address']};\n}}"
        for item in sorted(items, key=lambda value: value["name"])
    ]
    return "# Managed by NetAdmin. Manual changes may be replaced.\n\n" + "\n\n".join(blocks) + "\n"


def parse_leases(text: str) -> list[dict[str, str]]:
    """Parse useful fields from the ISC lease database."""
    leases: list[dict[str, str]] = []
    for address, body in LEASE_PATTERN.findall(text):
        entry = {"address": address}
        for key, pattern in {
            "state": r"binding state\s+([^;]+);",
            "mac": r"hardware ethernet\s+([^;]+);",
            "hostname": r'client-hostname\s+"([^"]+)";',
            "ends": r"ends\s+([^;]+);",
        }.items():
            match = re.search(pattern, body)
            entry[key] = match.group(1) if match else ""
        leases.append(entry)
    return leases


def _validate_item(item: dict[str, Any]) -> dict[str, str]:
    name = str(item.get("name", ""))
    mac = str(item.get("mac", "")).lower()
    address = str(item.get("address", ""))
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise ValueError("Invalid host name")
    if not re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", mac):
        raise ValueError("Invalid MAC address")
    ipaddress.IPv4Address(address)
    return {"name": name, "mac": mac, "address": address}


def create_dhcp_agent() -> Flask:
    """Create the DHCP agent application."""
    app = create_agent(__name__)
    managed = Path(os.getenv("DHCP_MANAGED_FILE", "/etc/dhcp/netadmin-reservations.conf"))
    main_config = Path(os.getenv("DHCP_MAIN_CONFIG", "/etc/dhcp/dhcpd.conf"))
    leases = Path(os.getenv("DHCP_LEASES_FILE", "/var/lib/dhcp/dhcpd.leases"))
    reload_command = os.getenv("DHCP_RELOAD_COMMAND", "").split()

    def current() -> list[dict[str, str]]:
        return parse_reservations(managed.read_text(encoding="utf-8")) if managed.exists() else []

    def all_reservations() -> list[dict[str, str]]:
        result = []
        for path in discover_files(main_config):
            result.extend(file_reservations(path.read_text(encoding="utf-8"), str(path)))
        return result

    def validate_live() -> None:
        subprocess.run(  # noqa: S603
            ["/usr/sbin/dhcpd", "-t", "-4", "-cf", str(main_config)],
            check=True,
            capture_output=True,
            timeout=20,
        )

    @app.get("/health")
    def health() -> Any:
        return jsonify({"status": "ok"})

    @app.get("/reservations")
    def reservations() -> Any:
        return jsonify(all_reservations())

    @app.get("/leases")
    def lease_list() -> Any:
        return jsonify(parse_leases(leases.read_text(encoding="utf-8")))

    @app.post("/reservations")
    def save_reservation() -> Any:
        item = _validate_item(json_object())
        all_items = [entry for entry in all_reservations() if entry["name"] != item["name"]]
        if any(
            entry["mac"] == item["mac"] or entry["address"] == item["address"]
            for entry in all_items
        ):
            return jsonify({"error": "Duplicate MAC or address"}), 409
        items = [entry for entry in current() if entry["name"] != item["name"]]
        items.append(item)
        changes = {managed: render_reservations(items)}
        for existing in all_reservations():
            if existing["name"] == item["name"] and Path(existing["source"]) != managed.resolve():
                source = Path(existing["source"])
                changes[source] = remove_reservation(
                    source.read_text(encoding="utf-8"), item["name"]
                )
        validated_transaction(changes, validate_live, reload_command)
        return jsonify(item)

    @app.post("/reservations/delete")
    def delete_reservation() -> Any:
        name = str(json_object().get("name", ""))
        items = [entry for entry in current() if entry["name"] != name]
        changes = {managed: render_reservations(items)}
        for existing in all_reservations():
            if existing["name"] == name and Path(existing["source"]) != managed.resolve():
                source = Path(existing["source"])
                changes[source] = remove_reservation(source.read_text(encoding="utf-8"), name)
        validated_transaction(changes, validate_live, reload_command)
        return jsonify({"deleted": name})

    @app.get("/configuration")
    def configuration() -> Any:
        return jsonify(subnets(main_config.read_text(encoding="utf-8")))

    @app.post("/configuration")
    def save_configuration() -> Any:
        body = json_object()
        network = str(body.get("network", ""))
        ranges = body.get("ranges", [])
        options = body.get("options", {})
        if not isinstance(ranges, list) or not isinstance(options, dict):
            return jsonify({"error": "ranges and options have invalid types"}), 400
        updated = update_subnet(main_config.read_text(encoding="utf-8"), network, ranges, options)
        validated_transaction({main_config: updated}, validate_live, reload_command)
        return jsonify({"network": network})

    @app.get("/export/kea")
    def export_kea() -> Any:
        return jsonify(
            kea_export(subnets(main_config.read_text(encoding="utf-8")), all_reservations())
        )

    return app


def main() -> None:
    """Run the development agent server."""
    create_dhcp_agent().run(host="0.0.0.0", port=8081)  # noqa: S104
