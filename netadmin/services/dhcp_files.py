"""ISC DHCPD include discovery and loss-minimizing transforms."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

INCLUDE = re.compile(r'^[ \t]*include\s+"([^"]+)"\s*;', re.MULTILINE)
HOST_START = re.compile(r"\bhost\s+([A-Za-z0-9_-]+)\s*\{")
SUBNET_START = re.compile(r"\bsubnet\s+([0-9.]+)\s+netmask\s+([0-9.]+)\s*\{")
# Possessive quantifiers keep the blank-line run linear for the regex
# analyzer; each iteration must start with \n so nothing usable is consumed.
_RANGE_LINE = re.compile(r"(?m)^[ \t]*+(?:\r?\n[ \t]*+)*+range\s+[0-9.]+\s+[0-9.]+\s*;\s*\n?")
_OPTION_LINE = re.compile(r"(?m)^[ \t]*+(?:\r?\n[ \t]*+)*+option\s+[A-Za-z0-9_-]+\s+[^;]+;\s*\n?")


def _block(text: str, start: int) -> tuple[int, str]:
    opening = text.index("{", start)
    depth = 0
    for index in range(opening, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return index + 1, text[start : index + 1]
    raise ValueError("Unbalanced DHCP configuration block")


def discover_files(main: Path) -> list[Path]:
    """Return the main file and every recursively included configuration file."""
    found: list[Path] = []

    def visit(path: Path) -> None:
        resolved = path.resolve()
        if resolved in found or not resolved.exists():
            return
        found.append(resolved)
        for include in INCLUDE.findall(resolved.read_text(encoding="utf-8")):
            visit(Path(include) if Path(include).is_absolute() else resolved.parent / include)

    visit(main)
    return found


def reservations(text: str, source: str) -> list[dict[str, str]]:
    """Parse reservations with source attribution."""
    result = []
    for match in HOST_START.finditer(text):
        _, body = _block(text, match.start())
        mac = re.search(r"hardware\s+ethernet\s+([0-9A-Fa-f:]{17})\s*;", body)
        address = re.search(r"fixed-address\s+([0-9.]+)\s*;", body)
        if mac and address:
            result.append(
                {
                    "name": match.group(1),
                    "mac": mac.group(1).lower(),
                    "address": address.group(1),
                    "source": source,
                }
            )
    return result


def remove_reservation(text: str, name: str) -> str:
    """Remove one complete host block while preserving surrounding content."""
    for match in HOST_START.finditer(text):
        if match.group(1) == name:
            end, _ = _block(text, match.start())
            return text[: match.start()] + text[end:].lstrip("\n")
    return text


def subnets(text: str) -> list[dict[str, Any]]:
    """Parse subnet ranges and options for editing/export."""
    result = []
    for match in SUBNET_START.finditer(text):
        _, body = _block(text, match.start())
        ranges = [list(item) for item in re.findall(r"\brange\s+([0-9.]+)\s+([0-9.]+)\s*;", body)]
        options = {
            name: value.strip().strip('"')
            for name, value in re.findall(r"(?m)^[ \t]*option\s+([A-Za-z0-9_-]+)\s+([^;]+);", body)
        }
        result.append(
            {
                "network": match.group(1),
                "netmask": match.group(2),
                "ranges": ranges,
                "options": options,
            }
        )
    return result


def update_subnet(text: str, network: str, ranges: list[list[str]], options: dict[str, str]) -> str:
    """Replace ranges and option statements inside one subnet block."""
    for match in SUBNET_START.finditer(text):
        if match.group(1) != network:
            continue
        end, body = _block(text, match.start())
        body = _RANGE_LINE.sub("", body)
        body = _OPTION_LINE.sub("", body)
        insertion = "".join(f"  range {start} {finish};\n" for start, finish in ranges)
        insertion += "".join(
            f"  option {name} {value};\n" for name, value in sorted(options.items())
        )
        body = (
            body[: body.index("{") + 1]
            + "\n"
            + insertion
            + body[body.index("{") + 1 :].lstrip("\n")
        )
        return text[: match.start()] + body + text[end:]
    raise KeyError(f"Unknown subnet {network}")


def kea_export(items: list[dict[str, Any]], hosts: list[dict[str, str]]) -> dict[str, Any]:
    """Create a Kea DHCPv4 migration snapshot."""
    return {
        "Dhcp4": {
            "subnet4": [
                {
                    "subnet": f"{item['network']}/{_prefix(item['netmask'])}",
                    "pools": [{"pool": f"{start} - {end}"} for start, end in item["ranges"]],
                    "option-data": [
                        {"name": name, "data": value.strip('"')}
                        for name, value in sorted(item["options"].items())
                    ],
                    "reservations": [
                        {
                            "hostname": host["name"],
                            "hw-address": host["mac"],
                            "ip-address": host["address"],
                        }
                        for host in hosts
                    ],
                }
                for item in items
            ]
        }
    }


def _prefix(netmask: str) -> int:
    return sum(bin(int(octet)).count("1") for octet in netmask.split("."))
