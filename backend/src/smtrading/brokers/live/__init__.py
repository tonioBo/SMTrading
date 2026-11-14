"""Real-money brokers.

LIVE_KINDS: the BrokerLink kinds that move real money.
LIVE_HOSTS: the hostnames of the live adapters' BASE_URL class constants (computed on
first use, lazily, to avoid import cycles). Nothing outside brokers/live spells a live
host: whoever needs one imports LIVE_HOSTS (a test scans the repository for it).
"""

from urllib.parse import urlsplit

from smtrading.common.errors import ErrorCode, SMTError

LIVE_KINDS = frozenset({"alpaca_live", "t212_live"})
GATE_REQUIRED_MESSAGE = "gli adattatori live richiedono il cancello"
PAPER_LIVE_HOST_MESSAGE = "host di produzione non ammesso per un adattatore paper"


def _live_hosts() -> frozenset[str]:
    from smtrading.brokers.live.alpaca_live import AlpacaLiveAdapter
    from smtrading.brokers.live.t212_live import T212LiveAdapter

    return frozenset(
        str(urlsplit(cls.BASE_URL).hostname) for cls in (AlpacaLiveAdapter, T212LiveAdapter)
    )


def __getattr__(name: str):
    if name == "LIVE_HOSTS":
        hosts = _live_hosts()
        globals()["LIVE_HOSTS"] = hosts
        return hosts
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def hostname(url: object) -> str | None:
    """The lower-case hostname of `url` (with or without a scheme), None if it has none."""
    text = str(url).strip()
    try:
        parts = urlsplit(text if "//" in text else f"//{text}")
        host = parts.hostname
    except ValueError:
        return None
    return host.rstrip(".") if host else None


def refuse_live_host(config: dict) -> None:
    """Paper/demo adapters: a base URL on a live host is refused (FORBIDDEN), whatever
    the rest of the configuration. The message never contains the host."""
    base = config.get("base_url")
    if base and hostname(base) in __getattr__("LIVE_HOSTS"):
        raise SMTError(ErrorCode.FORBIDDEN, PAPER_LIVE_HOST_MESSAGE)


def require_live_construction(cls: type, config: dict, http: object) -> None:
    """Live adapters: the base URL is the class constant (a different `base_url` is
    FORBIDDEN) and `http` is a LiveClient built by make_live_client whose only transport
    is its LiveGateTransport (anything else is FORBIDDEN)."""
    from smtrading.brokers.live.gate import LiveClient, LiveGateTransport

    base = config.get("base_url")
    if base and str(base).rstrip("/") != cls.BASE_URL:
        raise SMTError(ErrorCode.FORBIDDEN, f"{cls.kind}: base URL non consentito")
    if (
        not isinstance(http, LiveClient)
        or not isinstance(http.gate, LiveGateTransport)
        or http._transport is not http.gate
        or http._mounts
    ):
        raise SMTError(ErrorCode.FORBIDDEN, GATE_REQUIRED_MESSAGE)
