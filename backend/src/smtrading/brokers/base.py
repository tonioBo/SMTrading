from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
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

    `idempotent_place`: a place_order retried after a failure cannot create a second
    order (Alpaca: client_order_id). When False (Trading 212, Ruling R7) a failed send
    leaves the outcome unknown and BrokerSync looks for it with `find_order`.
    """

    kind: ClassVar[str]
    idempotent_place: ClassVar[bool] = True
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

    def routable(self, order: Order, instrument: Instrument) -> bool:
        """Placement-time routing decision, without network I/O. May be optimistic when
        it needs data only `prepare` loads; BrokerSync checks `supports` before sending."""
        return self.supports(order, instrument)

    async def prepare(self, instruments: Iterable[Instrument] = ()) -> None:
        """Load what `supports` and the symbol mapping need (network); no-op by default."""

    @staticmethod
    def client_ref(order: Order) -> str:
        """The broker client reference of the order's current submission."""
        attempt = getattr(order, "broker_attempt", 0) or 0
        return str(order.id) if attempt == 0 else f"{order.id}-{attempt}"

    async def place_order(self, order: Order, instrument: Instrument) -> BrokerOrder:
        raise NotImplementedError

    async def find_order(
        self, order: Order, instrument: Instrument, since: datetime,
        exclude: frozenset[str] = frozenset(),
    ) -> BrokerOrder | None:
        """The broker order created for `order`'s current submission at or after
        `since`, if one can be identified unambiguously, ignoring the broker order ids in
        `exclude` (already linked to other orders). Used after a send whose outcome is
        unknown; None means "not found" (definitive only for idempotent adapters)."""
        return None

    async def get_order(self, broker_order_id: str) -> BrokerOrder:
        raise NotImplementedError

    async def cancel_order(self, broker_order_id: str) -> None:
        raise NotImplementedError

    async def get_positions(self) -> dict[str, Decimal]:
        raise NotImplementedError

    async def health_check(self) -> None:
        raise NotImplementedError
