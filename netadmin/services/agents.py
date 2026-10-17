"""HTTP clients for the privileged sidecar agents."""

from __future__ import annotations

from typing import Any

import httpx


class AgentClient:
    """Authenticated JSON client for a local service agent."""

    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"}

    def get(self, path: str) -> Any:
        """Fetch JSON from the agent."""
        response = httpx.get(
            f"{self.base_url}/{path.lstrip('/')}", headers=self.headers, timeout=10
        )
        response.raise_for_status()
        return response.json()

    def post(self, path: str, payload: dict[str, Any]) -> Any:
        """Post JSON to the agent."""
        response = httpx.post(
            f"{self.base_url}/{path.lstrip('/')}",
            headers=self.headers,
            json=payload,
            timeout=20,
        )
        response.raise_for_status()
        return response.json()
