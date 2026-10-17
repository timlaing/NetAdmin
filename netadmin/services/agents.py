"""HTTP clients for the privileged sidecar agents."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx


class AgentClient:
    """Authenticated JSON client for a local service agent."""

    def __init__(
        self,
        base_url: str,
        token: str,
        ca: Path | None = None,
        cert: Path | None = None,
        key: Path | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"}
        self.verify: bool | str = str(ca) if ca else True
        self.cert: tuple[str, str] | None = (str(cert), str(key)) if cert and key else None

    def get(self, path: str) -> Any:
        """Fetch JSON from the agent."""
        with httpx.Client(verify=self.verify, cert=self.cert) as client:
            response = client.get(
                f"{self.base_url}/{path.lstrip('/')}", headers=self.headers, timeout=10
            )
        response.raise_for_status()
        return response.json()

    def post(self, path: str, payload: dict[str, Any]) -> Any:
        """Post JSON to the agent."""
        with httpx.Client(verify=self.verify, cert=self.cert) as client:
            response = client.post(
                f"{self.base_url}/{path.lstrip('/')}",
                headers=self.headers,
                json=payload,
                timeout=20,
            )
        response.raise_for_status()
        return response.json()
