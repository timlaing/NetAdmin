"""Tests for recursive ISC DHCPD configuration management."""

from pathlib import Path

from netadmin.services.dhcp_files import (
    discover_files,
    kea_export,
    remove_reservation,
    reservations,
    subnets,
    update_subnet,
)


def test_recursive_discovery(tmp_path: Path) -> None:
    child = tmp_path / "child.conf"
    child.write_text("# child")
    main = tmp_path / "dhcpd.conf"
    main.write_text(f'include "{child}";')
    assert discover_files(main) == [main.resolve(), child.resolve()]


def test_reservation_source_and_removal() -> None:
    text = "host pi { hardware ethernet aa:bb:cc:dd:ee:ff; fixed-address 192.0.2.3; }\n# keep\n"
    assert reservations(text, "legacy.conf")[0]["source"] == "legacy.conf"
    result = remove_reservation(text, "pi")
    assert "host pi" not in result
    assert "# keep" in result


def test_subnet_update_and_kea_export() -> None:
    text = """subnet 192.0.2.0 netmask 255.255.255.0 {
  range 192.0.2.10 192.0.2.20;
  option routers 192.0.2.1;
}"""
    updated = update_subnet(
        text,
        "192.0.2.0",
        [["192.0.2.30", "192.0.2.40"]],
        {"routers": "192.0.2.254"},
    )
    parsed = subnets(updated)
    assert parsed[0]["ranges"] == [["192.0.2.30", "192.0.2.40"]]
    snapshot = kea_export(
        parsed,
        [{"name": "pi", "mac": "aa:bb:cc:dd:ee:ff", "address": "192.0.2.5"}],
    )
    assert snapshot["Dhcp4"]["subnet4"][0]["subnet"] == "192.0.2.0/24"
    assert snapshot["Dhcp4"]["subnet4"][0]["reservations"][0]["hostname"] == "pi"
