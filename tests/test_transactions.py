"""Transactional file update tests."""

from pathlib import Path

import pytest

from netadmin.agents.common import validated_transaction


def test_transaction_rolls_back_validation_failure(tmp_path: Path) -> None:
    first = tmp_path / "first.conf"
    second = tmp_path / "second.conf"
    first.write_text("original-one")
    second.write_text("original-two")

    def fail() -> None:
        raise RuntimeError("invalid")

    with pytest.raises(RuntimeError):
        validated_transaction({first: "changed-one", second: "changed-two"}, fail, [])
    assert first.read_text() == "original-one"
    assert second.read_text() == "original-two"
