"""eToro demo (virtual money). Same host as the real API: the demo is told apart by the
path (`/demo/`), and `_EtoroProtocol._guard_path` refuses any other execution path
before it touches the network."""

from typing import ClassVar, Literal

from smtrading.brokers.etoro_core import ETORO_URL, MARKET_DATA_PREFIX, _EtoroProtocol
from smtrading.brokers.live import refuse_live_host
from smtrading.brokers.registry import register


@register
class EtoroDemoAdapter(_EtoroProtocol):
    """Config: `api_key` (x-api-key), `user_key` (x-user-key of the Demo environment)."""

    kind: ClassVar[str] = "etoro_demo"
    BASE_URL: ClassVar[str] = ETORO_URL
    ENV: ClassVar[Literal["demo", "real"]] = "demo"
    EXEC_V1: ClassVar[str] = "/api/v1/trading/execution/demo"
    EXEC_V2: ClassVar[str] = "/api/v2/trading/execution/demo"
    INFO_V1: ClassVar[str] = "/api/v1/trading/info/demo"
    INFO_V2: ClassVar[str] = "/api/v2/trading/info/demo"
    ALLOWED_PREFIXES: ClassVar[tuple[str, ...]] = (
        f"{EXEC_V1}/",
        f"{EXEC_V2}/",
        f"{INFO_V1}/",
        f"{INFO_V2}/",
        MARKET_DATA_PREFIX,
    )

    def __init__(self, config: dict, http) -> None:
        refuse_live_host(config)  # never a real-money host, whatever the message below
        super().__init__(config, http)
