"""Tests for the Kea control-socket client."""

import json
import socket
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from netadmin.agents.kea_ctrl import DEFAULT_SOCKET, command, main


def test_command_round_trip() -> None:
    # macOS limits AF_UNIX paths to ~104 characters and pytest's tmp_path
    # root is far longer than that, so bind in a short directory.
    with tempfile.TemporaryDirectory(dir="/tmp") as work:
        socket_path = Path(work) / "ctrl.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(socket_path))
        server.listen(1)

        def serve() -> None:
            connection, _ = server.accept()
            with connection:
                request = json.loads(connection.recv(4096))
                connection.sendall(
                    (json.dumps({"result": 0, "text": request["command"]}) + "\n").encode()
                )

        thread = threading.Thread(target=serve)
        thread.start()
        try:
            reply = command(str(socket_path), {"command": "config-reload"})
        finally:
            thread.join(timeout=5)
            server.close()
    assert reply == {"result": 0, "text": "config-reload"}


def test_main_succeeds_on_zero_result(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = {}

    def fake_command(socket_path, request):  # type: ignore[no-untyped-def]
        seen["socket"] = socket_path
        seen["request"] = request
        return {"result": 0}

    monkeypatch.setattr("netadmin.agents.kea_ctrl.command", fake_command)
    monkeypatch.setattr(sys, "argv", ["kea_ctrl", "config-reload"])
    main()
    assert '"result": 0' in capsys.readouterr().out
    # The connect target is the fixed deployment socket, never argv.
    assert seen["socket"] == DEFAULT_SOCKET
    assert seen["request"] == {"command": "config-reload"}


def test_main_fails_on_kea_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "netadmin.agents.kea_ctrl.command",
        lambda socket_path, request: {"result": 1, "text": "nope"},
    )
    monkeypatch.setattr(sys, "argv", ["kea_ctrl", "config-reload"])
    with pytest.raises(SystemExit):
        main()


def test_main_requires_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["kea_ctrl"])
    with pytest.raises(SystemExit, match="usage"):
        main()
