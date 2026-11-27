import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import ClassVar, Literal

import httpx
from sqlalchemy import inspect

from smtrading.brokers.tradable import Tradability, buyable, not_found
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
    # the share of each fill the broker keeps as its fee, when it charges one by rate
    # rather than reporting it (Alpaca crypto): taken out of the asset bought on a buy
    # (the account receives less than the filled quantity), out of the proceeds on a
    # sell. None: `fees` or the fee rules apply
    fee_rate: Decimal | None = None


@dataclass(frozen=True)
class BrokerOpenOrder:
    """An order still open at the broker, as its open-orders list shows it (reconciliation:
    read with GET requests only)."""

    broker_order_id: str
    symbol: str  # canonical when the adapter can map it, else the broker's own
    side: Literal["buy", "sell"]
    qty: Decimal | None  # the open (unfilled) quantity; None: an amount order
    limit_price: Decimal | None
    stop_price: Decimal | None  # None for a trailing stop (its level moves)


# plan 29: the resting types SMTrading keeps and watches itself on a market-only link
SYNTHETIC_TYPES = frozenset(
    {OrderType.limit, OrderType.stop, OrderType.stop_limit, OrderType.trailing_stop}
)


def market_view(order: Order) -> Order:
    """A transient copy of `order` as a market order (same id, quantity, side, client
    reference): what BrokerSync sends for a triggered synthetic order. Never added to a
    session. Only the loaded columns are copied (no lazy load: the order may be detached;
    a column expired by a commit, such as `updated_at`, is left out)."""
    loaded = inspect(order).dict
    view = Order(**{a.key: loaded[a.key] for a in Order.__mapper__.column_attrs
                    if a.key in loaded})
    view.type = OrderType.market
    view.limit_price = view.stop_price = view.trail_pct = view.trail_amount = None
    return view


class RateLimited(RuntimeError):  # retryable, like any RuntimeError
    """The broker refused the request for its rate limit (HTTP 429): not a rejection,
    and the request was not processed."""


class BrokerUnreachable(RuntimeError):  # retryable, like any RuntimeError
    """The broker did not answer at the network level (connect or read timeout, connection
    refused, DNS): expected during an outage, logged as a warning without traceback.
    Its text is the same as a plain RuntimeError's (`<kind>: <httpx error name>`).
    `connect`: the connection itself failed (connect timeout, refused, DNS): the request
    never reached the broker, and the link's other calls would fail the same way."""

    def __init__(self, message: str, *, connect: bool = False) -> None:
        super().__init__(message)
        self.connect = connect


# the gateway in front of the broker answered that the broker did not (an outage)
_GATEWAY_TEXT = re.compile(r"\bHTTP (502|503|504)\b")


def transport_error(kind: str, e: Exception) -> RuntimeError:
    """The adapter error for an httpx failure (never its text: it may carry secrets):
    BrokerUnreachable for a timeout or a network error (not a full local connection pool:
    PoolTimeout), a plain RuntimeError otherwise (protocol errors and the like)."""
    text = f"{kind}: {type(e).__name__}"
    if isinstance(e, httpx.ConnectTimeout | httpx.ConnectError):
        return BrokerUnreachable(text, connect=True)
    if isinstance(e, httpx.TimeoutException | httpx.NetworkError) and not isinstance(
            e, httpx.PoolTimeout):
        return BrokerUnreachable(text)
    return RuntimeError(text)


def is_unreachable(e: BaseException) -> bool:
    """The broker did not answer: a BrokerUnreachable, or an HTTP 502/503/504 of its
    gateway (adapters' text `HTTP <code>`). An answer (401/403, 400, 500...) is not."""
    return isinstance(e, BrokerUnreachable) or (
        isinstance(e, RuntimeError) and _GATEWAY_TEXT.search(str(e)) is not None)


class OutcomeUnknown(RuntimeError):
    """A real order's state cannot be read safely (e.g. an eToro lookup without the
    asset's leverage and settlement): BrokerSync marks its outcome unknown (reduce-only)
    and keeps reading it; never a rejection."""


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


