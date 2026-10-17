"""Tests for ISC DHCP parsing and rendering."""

from netadmin.agents.dhcp import parse_leases, parse_reservations, render_reservations


def test_reservation_round_trip() -> None:
    items = [{"name": "pi", "mac": "aa:bb:cc:dd:ee:ff", "address": "192.0.2.20"}]
    assert parse_reservations(render_reservations(items)) == items


def test_parse_leases() -> None:
    text = """lease 192.0.2.10 {
      ends 3 2026/08/26 12:00:00;
      binding state active;
      hardware ethernet aa:bb:cc:dd:ee:ff;
      client-hostname "laptop";
    }"""
    assert parse_leases(text) == [
        {
            "address": "192.0.2.10",
            "state": "active",
            "mac": "aa:bb:cc:dd:ee:ff",
            "hostname": "laptop",
            "ends": "3 2026/08/26 12:00:00",
        }
    ]
