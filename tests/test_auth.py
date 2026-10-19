"""Tests for authentication helpers."""

from argon2 import PasswordHasher

from netadmin.auth import safe_next_url, verify_root


def test_verify_root() -> None:
    password_hash = PasswordHasher().hash("a-long-emergency-password")
    assert verify_root(password_hash, "a-long-emergency-password")
    assert not verify_root(password_hash, "wrong")


def test_safe_next_url() -> None:
    assert safe_next_url("/dns") == "/dns"
    assert safe_next_url("//evil.example") == "/"
    assert safe_next_url("https://evil.example") == "/"
    assert safe_next_url("/\\evil.example") == "/"
    assert safe_next_url(None) == "/"
