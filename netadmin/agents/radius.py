"""Restricted FreeRADIUS files-backend agent."""

from __future__ import annotations

import os
import re
import secrets
import subprocess  # nosec B404
from pathlib import Path
from typing import Any

from flask import Flask, jsonify

from netadmin.agents.common import (
    atomic_validated_write,
    create_agent,
    json_object,
    validated_transaction,
)
from netadmin.services.radius_files import (
    migrate_authorize,
    migrate_to_managed,
    parse_authorize,
    parse_clients,
    render_clients,
)

USER_PATTERN = re.compile(
    r"^(?P<disabled>#DISABLED )?(?P<username>[A-Za-z0-9_.@-]+)\s+"
    r"Crypt-Password\s*:=\s*\"(?P<password>[^\"]+)\""
    r"(?:,\s*Filter-Id\s*:=\s*\"(?P<role>[^\"]+)\")?\s*$"
)


def parse_users(text: str) -> list[dict[str, Any]]:
    """Parse NetAdmin-managed RADIUS users without returning password hashes."""
    users = []
    for line in text.splitlines():
        match = USER_PATTERN.match(line)
        if match:
            users.append(
                {
                    "username": match.group("username"),
                    "role": match.group("role") or "netadmin-standard",
                    "disabled": bool(match.group("disabled")),
                }
            )
    return users


def _entries(text: str) -> dict[str, tuple[str, str, bool]]:
    result = {}
    for line in text.splitlines():
        match = USER_PATTERN.match(line)
        if match:
            result[match.group("username")] = (
                match.group("password"),
                match.group("role") or "netadmin-standard",
                bool(match.group("disabled")),
            )
    return result


def render_users(entries: dict[str, tuple[str, str, bool]]) -> str:
    """Render a dedicated FreeRADIUS users include."""
    lines = ["# Managed by NetAdmin. Password hashes are never returned by the API."]
    for username, (password_hash, role, disabled) in sorted(entries.items()):
        prefix = "#DISABLED " if disabled else ""
        lines.append(
            f'{prefix}{username} Crypt-Password := "{password_hash}", Filter-Id := "{role}"'
        )
    return "\n".join(lines) + "\n"


