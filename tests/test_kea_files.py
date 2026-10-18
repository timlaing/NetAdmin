"""Tests for Kea configuration assembly and memfile lease parsing."""

import json
from pathlib import Path

import pytest

from netadmin.services.kea_files import (
    discover_files,
    parse_leases,
    parse_reservations,
    remove_reservation,
    render_reservations,
    reservations,
    subnets,
    update_subnet,
)

FIXTURES = Path(__file__).parent / "fixtures" / "kea"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def structure(text: str) -> object:
    """Strip trivia the way Kea does: comments and include directives gone."""
    stripped = "\n".join(
        line
        for line in text.splitlines()
        if not line.lstrip().startswith("#") and "<?include" not in line
    )
    return json.loads(stripped)


def test_recursive_discovery(tmp_path: Path) -> None:
    child = tmp_path / "reservations.json"
    child.write_text('{"reservations": []}', encoding="utf-8")
    main = tmp_path / "kea4.conf"
    main.write_text(f'<?include "{child}"?>', encoding="utf-8")
    assert discover_files(main) == [main.resolve(), child.resolve()]


def test_fixture_subnets() -> None:
    parsed = subnets(fixture("kea4.conf"))
    assert [entry["network"] for entry in parsed] == ["192.0.2.0", "198.51.100.0"]
    assert parsed[0]["netmask"] == "255.255.255.0"
    assert parsed[0]["id"] == 1
    assert parsed[0]["ranges"] == [["192.0.2.100", "192.0.2.199"]]
    assert parsed[0]["options"] == {
        "routers": "192.0.2.1",
        "domain-name-servers": "192.0.2.1",
        "domain-name": "lan.example",
    }
    assert parsed[1]["id"] == 2
    assert parsed[1]["ranges"] == [["198.51.100.50", "198.51.100.99"]]
    assert "domain-name" not in parsed[1]["options"]


def test_update_subnet_preserves_everything_else() -> None:
    original = fixture("kea4.conf")
    updated = update_subnet(
        original,
        "198.51.100.0",
        [["198.51.100.10", "198.51.100.20"]],
        {"routers": "198.51.100.254"},
    )
    parsed = subnets(updated)
    assert parsed[1]["ranges"] == [["198.51.100.10", "198.51.100.20"]]
    assert parsed[1]["options"] == {"routers": "198.51.100.254"}
    assert parsed[0] == subnets(original)[0]
    assert "PXEClient-UEFI-x64" in updated
    assert "# Office LAN" in updated
    assert '<?include "/etc/dhcp/netadmin-reservations.json"?>' in updated
    assert '"control-sockets"' in updated
    before, after = structure(original), structure(updated)
    assert after["Dhcp4"]["subnet4"][0] == before["Dhcp4"]["subnet4"][0]
    assert after["Dhcp4"]["subnet4"][1]["id"] == 2
    for key in before["Dhcp4"]:
        if key != "subnet4":
            assert after["Dhcp4"][key] == before["Dhcp4"][key]


def test_update_unknown_subnet_raises() -> None:
    with pytest.raises(KeyError):
        update_subnet(fixture("kea4.conf"), "203.0.113.0", [], {})


def test_reservations_parse_remove_and_source() -> None:
    text = fixture("netadmin-reservations.json")
    entries = reservations(text, "netadmin-reservations.json")
    assert [(entry["name"], entry["address"]) for entry in entries] == [
        ("laptop", "192.0.2.10"),
        ("pi", "192.0.2.20"),
        ("printer", "198.51.100.20"),
    ]
    assert entries[0]["source"] == "netadmin-reservations.json"
    reduced = remove_reservation(text, "printer")
    assert [entry["name"] for entry in parse_reservations(reduced)] == ["laptop", "pi"]
    assert reduced.startswith("# Managed by NetAdmin.")
    assert remove_reservation(text, "ghost") == text


def test_render_is_deterministic_and_round_trips() -> None:
    items = [
        {"name": "pi", "mac": "aa:bb:cc:dd:ee:03", "address": "192.0.2.20"},
        {"name": "laptop", "mac": "AA:BB:CC:DD:EE:01", "address": "192.0.2.10"},
    ]
    first = render_reservations(items)
    assert render_reservations(list(reversed(items))) == first
    assert parse_reservations(first) == [
        {"name": "laptop", "mac": "aa:bb:cc:dd:ee:01", "address": "192.0.2.10"},
        {"name": "pi", "mac": "aa:bb:cc:dd:ee:03", "address": "192.0.2.20"},
    ]


def test_parse_leases_memfile() -> None:
    text = """\
# a comment the parser must skip
192.0.2.100,aa:bb:cc:dd:ee:ff,01:aa:bb:cc:dd:ee:ff,3600,1766798400,1,1,1,laptop,0,,
198.51.100.7,00:11:22:33:44:55,,3600,1700000000,2,0,0,,3,,
192.0.2.101,22:33:44:55:66:77,,3600,1700000100,1,0,0,,1,,"{""ISC"":{""relay-agent"":""guest""}}"
address,hwaddr,clientid,valid_lifetime,expire,subnet_id,fqdn_fwd,fqdn_rev,hostname,state,,
192.0.2.99,aa
"""
    assert parse_leases(text) == [
        {
            "address": "192.0.2.100",
            "state": "active",
            "mac": "aa:bb:cc:dd:ee:ff",
            "hostname": "laptop",
            "ends": "1766798400",
        },
        {
            "address": "198.51.100.7",
            "state": "released",
            "mac": "00:11:22:33:44:55",
            "hostname": "",
            "ends": "1700000000",
        },
        {
            "address": "192.0.2.101",
            "state": "declined",
            "mac": "22:33:44:55:66:77",
            "hostname": "",
            "ends": "1700000100",
        },
    ]
