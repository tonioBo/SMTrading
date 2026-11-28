"""The search for Samsung TVs on the local network, started by an admin from the portal:
every address of a private /24 (or smaller) network is asked `GET :8001/api/v2/` with a
short timeout, at most SCAN_CONCURRENCY at a time (a /24 answers in a few seconds); the
ones that say `"type": "Samsung SmartTV"` are the TVs found.

The network: the one given, else the /24 of the portal address the admin is using (the
request's Host, when it is a private IPv4 address; from inside a container the host's
own address is not the LAN's, so nothing is guessed beyond that). Nothing is ever
hard-coded."""

import asyncio
import ipaddress
from typing import Any

import httpx

from smtrading.tv import samsung

SCAN_TIMEOUT_S = 1.2
SCAN_CONCURRENCY = 64
MIN_PREFIX = 24  # never more than 254 addresses


class SubnetError(ValueError):
    pass


def parse_subnet(raw: str) -> ipaddress.IPv4Network:
    try:
        net = ipaddress.IPv4Network(raw.strip(), strict=False)
    except (ValueError, AttributeError):
        raise SubnetError("rete non valida: scrivi per esempio 192.168.1.0/24") from None
    if net.prefixlen < MIN_PREFIX:
        raise SubnetError("rete troppo grande: al massimo /24 (254 indirizzi)")
    if not (net.is_private or net.is_loopback):
        raise SubnetError("solo reti locali (private)")
    return net


def scan_hosts(net: ipaddress.IPv4Network) -> list[str]:
    return [str(h) for h in net.hosts()] or [str(net.network_address)]


def _private_v4(host: str | None) -> ipaddress.IPv4Address | None:
    if not host:
        return None
    try:
        ip = ipaddress.IPv4Address(host.strip().rsplit(":", 1)[0] if host.count(":") == 1
                                   else host.strip())
    except ValueError:
        return None
    return ip if ip.is_private and not ip.is_loopback else None


def default_subnet(request_host: str | None) -> ipaddress.IPv4Network | None:
    ip = _private_v4(request_host)
    return ipaddress.IPv4Network(f"{ip}/24", strict=False) if ip else None


async def discover(hosts: list[str], *, client: httpx.AsyncClient | None = None,
                   timeout: float = SCAN_TIMEOUT_S) -> list[dict[str, Any]]:
    """The Samsung TVs among `hosts`: [{host, name, model, resolution, mac, power}], by
    address."""
    sem = asyncio.Semaphore(SCAN_CONCURRENCY)
    own = client is None
    http = client or httpx.AsyncClient(timeout=timeout, trust_env=False)

    async def probe(host: str) -> dict[str, Any] | None:
        async with sem:
            try:
                dev = await samsung.device_info(host, client=http, timeout=timeout)
            except samsung.TvError:
                return None
        if dev is None:
            return None
        return {"host": host, "name": dev["name"], "model": dev["model"],
                "resolution": dev["resolution"], "mac": dev["mac"], "power": dev["power"]}

    try:
        found = await asyncio.gather(*(probe(h) for h in hosts))
    finally:
        if own:
            await http.aclose()
    return sorted((f for f in found if f), key=lambda d: ipaddress.IPv4Address(d["host"]))
