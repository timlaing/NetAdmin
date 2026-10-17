"""Tests for safe FreeRADIUS configuration transforms."""

from netadmin.services.radius_files import (
    migrate_authorize,
    parse_authorize,
    parse_clients,
    render_clients,
)


def test_migrate_preserves_entries_and_assigns_roles() -> None:
    source = """alice Cleartext-Password := "plain"
    Framed-Pool = "pool-parent"

bob Cleartext-Password := "another"
    Framed-Pool = "pool-child"
"""
    migrated, names = migrate_authorize(
        source,
        lambda value: f"hash-{value}",
        {"pool-parent"},
        "netadmin-admin",
        "netadmin-standard",
    )
    assert names == ["alice", "bob"]
    assert "Cleartext-Password" not in migrated
    assert 'alice Crypt-Password := "hash-plain", Filter-Id := "netadmin-admin"' in migrated
    assert 'Filter-Id := "netadmin-standard"' in migrated


def test_parse_authorize_never_returns_password() -> None:
    users = parse_authorize(
        'alice Crypt-Password := "$6$private", Filter-Id := "netadmin-admin"\n', "legacy"
    )
    assert users[0].username == "alice"
    assert users[0].role == "netadmin-admin"
    assert not hasattr(users[0], "password")


def test_client_round_trip_omits_secret() -> None:
    rendered = render_clients({"router": ("192.0.2.1", "gateway", "very-secret")})
    clients = parse_clients(rendered)
    assert clients[0].name == "router"
    assert clients[0].address == "192.0.2.1"
    assert not hasattr(clients[0], "secret")
