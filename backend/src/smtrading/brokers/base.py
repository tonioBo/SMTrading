from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import ClassVar, Literal

from smtrading.engine.models import Order, OrderType
from smtrading.instruments.models import AssetClass, Instrument

# the reject message of an order the broker refused without saying why (BrokerSync)
BROKER_REJECTED_MESSAGE = "rejected by the broker"

BrokerStatus = Literal["accepted", "partially_filled", "filled", "canceled", "rejected", "expired"]


@dataclass
class BrokerOrder:
    broker_order_id: str
    status: BrokerStatus
    filled_qty: Decimal
    avg_fill_price: Decimal | None
    message: str | None
    raw: dict = field(default_factory=dict)
    # fees reported by the broker, in the account currency (None: not reported)
    fees: Decimal | None = None


class RateLimited(RuntimeError):  # retryable, like any RuntimeError
    """The broker refused the request for its rate limit (HTTP 429): not a rejection,
    and the request was not processed."""


@dataclass(frozen=True)
class AccountSnapshot:
    """A read-only view of the broker account (GET requests only)."""

    account_id: str
    account_type: str | None
    currency: str
    cash_available: Decimal
    positions_count: int
    open_orders_count: int
    crypto_enabled: bool | None
    order_types: frozenset[OrderType] | None
    raw: dict


class BrokerAdapter:
    """Broker connection. Paper/demo adapters never talk to real-money endpoints.
    Live adapters (brokers/live) talk to them only through the live gate.

    Errors: order rejections come back as BrokerOrder(status="rejected"); transport and 5xx
    failures raise RuntimeError (retryable). Error text never contains credentials.

    `idempotent_place`: a place_order retried after a failure cannot create a second
    order (Alpaca: client_order_id). When False (Trading 212, Ruling R7) a failed send
    leaves the outcome unknown and BrokerSync looks for it with `find_order`.
    """

    kind: ClassVar[str]
    live: ClassVar[bool] = False
    BASE_URL: ClassVar[str]  # a class constant, never read from the configuration
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

    def unsupported_reason(self, order: Order, instrument: Instrument) -> str | None:
        """Why `routable` refuses the order, in a text for the user (None: the generic
        «not supported» message). No network I/O."""
        return None

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
        self,
        order: Order,
        instrument: Instrument,
        since: datetime,
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

    async def get_account(self) -> AccountSnapshot:
        """The account snapshot, read with GET requests only."""
        raise NotImplementedError

    async def positions_after_account(self) -> dict[str, Decimal]:
        """The positions, right after `get_account` (an adapter whose account read
        already fetched them reuses that answer: one positions read per pass)."""
        return await self.get_positions()

    async def health_check(self) -> None:
        raise NotImplementedError
