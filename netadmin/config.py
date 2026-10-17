"""Environment-backed application configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _required(name: str) -> str:
    value = os.getenv(name, "")
    if not value:
        raise RuntimeError(f"Required environment variable {name} is not set")
    return value


@dataclass(frozen=True, slots=True)
class Settings:  # pylint: disable=too-many-instance-attributes
    """Runtime settings shared by the web application."""

    secret_key: str
    root_password_hash: str
    data_dir: Path
    radius_host: str = "radius"
    radius_secret: str = ""
    radius_nas_ip: str = "127.0.0.1"
    radius_admin_value: str = "netadmin-admin"
    radius_standard_value: str = "netadmin-standard"
    radius_role_attribute: str = "Filter-Id"
    dns_server: str = "dns"
    dns_tsig_name: str = "rndc-key"
    dns_tsig_secret: str = ""
    dns_tsig_algorithm: str = "hmac-sha256"
    dhcp_agent_url: str = "http://dhcp-agent:8081"
    radius_agent_url: str = "http://radius-agent:8082"
    agent_token: str = ""
    session_minutes: int = 60

    @classmethod
    def from_env(cls) -> Settings:
        """Create settings from environment variables."""
        return cls(
            secret_key=_required("NETADMIN_SECRET_KEY"),
            root_password_hash=_required("NETADMIN_ROOT_PASSWORD_HASH"),
            data_dir=Path(os.getenv("NETADMIN_DATA_DIR", "/data")),
            radius_host=os.getenv("NETADMIN_RADIUS_HOST", "radius"),
            radius_secret=os.getenv("NETADMIN_RADIUS_SECRET", ""),
            radius_nas_ip=os.getenv("NETADMIN_RADIUS_NAS_IP", "127.0.0.1"),
            radius_admin_value=os.getenv("NETADMIN_RADIUS_ADMIN_VALUE", "netadmin-admin"),
            radius_standard_value=os.getenv("NETADMIN_RADIUS_STANDARD_VALUE", "netadmin-standard"),
            radius_role_attribute=os.getenv("NETADMIN_RADIUS_ROLE_ATTRIBUTE", "Filter-Id"),
            dns_server=os.getenv("NETADMIN_DNS_SERVER", "dns"),
            dns_tsig_name=os.getenv("NETADMIN_DNS_TSIG_NAME", "rndc-key"),
            dns_tsig_secret=os.getenv("NETADMIN_DNS_TSIG_SECRET", ""),
            dns_tsig_algorithm=os.getenv("NETADMIN_DNS_TSIG_ALGORITHM", "hmac-sha256"),
            dhcp_agent_url=os.getenv("NETADMIN_DHCP_AGENT_URL", "http://dhcp-agent:8081"),
            radius_agent_url=os.getenv("NETADMIN_RADIUS_AGENT_URL", "http://radius-agent:8082"),
            agent_token=os.getenv("NETADMIN_AGENT_TOKEN", ""),
            session_minutes=int(os.getenv("NETADMIN_SESSION_MINUTES", "60")),
        )
