"""Minimal raw-JSON client for Kea's unix control sockets.

Kea's ``unix`` control sockets speak line-delimited JSON: one request
object per line, one reply object per line. In kea mode the agent's reload
command is ``python3 -m netadmin.agents.kea_ctrl config-reload``, which
keeps ``validated_transaction``'s contract: a non-zero exit rolls the
config change back. The connect target is a fixed constant, never taken
from argv (Sonar S8703: SSRF through CLI arguments).
"""

from __future__ import annotations

import json
import socket
import sys
from typing import Any

# The control socket's location is pinned by the deployment (the compose
# variants share the kea-run volume at /run/kea).
DEFAULT_SOCKET = "/run/kea/kea4-ctrl.sock"


def command(socket_path: str, request: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
    """Send one JSON command over a Kea unix control socket and return its reply."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(socket_path)
        client.sendall((json.dumps(request) + "\n").encode())
        reply = b""
        while b"\n" not in reply and len(reply) < 1_048_576:
            chunk = client.recv(65536)
            if not chunk:
                break
            reply += chunk
    parsed = json.loads(reply.split(b"\n", 1)[0])
    if not isinstance(parsed, dict):
        raise ValueError(f"Kea returned a non-object reply: {parsed!r}")
    return parsed


def main() -> None:
    """CLI entry point: COMMAND, with a non-zero exit on Kea failure."""
    if len(sys.argv) != 2:
        raise SystemExit("usage: python3 -m netadmin.agents.kea_ctrl COMMAND")
    reply = command(DEFAULT_SOCKET, {"command": sys.argv[1]})
    print(json.dumps(reply))
    if reply.get("result", 1) != 0:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
