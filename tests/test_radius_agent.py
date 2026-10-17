"""Tests for FreeRADIUS user rendering."""

from netadmin.agents.radius import parse_users, render_users


def test_user_output_never_contains_hash() -> None:
    rendered = render_users({"alice": ("$6$secret-hash", "netadmin-standard", False)})
    users = parse_users(rendered)
    assert users == [{"username": "alice", "role": "netadmin-standard", "disabled": False}]
    assert "password" not in users[0]


def test_disabled_user() -> None:
    rendered = render_users({"bob": ("$6$hash", "netadmin-admin", True)})
    assert parse_users(rendered)[0]["disabled"] is True
