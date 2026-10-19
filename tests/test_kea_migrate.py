"""Tests for the one-shot ISC-to-Kea migration engine."""

import csv
import io
import json
import time
from datetime import datetime
from pathlib import Path

import pytest

from netadmin.services.kea_files import parse_reservations
from netadmin.services.kea_files import subnets as kea_subnets
from netadmin.services.kea_migrate import (
    build_config,
    collect,
    conflicts,
    convert_leases,
    fold,
    main,
    migrate,
    render_config,
    subnet_id_for,
)

# Kea 3.0.3's memfile column order (verified against a live daemon); the
# numeric trailing columns must be present or the daemon discards the row.
MEMFILE_HEADER = [
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

SUBNETS = [
    {"network": "192.0.2.0", "netmask": "255.255.255.0"},
    {"network": "198.51.100.0", "netmask": "255.255.255.0"},
]

LEASES = """\
lease 192.0.2.10 {
  starts 6 2026/08/26 11:00:00;
  ends 6 2026/08/26 12:00:00;
  binding state active;
  hardware ethernet AA:BB:CC:DD:EE:01;
  client id 01:AA:BB:CC:DD:EE:01;
  client-hostname "laptop";
}
lease 192.0.2.55 {
  starts 6 2026/08/26 11:00:00;
  ends 6 2026/08/26 11:30:00;
  binding state free;
  hardware ethernet aa:bb:cc:dd:ee:99;
}
lease 198.51.100.20 {
  starts 6 2026/08/26 11:00:00;
  ends 6 2026/08/26 23:00:00;
  binding state released;
  hardware ethernet aa:bb:cc:dd:ee:02;
}
lease 192.0.2.77 {
  binding state active;
  hardware ethernet aa:bb:cc:dd:ee:77;
}
"""


def isc_tree(tmp_path: Path) -> Path:
    """Realistic ISC tree: global options, an include, a shared-network."""
    hosts = tmp_path / "hosts.conf"
    hosts.write_text(
        "host laptop {\n"
        "  hardware ethernet aa:bb:cc:dd:ee:01;\n"
        "  fixed-address 192.0.2.10;\n"
        "}\n"
        "host pi {\n"
        "  hardware ethernet aa:bb:cc:dd:ee:03;\n"
        "  fixed-address 192.0.2.20;\n"
        "}\n",
        encoding="utf-8",
    )
    config = tmp_path / "dhcpd.conf"
    config.write_text(
        "default-lease-time 600;\n"
        "authoritative;\n"
        'option domain-name "lan.example";\n'
        "option domain-name-servers 192.0.2.1, 192.0.2.2;\n"
        f'include "{hosts}";\n'
        "subnet 192.0.2.0 netmask 255.255.255.0 {\n"
        "  range 192.0.2.100 192.0.2.199;\n"
        "  option routers 192.0.2.1;\n"
        "}\n"
        "shared-network office {\n"
        "  subnet 198.51.100.0 netmask 255.255.255.0 {\n"
        "    range 198.51.100.50 198.51.100.99;\n"
        "    option routers 198.51.100.1;\n"
        "  }\n"
        "}\n"
        "host printer {\n"
        "  hardware ethernet aa:bb:cc:dd:ee:02;\n"
        "  fixed-address 198.51.100.20;\n"
        "}\n",
        encoding="utf-8",
    )
    return config


def rows(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


def migrate_kwargs() -> dict[str, object]:
    return {
        "include_path": "/etc/dhcp/netadmin-reservations.json",
        "interfaces": ["eth0"],
        "lease_db": "/var/lib/kea/dhcp4.leases",
        "lifetime": 3600,
        "max_lifetime": 7200,
        "enable_d2": True,
        "socket_name": "kea4-ctrl.sock",
    }


def test_collect_subnets_hosts_and_global_option_inheritance(tmp_path: Path) -> None:
    config = isc_tree(tmp_path)
    subnets, hosts = collect(config)
    assert [entry["network"] for entry in subnets] == ["192.0.2.0", "198.51.100.0"]
    assert subnets[0]["ranges"] == [["192.0.2.100", "192.0.2.199"]]
    assert subnets[1]["ranges"] == [["198.51.100.50", "198.51.100.99"]]
    # Top-level options reach every subnet; subnet-local routers wins.
    for entry in subnets:
        assert entry["options"]["domain-name"] == "lan.example"
        assert entry["options"]["domain-name-servers"] == "192.0.2.1, 192.0.2.2"
    assert subnets[0]["options"]["routers"] == "192.0.2.1"
    assert subnets[1]["options"]["routers"] == "198.51.100.1"
    assert sorted(host["name"] for host in hosts) == ["laptop", "pi", "printer"]
    assert {Path(host["source"]).name for host in hosts} == {"dhcpd.conf", "hosts.conf"}


def test_fold_dedupes_by_name_first_wins_and_sorts() -> None:
    hosts = [
        {"name": "pi", "mac": "aa:00:00:00:00:01", "address": "192.0.2.7", "source": "a"},
        {"name": "laptop", "mac": "aa:00:00:00:00:02", "address": "192.0.2.8", "source": "a"},
        {"name": "pi", "mac": "aa:00:00:00:00:03", "address": "192.0.2.9", "source": "b"},
    ]
    folded = fold(hosts)
    assert [entry["name"] for entry in folded] == ["laptop", "pi"]
    assert folded[1]["mac"] == "aa:00:00:00:00:01"


def test_conflicts_reports_duplicate_mac_and_address() -> None:
    problems = conflicts(
        [
            {"name": "a", "mac": "aa:bb:cc:dd:ee:01", "address": "192.0.2.10"},
            {"name": "b", "mac": "aa:bb:cc:dd:ee:01", "address": "192.0.2.11"},
            {"name": "c", "mac": "aa:bb:cc:dd:ee:02", "address": "192.0.2.11"},
        ]
    )
    assert any("MAC aa:bb:cc:dd:ee:01" in problem for problem in problems)
    assert any("address 192.0.2.11" in problem for problem in problems)
    assert len(problems) == 2
    assert conflicts([{"name": "a", "mac": "aa:bb:cc:dd:ee:01", "address": "192.0.2.10"}]) == []


def test_build_config_shape(tmp_path: Path) -> None:
    subnets, _ = collect(isc_tree(tmp_path))
    config = build_config(
        subnets,
        interfaces=["eth0", "eth1"],
        lease_db="/var/lib/kea/dhcp4.leases",
        lifetime=1800,
        max_lifetime=3600,
        enable_d2=True,
        socket_name="kea4-ctrl.sock",
    )
    dhcp4 = config["Dhcp4"]
    assert dhcp4["interfaces-config"]["interfaces"] == ["eth0", "eth1"]
    assert dhcp4["lease-database"] == {
        "type": "memfile",
        "persist": True,
        "name": "/var/lib/kea/dhcp4.leases",
    }
    assert dhcp4["reservations-global"] is True
    assert dhcp4["valid-lifetime"] == 1800
    assert dhcp4["max-valid-lifetime"] == 3600
    assert dhcp4["authoritative"] is True
    assert dhcp4["dhcp-ddns"] == {
        "enable-updates": True,
        "ncr-protocol": "UDP",
        "ncr-format": "JSON",
    }
    assert [entry["id"] for entry in dhcp4["subnet4"]] == [1, 2]
    assert dhcp4["subnet4"][0]["subnet"] == "192.0.2.0/24"
    assert dhcp4["subnet4"][0]["pools"] == [{"pool": "192.0.2.100 - 192.0.2.199"}]
    assert dhcp4["subnet4"][1]["subnet"] == "198.51.100.0/24"
    # Reservations live only in the agent-owned include, never inline.
    assert all("reservations" not in entry for entry in dhcp4["subnet4"])
    assert dhcp4["control-sockets"] == [{"socket-type": "unix", "socket-name": "kea4-ctrl.sock"}]
    assert dhcp4["loggers"] == [{"name": "*", "severity": "INFO"}]
    option_names = [option["name"] for option in dhcp4["subnet4"][0]["option-data"]]
    assert option_names == sorted(option_names)
    second = {option["name"]: option["data"] for option in dhcp4["subnet4"][1]["option-data"]}
    assert second["routers"] == "198.51.100.1"
    assert second["domain-name"] == "lan.example"


def test_build_config_omits_d2_when_disabled() -> None:
    config = build_config(
        [],
        interfaces=["eth0"],
        lease_db="/db",
        lifetime=60,
        max_lifetime=120,
        enable_d2=False,
        socket_name="ctrl.sock",
    )
    assert "dhcp-ddns" not in config["Dhcp4"]


def test_render_config_splices_include_and_stays_parsable(tmp_path: Path) -> None:
    subnets, _ = collect(isc_tree(tmp_path))
    config = build_config(
        subnets,
        interfaces=["eth0"],
        lease_db="/var/lib/kea/dhcp4.leases",
        lifetime=3600,
        max_lifetime=7200,
        enable_d2=True,
        socket_name="kea4-ctrl.sock",
    )
    text = render_config(config, "/etc/dhcp/netadmin-reservations.json")
    assert '<?include "/etc/dhcp/netadmin-reservations.json"?>,' in text
    assert "@@NETADMIN_INCLUDE@@" not in text
    assert "__include__" not in text
    stripped = "\n".join(line for line in text.splitlines() if "<?include" not in line)
    assert json.loads(stripped)["Dhcp4"]["reservations-global"] is True
    parsed = kea_subnets(text)
    assert [entry["network"] for entry in parsed] == ["192.0.2.0", "198.51.100.0"]


def test_subnet_id_for_containment_and_miss() -> None:
    assert subnet_id_for("192.0.2.10", SUBNETS) == 1
    assert subnet_id_for("198.51.100.20", SUBNETS) == 2
    assert subnet_id_for("203.0.113.5", SUBNETS) == 0


def test_convert_leases_keeps_live_bindings_only() -> None:
    converted = rows(convert_leases(LEASES, SUBNETS))
    header, *data = converted
    assert header == MEMFILE_HEADER
    # Stale (free) and malformed (no timestamps) leases are not carried over.
    assert {row[0] for row in data} == {"192.0.2.10", "198.51.100.20"}
    active = next(row for row in data if row[0] == "192.0.2.10")
    assert active[1] == "aa:bb:cc:dd:ee:01"  # mac lowercased
    assert active[2] == "01:aa:bb:cc:dd:ee:01"  # client id passed through
    assert active[3] == "3600"  # ends - starts
    assert active[4] == str(int(time.mktime(datetime(2026, 8, 26, 12, 0, 0).timetuple())))
    assert active[5] == "1"  # subnet containment
    assert active[6:8] == ["0", "0"]
    assert active[8] == "laptop"
    assert active[9] == "0"  # STATE_ACTIVE
    assert active[10] == ""  # user_context may be empty
    released = next(row for row in data if row[0] == "198.51.100.20")
    assert released[5] == "2"
    assert released[9] == "3"  # STATE_RELEASED
    assert released[8] == ""  # no client-hostname
    # pool_id is numeric: an empty value makes Kea 3.0.3 discard the row.
    assert all(row[11] == "0" for row in data)


def test_convert_leases_survives_nested_failover_block() -> None:
    text = """\
lease 192.0.2.90 {
  starts 6 2026/08/26 11:00:00;
  ends 6 2026/08/26 12:00:00;
  binding state active;
  hardware ethernet aa:bb:cc:dd:ee:09;
  failover peer "home" state {
     my state is normal,
     peer state is normal.
  }
  client-hostname "nas";
}
"""
    _, *data = rows(convert_leases(text, SUBNETS))
    assert len(data) == 1
    assert data[0][0] == "192.0.2.90"
    assert data[0][8] == "nas"


def test_migrate_rejects_conflicting_reservations(tmp_path: Path) -> None:
    config = tmp_path / "dhcpd.conf"
    config.write_text(
        "host a { hardware ethernet aa:bb:cc:dd:ee:01; fixed-address 192.0.2.10; }\n"
        "host b { hardware ethernet aa:bb:cc:dd:ee:01; fixed-address 192.0.2.11; }\n",
        encoding="utf-8",
    )
    kwargs = migrate_kwargs()
    with pytest.raises(ValueError, match="aa:bb:cc:dd:ee:01"):
        migrate(config, **kwargs)


def test_collect_fails_on_missing_include(tmp_path: Path) -> None:
    config = tmp_path / "dhcpd.conf"
    config.write_text(
        'include "gone.conf";\n'
        "subnet 192.0.2.0 netmask 255.255.255.0 { range 192.0.2.10 192.0.2.20; }\n",
        encoding="utf-8",
    )
    with pytest.raises(FileNotFoundError, match=r"gone\.conf"):
        collect(config)


def test_migrate_requires_at_least_one_subnet(tmp_path: Path) -> None:
    config = tmp_path / "dhcpd.conf"
    config.write_text("default-lease-time 600;\n", encoding="utf-8")
    kwargs = migrate_kwargs()
    with pytest.raises(ValueError, match="no subnets"):
        migrate(config, **kwargs)


def test_migrate_round_trips_config_and_reservations(tmp_path: Path) -> None:
    config_text, reservations_text, folded = migrate(isc_tree(tmp_path), **migrate_kwargs())
    assert [entry["name"] for entry in folded] == ["laptop", "pi", "printer"]
    assert parse_reservations(reservations_text) == [
        {"name": "laptop", "mac": "aa:bb:cc:dd:ee:01", "address": "192.0.2.10"},
        {"name": "pi", "mac": "aa:bb:cc:dd:ee:03", "address": "192.0.2.20"},
        {"name": "printer", "mac": "aa:bb:cc:dd:ee:02", "address": "198.51.100.20"},
    ]
    assert reservations_text.startswith("# Managed by NetAdmin.")
    assert '<?include "/etc/dhcp/netadmin-reservations.json"?>' in config_text
    assert [entry["network"] for entry in kea_subnets(config_text)] == [
        "192.0.2.0",
        "198.51.100.0",
    ]


def test_main_writes_outputs_and_is_idempotent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = isc_tree(tmp_path)
    leases = tmp_path / "dhcpd.leases"
    leases.write_text(LEASES, encoding="utf-8")
    out = tmp_path / "out"
    argv = [
        "--config",
        str(config),
        "--out-dir",
        str(out),
        "--leases",
        str(leases),
        "--interfaces",
        "eth0, eth1",
        "--lifetime",
        "1800",
    ]
    assert main(argv) == 0
    first = {path.name: path.read_text(encoding="utf-8") for path in sorted(out.iterdir())}
    assert set(first) == {"kea4.conf", "netadmin-reservations.json", "dhcp4.leases"}
    assert main(argv) == 0
    second = {path.name: path.read_text(encoding="utf-8") for path in sorted(out.iterdir())}
    assert first == second
    assert '"valid-lifetime": 1800' in first["kea4.conf"]
    assert '"eth1"' in first["kea4.conf"]
    assert '<?include "/etc/dhcp/netadmin-reservations.json"?>' in first["kea4.conf"]
    captured = capsys.readouterr()
    assert "subnets: 2" in captured.err
    assert "reservations: 3" in captured.err
    assert "leases converted: 2" in captured.err


def test_main_dry_run_writes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "out"
    assert main(["--config", str(isc_tree(tmp_path)), "--out-dir", str(out), "--dry-run"]) == 0
    assert not out.exists()
    printed = capsys.readouterr().out
    assert "=== kea4.conf ===" in printed
    assert "=== netadmin-reservations.json ===" in printed


def test_main_reports_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "out"
    missing = tmp_path / "nope.conf"
    assert main(["--config", str(missing), "--out-dir", str(out)]) == 1
    assert "error:" in capsys.readouterr().err
    conflict = tmp_path / "dhcpd.conf"
    conflict.write_text(
        "host a { hardware ethernet aa:bb:cc:dd:ee:01; fixed-address 192.0.2.10; }\n"
        "host b { hardware ethernet aa:bb:cc:dd:ee:01; fixed-address 192.0.2.11; }\n",
        encoding="utf-8",
    )
    assert main(["--config", str(conflict), "--out-dir", str(out)]) == 1
    assert "Conflicting reservations" in capsys.readouterr().err
    assert not out.exists()
