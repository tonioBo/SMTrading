from dataclasses import dataclass, field
from decimal import Decimal
from typing import ClassVar, Literal

from smtrading.engine.models import Order, OrderType
from smtrading.instruments.models import AssetClass, Instrument

BrokerStatus = Literal["accepted", "partially_filled", "filled", "canceled", "rejected", "expired"]


@dataclass
class BrokerOrder:
    broker_order_id: str
    status: BrokerStatus
    filled_qty: Decimal
    avg_fill_price: Decimal | None
    message: str | None
    raw: dict = field(default_factory=dict)


class BrokerAdapter:
    """Paper/demo broker connection. Adapters never talk to real-money endpoints.

    Errors: order rejections come back as BrokerOrder(status="rejected"); transport and 5xx
    failures raise RuntimeError (retryable). Error text never contains credentials.
    """

    kind: ClassVar[str]
    supported_types: ClassVar[frozenset[OrderType]]
    supported_asset_classes: ClassVar[frozenset[AssetClass]]

    def __init__(self, config: dict, http) -> None:
        self.config = config
        self.http = http

    def supports(self, order: Order, instrument: Instrument) -> bool:
        return (
            order.type in self.supported_types
            and instrument.asset_class in self.supported_asset_classes
        )

    async def place_order(self, order: Order, instrument: Instrument) -> BrokerOrder:
        raise NotImplementedError

    async def get_order(self, broker_order_id: str) -> BrokerOrder:
        raise NotImplementedError

    async def cancel_order(self, broker_order_id: str) -> None:
        raise NotImplementedError

    async def get_positions(self) -> dict[str, Decimal]:
        raise NotImplementedError

    async def health_check(self) -> None:
        raise NotImplementedError