@dataclass(frozen=True)
class BrokerFee:
    """A fee the broker charged apart from any order or fill (Alpaca: the account
    activity FEE, the regulatory fees on sells and the CAT fee, posted the night after the
    trades, with no symbol and no order). Booked by brokers.fee_activities."""

    id: str  # the broker's activity id (unique per account)
    trade_date: date  # the trading day of the trades it is for
    amount: Decimal  # > 0, in `currency`
    currency: str
    kind: str | None  # the broker's sub type (REG, CAT, TAF...)
    description: str


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
    # the account's currency when the broker has only one (eToro: USD), else None (read
    # from the account snapshot); the currency of the broker's fees
    ACCOUNT_CURRENCY: ClassVar[str | None] = None

    def __init__(self, config: dict, http) -> None:
        self.config = config
        self.http = http

    # the text of a refused sell beyond the position on a link that cannot short (eToro):
    # a synthetic sell (plan 29) that would open a short is refused with it at placement
    SELL_BEYOND_POSITION_MESSAGE: ClassVar[str | None] = None

    @property
    def market_only(self) -> bool:
        """The broker takes only market orders (eToro; a Trading 212 link configured so):
        resting orders are kept and watched by SMTrading (plan 29, `Order.synthetic`)."""
        return self.supported_types == frozenset({OrderType.market})

    def synthetic(self, order: Order, instrument: Instrument) -> bool:
        """Plan 29: `order` is kept and watched here, then sent as a market order at its
        trigger: a resting type on a market-only link that takes the same order at market.
        No network I/O."""
        return (
            self.market_only and order.type in SYNTHETIC_TYPES
            and self.routable(market_view(order), instrument)
        )

    def supports(self, order: Order, instrument: Instrument) -> bool:
        return (
            order.type in self.supported_types
            and instrument.asset_class in self.supported_asset_classes
        )

    def routable(self, order: Order, instrument: Instrument) -> bool:
        """Placement-time routing decision, without network I/O. May be optimistic when
        it needs data only `prepare` loads; BrokerSync checks `supports` before sending."""
        return self.supports(order, instrument)

    def renewed_daily(self, order: Order, instrument: Instrument) -> bool:
        """The broker takes this `gtc` order only as a `day` order (Alpaca: fractional
        quantities): it is sent as `day` and, when the broker expires it unfilled at the
        session end, BrokerSync sends it again at the next session (`_renew`), keeping
        it open locally as `gtc`. No network I/O."""
        return False

    def unsupported_reason(self, order: Order, instrument: Instrument) -> str | None:
        """Why `routable` refuses the order, in a text for the user (None: the generic
        «not supported» message). No network I/O."""
        return None

    async def prepare(self, instruments: Iterable[Instrument] = ()) -> None:
        """Load what `supports` and the symbol mapping need (network); no-op by default."""

    async def tradability(self, instruments: Iterable[Instrument]) -> dict[str, Tradability]:
        """What the broker lets SMTrading do with each instrument, by symbol (network
        allowed; plan 44). Raises RuntimeError when the broker cannot answer at all.
        Default: by asset class only."""
        return {i.symbol: (buyable(short=None) if i.asset_class in self.supported_asset_classes
                           else not_found("asset_class")) for i in instruments}

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

    async def asset_balance(self, symbol: str, instrument: Instrument
                            ) -> tuple[Decimal, Decimal] | None:
        """(quantity held, quantity free to sell) of `symbol` in the broker account, for a
        broker whose fee in the asset leaves the account holding less than the fills
        (Alpaca crypto: a sell is sized to what is there, see BrokerSync); None: not
        read (no network I/O)."""
        return None

    async def load_fee_dust(self, symbols: Iterable[str]) -> None:
        """Load what `fee_dust` needs (network); no-op by default."""

    def fee_dust(self, symbol: str, qty: Decimal) -> Decimal:
        """The quantity difference the broker's fee in the asset can leave between the
        account and the fills booked here for a position of `qty` (Alpaca crypto: the
        rate charged differs from the one applied, rounding, a residue below the minimum
        order size); 0 for a broker that takes no fee in the asset. No network I/O."""
        return Decimal(0)

    async def get_open_orders(self) -> list[BrokerOpenOrder] | None:
        """The orders open at the broker, read with GET requests only; None: this broker
        does not list them (the reconciliation then does not compare the orders)."""
        return None

    async def fee_activities(self, since: date) -> list[BrokerFee]:
        """The fees the broker charged apart from the orders from `since` on (GET only);
        none by default (a broker whose fees come with its orders, or none)."""
        return []

    async def health_check(self) -> None:
        raise NotImplementedError
