"""Bind AXFR and RFC 2136 adapter."""

from __future__ import annotations

from dataclasses import dataclass

import dns.name
import dns.query
import dns.tsigkeyring
import dns.update
import dns.zone


@dataclass(frozen=True, slots=True)
class DnsRecord:
    """Rendered DNS record."""

    name: str
    ttl: int
    record_type: str
    value: str


class BindService:
    """Read and mutate authoritative Bind zones."""

    def __init__(
        self, server: str, key_name: str, key_secret: str, algorithm: str = "hmac-sha256"
    ) -> None:
        self.server = server
        self.key_name = key_name
        self.keyring = dns.tsigkeyring.from_text({key_name: key_secret}) if key_secret else None
        self.algorithm = algorithm

    def records(self, zone_name: str) -> list[DnsRecord]:
        """Read current zone state with AXFR."""
        transfer = dns.query.xfr(
            self.server,
            zone_name,
            keyring=self.keyring,
            keyname=self.key_name if self.keyring else None,
            relativize=True,
            lifetime=10,
        )
        zone = dns.zone.from_xfr(transfer)
        records: list[DnsRecord] = []
        for name, node in zone.nodes.items():
            for rdataset in node.rdatasets:
                for record in rdataset:
                    records.append(
                        DnsRecord(
                            name.to_text(), rdataset.ttl, rdataset.rdtype.name, record.to_text()
                        )
                    )
        return sorted(records, key=lambda item: (item.name, item.record_type, item.value))

    def upsert(self, zone: str, name: str, record_type: str, ttl: int, value: str) -> None:
        """Replace an RRset using authenticated dynamic update."""
        update = dns.update.Update(
            zone,
            keyring=self.keyring,
            keyname=dns.name.from_text(self.key_name),
            keyalgorithm=self.algorithm,
        )
        update.replace(name, ttl, record_type, value)
        response = dns.query.tcp(update, self.server, timeout=10)
        if response.rcode() != 0:
            raise RuntimeError(f"DNS update failed with rcode {response.rcode()}")

    def delete(self, zone: str, name: str, record_type: str, value: str = "") -> None:
        """Delete one value or a complete RRset."""
        update = dns.update.Update(
            zone,
            keyring=self.keyring,
            keyname=dns.name.from_text(self.key_name),
            keyalgorithm=self.algorithm,
        )
        if value:
            update.delete(name, record_type, value)
        else:
            update.delete(name, record_type)
        response = dns.query.tcp(update, self.server, timeout=10)
        if response.rcode() != 0:
            raise RuntimeError(f"DNS delete failed with rcode {response.rcode()}")
