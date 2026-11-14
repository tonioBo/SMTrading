from typing import ClassVar

from smtrading.brokers.alpaca_core import (  # noqa: F401  (re-exported)
    _STATUS,
    _AlpacaProtocol,
    from_alpaca_symbol,
    to_alpaca_symbol,
)
from smtrading.brokers.live import refuse_live_host
from smtrading.brokers.registry import register

PAPER_URL = "https://paper-api.alpaca.markets"


@register
class AlpacaPaperAdapter(_AlpacaProtocol):
    kind: ClassVar[str] = "alpaca_paper"
    BASE_URL: ClassVar[str] = PAPER_URL

    def __init__(self, config: dict, http) -> None:
        refuse_live_host(config)  # never a real-money host, whatever the message below
        super().__init__(config, http)