def hash_password(password: str) -> str:
    """Generate SHA-512 crypt for broad FreeRADIUS libc compatibility."""
    if len(password) < 12:
        raise ValueError("Password must be at least 12 characters")
    result = subprocess.run(
        ["/usr/bin/openssl", "passwd", "-6", "-stdin"],
        input=password,
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    return result.stdout.strip()


def create_radius_agent() -> Flask:
    """Create the FreeRADIUS management agent."""
    app = create_agent(__name__)
    managed = Path(os.getenv("RADIUS_MANAGED_FILE", "/etc/raddb/mods-config/files/netadmin"))
    authorize = Path(os.getenv("RADIUS_AUTHORIZE_FILE", "/etc/raddb/mods-config/files/authorize"))
    clients_file = Path(os.getenv("RADIUS_CLIENTS_FILE", "/etc/raddb/clients.conf"))
    managed_clients = Path(
        os.getenv("RADIUS_MANAGED_CLIENTS_FILE", "/etc/raddb/netadmin-clients.conf")
    )
    admin_pools = set(filter(None, os.getenv("RADIUS_ADMIN_POOLS", "pool-parent").split(",")))
    reload_command = os.getenv("RADIUS_RELOAD_COMMAND", "").split()

    def text() -> str:
        return managed.read_text(encoding="utf-8") if managed.exists() else ""

    def validator(target: Path) -> Any:
        def validate(candidate: Path) -> None:
            original = target.read_text(encoding="utf-8") if target.exists() else None
            try:
                target.write_text(candidate.read_text(encoding="utf-8"), encoding="utf-8")
                subprocess.run(
                    ["/usr/sbin/radiusd", "-XC"],
                    check=True,
                    capture_output=True,
                    timeout=20,
                )
            finally:
                if original is None:
                    target.unlink(missing_ok=True)
                else:
                    target.write_text(original, encoding="utf-8")

        return validate

    def validate_radius() -> None:
        subprocess.run(
            ["/usr/sbin/radiusd", "-XC"],
            check=True,
            capture_output=True,
            timeout=20,
        )

    @app.get("/health")
    def health() -> Any:
        return jsonify({"status": "ok"})

    @app.get("/users")
    def users() -> Any:
        legacy = parse_authorize(authorize.read_text(encoding="utf-8"), "authorize")
        return jsonify(
            [
                {
                    "username": user.username,
                    "role": user.role,
                    "disabled": user.disabled,
                    "source": user.source,
                    "password_kind": user.password_kind,
                }
                for user in legacy
            ]
            + parse_users(text())
        )

    @app.get("/migration")
    def migration_preview() -> Any:
        _, names = migrate_authorize(
            authorize.read_text(encoding="utf-8"),
            lambda _password: "preview",
            admin_pools,
            "netadmin-admin",
            "netadmin-standard",
        )
        return jsonify({"users": names, "count": len(names)})

    @app.post("/migration")
    def migration_apply() -> Any:
        remaining, legacy_entries = migrate_to_managed(
            authorize.read_text(encoding="utf-8"),
            hash_password,
            admin_pools,
            "netadmin-admin",
            "netadmin-standard",
        )
        entries = _entries(text())
        entries.update(legacy_entries)
        validated_transaction(
            {authorize: remaining, managed: render_users(entries)},
            validate_radius,
            reload_command,
        )
        names = sorted(legacy_entries)
        return jsonify({"users": names, "count": len(names)})

    @app.post("/users")
    def save_user() -> Any:
        body = json_object()
        username = str(body.get("username", ""))
        if not re.fullmatch(r"[A-Za-z0-9_.@-]+", username):
            return jsonify({"error": "Invalid username"}), 400
        entries = _entries(text())
        existing = entries.get(username)
        password = str(body.get("password", ""))
        password_hash = hash_password(password) if password else (existing[0] if existing else "")
        if not password_hash:
            return jsonify({"error": "Password required for a new user"}), 400
        role = str(body.get("role", "standard"))
        role_value = "netadmin-admin" if role == "admin" else "netadmin-standard"
        entries[username] = (password_hash, role_value, bool(body.get("disabled", False)))
        atomic_validated_write(managed, render_users(entries), validator(managed), reload_command)
        return jsonify({"username": username, "role": role, "disabled": bool(body.get("disabled"))})

    @app.post("/users/delete")
    def delete_user() -> Any:
        username = str(json_object().get("username", ""))
        entries = _entries(text())
        entries.pop(username, None)
        atomic_validated_write(managed, render_users(entries), validator(managed), reload_command)
        return jsonify({"deleted": username})

    @app.post("/users/password")
    def change_password() -> Any:
        body = json_object()
        username = str(body.get("username", ""))
        entries = _entries(text())
        if username not in entries:
            return jsonify({"error": "Unknown user"}), 404
        _, role, disabled = entries[username]
        entries[username] = (hash_password(str(body.get("password", ""))), role, disabled)
        atomic_validated_write(managed, render_users(entries), validator(managed), reload_command)
        return jsonify({"changed": username, "nonce": secrets.token_hex(4)})

    @app.get("/clients")
    def clients() -> Any:
        values = parse_clients(clients_file.read_text(encoding="utf-8"))
        if managed_clients.exists():
            values += parse_clients(managed_clients.read_text(encoding="utf-8"))
        return jsonify(
            [
                {"name": item.name, "address": item.address, "shortname": item.shortname}
                for item in values
            ]
        )

    @app.post("/clients")
    def save_client() -> Any:
        body = json_object()
        name = str(body.get("name", ""))
        address = str(body.get("address", ""))
        shortname = str(body.get("shortname", name))
        secret_value = str(body.get("secret", ""))
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or not secret_value:
            return jsonify({"error": "Valid name and secret required"}), 400
        entries = _client_entries(
            managed_clients.read_text(encoding="utf-8") if managed_clients.exists() else ""
        )
        entries[name] = (address, shortname, secret_value)
        atomic_validated_write(
            managed_clients,
            render_clients(entries),
            validator(managed_clients),
            reload_command,
        )
        return jsonify({"name": name, "address": address, "shortname": shortname})

    @app.post("/clients/delete")
    def delete_client() -> Any:
        name = str(json_object().get("name", ""))
        entries = _client_entries(
            managed_clients.read_text(encoding="utf-8") if managed_clients.exists() else ""
        )
        entries.pop(name, None)
        atomic_validated_write(
            managed_clients,
            render_clients(entries),
            validator(managed_clients),
            reload_command,
        )
        return jsonify({"deleted": name})

    return app


def _client_entries(text: str) -> dict[str, tuple[str, str, str]]:
    """Parse managed clients internally, including secrets required for rewrites."""
    result: dict[str, tuple[str, str, str]] = {}
    for match in re.finditer(r"(?ms)^client\s+([A-Za-z0-9_.-]+)\s*\{(.*?)^\}", text):
        body = match.group(2)
        values = {}
        for key in ("ipaddr", "shortname", "secret"):
            item = re.search(rf"(?m)^\s*{key}\s*=\s*([^\s#]+)", body)
            values[key] = item.group(1) if item else ""
        if values["ipaddr"] and values["secret"]:
            result[match.group(1)] = (values["ipaddr"], values["shortname"], values["secret"])
    return result


def main() -> None:
    """Run the development agent server."""
    create_radius_agent().run(host="0.0.0.0", port=8082)  # noqa: S104
