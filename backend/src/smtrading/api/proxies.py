"""The proxies uvicorn trusts for X-Forwarded-Proto/For (`--forwarded-allow-ips`).

Only the networks the api container is directly attached to (the compose network, where
nginx lives) are trusted: printed as CIDRs from the kernel routing table, so the compose
subnet need not be pinned. `SMT_FORWARDED_ALLOW_IPS` overrides the detection.

Usage (docker/entrypoint-api.sh): `python -m smtrading.api.proxies`.
"""

import ipaddress
import os
import socket
import struct

ROUTE_FILE = "/proc/net/route"
FALLBACK = "127.0.0.1"


def _ipv4(hex_le: str) -> ipaddress.IPv4Address:
    return ipaddress.IPv4Address(socket.inet_ntoa(struct.pack("<L", int(hex_le, 16))))


def attached_subnets(route_table: str) -> list[str]:
    """The on-link (gateway 0.0.0.0) non-default routes of /proc/net/route, as CIDRs."""
    out: list[str] = []
    for line in route_table.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 8 or fields[0] == "lo":
            continue
        dest, gateway, mask = fields[1], fields[2], fields[7]
        if int(gateway, 16) != 0 or int(mask, 16) == 0:
            continue
        net = ipaddress.IPv4Network(f"{_ipv4(dest)}/{_ipv4(mask)}", strict=False)
        if str(net) not in out:
            out.append(str(net))
    return out


def forwarded_allow_ips() -> str:
    override = os.environ.get("SMT_FORWARDED_ALLOW_IPS", "").strip()
    if override:
        return override
    try:
        with open(ROUTE_FILE) as f:
            subnets = attached_subnets(f.read())
    except OSError:
        subnets = []
    return ",".join(subnets) or FALLBACK


if __name__ == "__main__":
    print(forwarded_allow_ips())
