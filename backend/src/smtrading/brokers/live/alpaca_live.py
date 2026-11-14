"""Alpaca live (real money). Built only on a LiveClient: every request goes through the
live gate. The URL is this class constant, never configuration.

Docs: https://docs.alpaca.markets/reference (orders, positions, account) and
https://docs.alpaca.markets/docs/fractional-trading (fractional/notional: day orders).
"""

from typing import ClassVar

from smtrading.brokers.alpaca_core import _AlpacaProtocol
from smtrading.brokers.live import require_live_construction
from smtrading.brokers.registry import register
from smtrading.engine.models import Order, OrderType, TimeInForce
from smtrading.instruments.models import AssetClass, Instrument


def _flag(value: object) -> bool:
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


@register
class AlpacaLiveAdapter(_AlpacaProtocol):
    """Config: `key_id`, `secret_key`, `crypto_enabled` (default false: live crypto is
    off until the account snapshot reports it active)."""

    kind: ClassVar[str] = "alpaca_live"
    live: ClassVar[bool] = True
    BASE_URL: ClassVar[str] = "https://api.alpaca.markets"

    def __init__(self, config: dict, http) -> None:
        require_live_construction(type(self), config, http)
        super().__init__(config, http)
        self.crypto_enabled = _flag(config.get("crypto_enabled", False))

    def supports(self, order: Order, instrument: Instrument) -> bool:
        if not super().supports(order, instrument):
            return False
        if instrument.asset_class is AssetClass.crypto:
            return self.crypto_enabled
        if order.notional is not None:  # notional: market day orders only
            return order.type is OrderType.market and order.tif is TimeInForce.day
        fractional = order.qty is not None and order.qty != order.qty.to_integral_value()
        if fractional:  # fractional shares: day orders, never a trailing stop
            return order.tif is TimeInForce.day and order.type is not OrderType.trailing_stop
        return True
