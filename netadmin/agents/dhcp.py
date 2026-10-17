"""Restricted ISC DHCPD configuration agent."""

from __future__ import annotations

import ipaddress
import os
import re
import subprocess  # nosec B404
from pathlib import Path
from typing import Any, cast

from flask import Flask, current_app, jsonify

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


def _current(managed: Path) -> list[dict[str, str]]:
    """Managed reservations, empty when the file does not exist yet."""
    return parse_reservations(managed.read_text(encoding="utf-8")) if managed.exists() else []


def _all_reservations(main_config: Path) -> list[dict[str, str]]:
    """Reservations across the main config and every include."""
    result = []
    for path in discover_files(main_config):
        result.extend(file_reservations(path.read_text(encoding="utf-8"), str(path)))
    return result


def _validate_live(main_config: Path) -> None:
    subprocess.run(  # noqa: S603
        ["/usr/sbin/dhcpd", "-t", "-4", "-cf", str(main_config)],
        check=True,
        capture_output=True,
        timeout=20,
    )


def _duplicate(entries: list[dict[str, str]], item: dict[str, str]) -> bool:
    """True when another reservation already uses this MAC or address."""
    return any(
        entry["mac"] == item["mac"] or entry["address"] == item["address"] for entry in entries
    )


def _external_changes(entries: list[dict[str, str]], managed: Path, name: str) -> dict[Path, str]:
    """Removals of the same host from files the managed file does not own."""
    changes = {}
    for existing in entries:
        if existing["name"] == name and Path(existing["source"]) != managed.resolve():
            source = Path(existing["source"])
            changes[source] = remove_reservation(source.read_text(encoding="utf-8"), name)
    return changes


def _path(key: str) -> Path:
    """Read a configured agent path from the application config."""
    return cast(Path, current_app.config[key])


def _reload() -> list[str]:
    """Read the configured reload command from the application config."""
    return cast(list[str], current_app.config["DHCP_RELOAD"])


def _health() -> Any:
    return jsonify({"status": "ok"})


def _reservations() -> Any:
    return jsonify(_all_reservations(_path("DHCP_MAIN")))


def _lease_list() -> Any:
    return jsonify(parse_leases(_path("DHCP_LEASES").read_text(encoding="utf-8")))


def _save_reservation() -> Any:
    item = _validate_item(json_object())
    managed = _path("DHCP_MANAGED")
    main_config = _path("DHCP_MAIN")
    all_items = _all_reservations(main_config)
    others = [entry for entry in all_items if entry["name"] != item["name"]]
    if _duplicate(others, item):
        return jsonify({"error": "Duplicate MAC or address"}), 409
    items = [entry for entry in _current(managed) if entry["name"] != item["name"]]
    items.append(item)
    changes = {managed: render_reservations(items)}
    changes.update(_external_changes(all_items, managed, item["name"]))
    validated_transaction(changes, lambda: _validate_live(main_config), _reload())
    return jsonify(item)


def _delete_reservation() -> Any:
    name = str(json_object().get("name", ""))
    managed = _path("DHCP_MANAGED")
    main_config = _path("DHCP_MAIN")
    kept = [entry for entry in _current(managed) if entry["name"] != name]
    changes = {managed: render_reservations(kept)}
    changes.update(_external_changes(_all_reservations(main_config), managed, name))
    validated_transaction(changes, lambda: _validate_live(main_config), _reload())
    return jsonify({"deleted": name})


def _configuration() -> Any:
    return jsonify(subnets(_path("DHCP_MAIN").read_text(encoding="utf-8")))


def _save_configuration() -> Any:
    body = json_object()
    network = str(body.get("network", ""))
    ranges = body.get("ranges", [])
    options = body.get("options", {})
    if not isinstance(ranges, list) or not isinstance(options, dict):
        return jsonify({"error": "ranges and options have invalid types"}), 400
    main_config = _path("DHCP_MAIN")
    updated = update_subnet(main_config.read_text(encoding="utf-8"), network, ranges, options)
    validated_transaction({main_config: updated}, lambda: _validate_live(main_config), _reload())
    return jsonify({"network": network})


def _export_kea() -> Any:
    main_config = _path("DHCP_MAIN")
    return jsonify(
        kea_export(
            subnets(main_config.read_text(encoding="utf-8")),
            _all_reservations(main_config),
        )
    )


def create_dhcp_agent() -> Flask:
    """Create the DHCP agent application."""
    app = create_agent(__name__)
    app.config.update(
        DHCP_MANAGED=Path(os.getenv("DHCP_MANAGED_FILE", "/etc/dhcp/netadmin-reservations.conf")),
        DHCP_MAIN=Path(os.getenv("DHCP_MAIN_CONFIG", "/etc/dhcp/dhcpd.conf")),
        DHCP_LEASES=Path(os.getenv("DHCP_LEASES_FILE", "/var/lib/dhcp/dhcpd.leases")),
        DHCP_RELOAD=os.getenv("DHCP_RELOAD_COMMAND", "").split(),
    )
    app.add_url_rule("/health", view_func=_health, methods=["GET"])
    app.add_url_rule("/reservations", view_func=_reservations, methods=["GET"])
    app.add_url_rule("/leases", view_func=_lease_list, methods=["GET"])
    app.add_url_rule("/reservations", view_func=_save_reservation, methods=["POST"])
    app.add_url_rule("/reservations/delete", view_func=_delete_reservation, methods=["POST"])
    app.add_url_rule("/configuration", view_func=_configuration, methods=["GET"])
    app.add_url_rule("/configuration", view_func=_save_configuration, methods=["POST"])
    app.add_url_rule("/export/kea", view_func=_export_kea, methods=["GET"])
    return app


def main() -> None:
    """Run the development agent server."""
    # The container listens on every interface; mTLS and the token gate access.
    create_dhcp_agent().run(host="0.0.0.0", port=8081)  # noqa
