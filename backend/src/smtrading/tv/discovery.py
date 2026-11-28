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
SEARCH_TIMEOUT_S = 10.0
MIN_PREFIX = 24  # never more than 254 addresses


# the home networks (RFC 1918) a TV may be on; nothing else is ever contacted
LAN_NETS = tuple(ipaddress.IPv4Network(n) for n in ("10.0.0.0/8", "172.16.0.0/12",
                                                       "192.168.0.0/16"))
_LOOPBACK = ipaddress.IPv4Network("127.0.0.0/8")


class SubnetError(ValueError):
    pass


def _loopback_ok(ip) -> bool:
    return samsung.ALLOW_LOOPBACK and ip in _LOOPBACK


def is_lan_host(ip: ipaddress.IPv4Address) -> bool:
    """An address of a home network (10/8, 172.16/12, 192.168/16) that can be a device:
    not a .0 or .255 address (network, broadcast). Loopback only for the tests' fake TV
    (`samsung.ALLOW_LOOPBACK`)."""
    if _loopback_ok(ip):
        return True
    last = int(ip) & 0xFF
    return any(ip in n for n in LAN_NETS) and last not in (0, 255)


def is_lan_net(net: ipaddress.IPv4Network) -> bool:
    if samsung.ALLOW_LOOPBACK and net.subnet_of(_LOOPBACK):
        return True
    return any(net.subnet_of(n) for n in LAN_NETS)


def parse_subnet(raw: str) -> ipaddress.IPv4Network:
    try:
        net = ipaddress.IPv4Network(raw.strip(), strict=False)
    except (ValueError, AttributeError):
        raise SubnetError("rete non valida: scrivi per esempio 192.168.1.0/24") from None
    if net.prefixlen < MIN_PREFIX:
        raise SubnetError("rete troppo grande: al massimo /24 (254 indirizzi)")
    if not is_lan_net(net):
        raise SubnetError("solo reti di casa: 10.x, 172.16–31.x o 192.168.x")
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
    return ip if any(ip in n for n in LAN_NETS) else None


def default_subnet(request_host: str | None) -> ipaddress.IPv4Network | None:
    ip = _private_v4(request_host)
    return ipaddress.IPv4Network(f"{ip}/24", strict=False) if ip else None


async def discover(hosts: list[str], *, client: httpx.AsyncClient | None = None,
                   timeout: float = SCAN_TIMEOUT_S,
                   timeout_s: float | None = None) -> list[dict[str, Any]]:
    """The Samsung TVs among `hosts`: [{host, name, model, resolution, mac, power}], by
    address; the whole search bounded by `timeout_s` (default SEARCH_TIMEOUT_S)."""
    timeout_s = SEARCH_TIMEOUT_S if timeout_s is None else timeout_s
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

    tasks = [asyncio.create_task(probe(h)) for h in hosts]
    try:
        done, pending = await asyncio.wait(tasks, timeout=timeout_s) if tasks else (set(), set())
        for t in pending:  # the search never lasts more than timeout_s: what answered
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    finally:
        if own:
            await http.aclose()
    found = [t.result() for t in done if not t.cancelled() and t.exception() is None]
    return sorted((f for f in found if f), key=lambda d: ipaddress.IPv4Address(d["host"]))
