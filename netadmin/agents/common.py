"""Shared service-agent security and file transaction helpers."""

from __future__ import annotations

import hmac
import os
import shutil
import subprocess  # nosec B404 - commands are fixed by the operator
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from flask import Flask, abort, request


def create_agent(name: str) -> Flask:
    """Create an internal agent protected by a bearer token."""
    app = Flask(name)
    expected = os.environ.get("NETADMIN_AGENT_TOKEN", "")
    if not expected:
        raise RuntimeError("NETADMIN_AGENT_TOKEN must be set")

    @app.before_request
    def authenticate() -> None:
        supplied = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if not hmac.compare_digest(supplied, expected):
            abort(401)

    return app


def atomic_validated_write(
    path: Path, content: str, validate: Callable[[Path], None], reload_command: list[str]
) -> None:
    """Validate a candidate, atomically install it, and roll back failed reloads."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, candidate_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    candidate = Path(candidate_name)
    backup = path.with_suffix(f"{path.suffix}.{int(time.time())}.bak")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        candidate.chmod(0o640)
        validate(candidate)
        if path.exists():
            shutil.copy2(path, backup)
        os.replace(candidate, path)
        if reload_command:
            subprocess.run(reload_command, check=True, timeout=30)  # noqa: S603
    except Exception:
        if backup.exists():
            os.replace(backup, path)
        raise
    finally:
        candidate.unlink(missing_ok=True)


def json_object() -> dict[str, Any]:
    """Return and validate a JSON object request body."""
    value = request.get_json(silent=False)
    if not isinstance(value, dict):
        abort(400, "JSON object required")
    return value
