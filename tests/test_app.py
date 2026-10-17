"""Route authorization and CSRF tests."""

from pathlib import Path

from argon2 import PasswordHasher

from netadmin import create_app
from netadmin.config import Settings


def make_app(tmp_path: Path):  # type: ignore[no-untyped-def]
    app = create_app(
        Settings(
            secret_key="test-secret",  # noqa: S106
            root_password_hash=PasswordHasher().hash("correct-horse-battery-staple"),
            data_dir=tmp_path,
        )
    )
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    return app


def test_anonymous_redirected(tmp_path: Path) -> None:
    client = make_app(tmp_path).test_client()
    response = client.get("/")
    assert response.status_code == 302
    assert "/login" in response.location


def test_root_login_and_dashboard(tmp_path: Path) -> None:
    client = make_app(tmp_path).test_client()
    response = client.post(
        "/login",
        data={"username": "root", "password": "correct-horse-battery-staple"},
    )
    assert response.status_code == 302
    response = client.get("/", headers={"Cookie": response.headers["Set-Cookie"]})
    assert response.status_code == 200
    assert b"Network overview" in response.data


def test_bad_root_login(tmp_path: Path) -> None:
    client = make_app(tmp_path).test_client()
    response = client.post("/login", data={"username": "root", "password": "wrong"})
    assert b"Invalid username" in response.data
