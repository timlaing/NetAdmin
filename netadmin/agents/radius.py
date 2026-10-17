"""Restricted FreeRADIUS files-backend agent."""

from __future__ import annotations

import os
import re
import secrets
import subprocess  # nosec B404
from pathlib import Path
from typing import Any

from flask import Flask, jsonify

from netadmin.agents.common import atomic_validated_write, create_agent, json_object

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
    reload_command = os.getenv("RADIUS_RELOAD_COMMAND", "").split()

    def text() -> str:
        return managed.read_text(encoding="utf-8") if managed.exists() else ""

    def validate(candidate: Path) -> None:
        original = managed.read_text(encoding="utf-8") if managed.exists() else None
        try:
            managed.write_text(candidate.read_text(encoding="utf-8"), encoding="utf-8")
            subprocess.run(
                ["/usr/sbin/radiusd", "-XC"],
                check=True,
                capture_output=True,
                timeout=20,
            )
        finally:
            if original is None:
                managed.unlink(missing_ok=True)
            else:
                managed.write_text(original, encoding="utf-8")

    @app.get("/health")
    def health() -> Any:
        return jsonify({"status": "ok"})

    @app.get("/users")
    def users() -> Any:
        return jsonify(parse_users(text()))

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
        atomic_validated_write(managed, render_users(entries), validate, reload_command)
        return jsonify({"username": username, "role": role, "disabled": bool(body.get("disabled"))})

    @app.post("/users/delete")
    def delete_user() -> Any:
        username = str(json_object().get("username", ""))
        entries = _entries(text())
        entries.pop(username, None)
        atomic_validated_write(managed, render_users(entries), validate, reload_command)
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
        atomic_validated_write(managed, render_users(entries), validate, reload_command)
        return jsonify({"changed": username, "nonce": secrets.token_hex(4)})

    return app


def main() -> None:
    """Run the development agent server."""
    create_radius_agent().run(host="0.0.0.0", port=8082)  # noqa: S104
