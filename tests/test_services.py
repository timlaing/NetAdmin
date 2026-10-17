"""Tests for external service adapters."""

from pathlib import Path
from types import SimpleNamespace

import httpx

from netadmin.auth import authenticate_radius
from netadmin.config import Settings
from netadmin.services.agents import AgentClient


def test_radius_role_mapping(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        return SimpleNamespace(returncode=0, stdout='Access-Accept\nFilter-Id = "admin-value"')

    monkeypatch.setattr("netadmin.auth.subprocess.run", fake_run)
    settings = Settings(
        secret_key="x",  # noqa: S106
        root_password_hash="x",  # noqa: S106
        data_dir=tmp_path,
        radius_secret="shared-secret",  # noqa: S106
        radius_admin_value="admin-value",
    )
    assert authenticate_radius("alice", "password", settings) == "admin"


def test_radius_reject(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        "netadmin.auth.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="Access-Reject"),
    )
    settings = Settings(
        secret_key="x",  # noqa: S106
        root_password_hash="x",  # noqa: S106
        data_dir=tmp_path,
        radius_secret="shared-secret",  # noqa: S106
    )
    assert authenticate_radius("alice", "bad", settings) is None


def test_agent_client(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self):  # type: ignore[no-untyped-def]
            return {"ok": True}

    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: Response())
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: Response())
    client = AgentClient("http://agent", "token")
    assert client.get("health") == {"ok": True}
    assert client.post("items", {"name": "one"}) == {"ok": True}
