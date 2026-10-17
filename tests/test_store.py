"""Tests for server-side state."""

from pathlib import Path

from netadmin.store import Store


def test_session_lifecycle(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    session_id = store.create_session("alice", "standard", 60)
    record = store.get_session(session_id)
    assert record is not None
    assert record.username == "alice"
    store.delete_session(session_id)
    assert store.get_session(session_id) is None


def test_audit(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    store.audit("root", "dns.upsert", "host.example/A", True)
    events = store.recent_audit()
    assert events[0]["username"] == "root"
    assert events[0]["success"] == 1


def test_delete_all_user_sessions(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    first = store.create_session("alice", "standard", 60)
    second = store.create_session("alice", "standard", 60)
    store.delete_user_sessions("alice")
    assert store.get_session(first) is None
    assert store.get_session(second) is None


def test_expired_session_is_rejected(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    session_id = store.create_session("alice", "standard", -1)
    assert store.get_session(session_id) is None
