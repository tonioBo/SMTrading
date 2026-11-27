"""BrokerSync: the only component that talks to brokers about orders; reconciliation.

The engine never calls a broker (no network I/O under any lock): it marks an order's
`broker_status` (engine.models.BrokerState) and BrokerSync carries it out every
`interval_s`, each order in its own short transactions; every network call happens
with no transaction open.

Single writer (I5): `run` holds a Postgres advisory lock; a worker that cannot take it
skips its passes. `sending` orders are only reclaimed by the first pass after taking
the lock (startup recovery).

Wake-up (intraday, mode B): between two full passes the lock holder listens on
WAKE_CHANNEL (one subscription on its own Redis connection, opened while it holds the
lock); a message carries the id of an order just committed (`wake_broker_sync`) and runs
`tick_orders` on those orders only, in the same task (never concurrently with a full
pass), after checking the lock connection again; then it waits for the rest of the
interval, so the full pass still runs every `interval_s` (a burst of wakes stops at the
interval's end: what is left goes to the full pass). An automatic order (`auto_rule_id`)
sent by a wake is read again every HOT_POLL_S for HOT_FOR_S (at most HOT_MAX at a time,
the broker's request limit), then only by the full passes. Without messages, or with
Redis unavailable (a plain sleep then), the passes are exactly those of before. The
lock holder writes HEARTBEAT_KEY (ISO UTC, TTL HEARTBEAT_TTL_S) after every pass; a
worker without the lock neither listens nor writes it.

- `to_send` (never attempted): once the market is open, the local checks run again
  (locked/reduce-only portfolio, kill switch, piani; not for liquidation orders). The order is
  claimed (`sending`, `broker_sent_at`, committed) and only then sent. A broker
  rejection rejects the order (PROVIDER_UNAVAILABLE + broker message) and notifies.
  Ruling R10: nothing falls back to the simulator on a linked portfolio: an order the
  adapter cannot take after `prepare`, or whose link is disabled, is rejected
  (notified); only an order whose portfolio the admin unlinked, never attempted, is
  released to the simulator.
- synthetic orders (plan 29: a market-only broker, eToro): a resting one is not here
  (kept and watched by the processor); once triggered it is sent as a market order
  (`market_view`), keeping its own type locally. A limit / stop-limit is first checked
  against the current quote (the processor's guards): no longer marketable (or no good
  quote) it goes back to the processor (`_watch_again`), never sent at any price. On a
  market-only link every sell is reduce-only at the send (`market_only_sell`): clamped to
  the portfolio's position less its sells already at the broker, waiting while another
  sell holds all of it, canceled (nothing_to_reduce) when flat;
- once a send was attempted (`broker_sent_at` set) the order is never expired,
  rejected or released before `find_order` (with every broker order id already linked
  on the link excluded, C1) gave an answer: found -> adopted (then canceled at the
  broker if it must not proceed: expired, link disabled/unlinked, kill switch, locked
  portfolio); not found -> for an idempotent adapter (Alpaca, by client_order_id) the
  answer is definitive and the order goes back to `to_send`; for Trading 212
  (Ruling R7) it is never resent and, after MATCH_WINDOW, rejected (or canceled when
  a cancel was requested) and left to reconciliation.
- live orders: `get_order`; a growth of filled_qty becomes a broker fill of the delta
  at `(avg_new * filled_new - avg_old * filled_old) / delta` (journal LIQUIDATION for
  liquidation orders). Without a broker price the fill waits; after
  FILL_PRICE_WAIT on a final order `broker.fill_price_missing` is notified once and
  it keeps waiting (no invented prices). Final states are applied. The daily loss
  check runs after the fill's commit, in its own transaction, on a memo prefetched
  before the portfolio lock.
- `cancel_requested`: cancel at the broker (an error is logged and retried, but the
  order is still read back so fills are never hidden, I2), apply the last fills and
  the final state; the order stays open until the broker confirms.
- `replace_requested` (modify): cancel; once canceled without fills it is sent again
  with a new client reference (broker_attempt + 1); if it filled, the fills stand.
- bracket exits at the broker are independent orders: when one fills, the cancel of
  its OCO sibling is requested and sent in the same pass.
- a fee charged by rate (`BrokerOrder.fee_rate`, Alpaca crypto): a buy books the net
  quantity (the filled one less the fee, which the broker keeps in the coin, rounded up
  to _COIN_STEP) and the fee's value; a sell its proceeds less the fee. A sell that
  completes leaving a residue here within the adapter's `fee_dust` (an earlier fee
  estimated low) takes the residue along, its value as fee; a sell beyond the position
  within it (a residue the account held, never booked) books only the position. Its
  bracket exits are sized to the net position (`activate_linked`).
- a sell on a broker with a fee in the asset (`adapter.asset_balance`, Alpaca crypto) is
  sized at the send, the account's balance read before the lock: down to the free
  quantity when it lacks no more than `fee_dust` (an exit is never refused for a fee
  residue); a full exit also takes the account's residue never booked here (the
  account's quantity beyond the linked portfolios', within `fee_dust`); when what is
  left is below the asset's minimum order size the residue is closed here, never sent
  (`write_off_dust`: its value as fee, no cash). A failed balance read: the order waits
  for the next pass.
- daily renewal: an order the broker takes only as `day` although it is `gtc` here
  (`adapter.renewed_daily`: Alpaca fractional quantities, resting types) is sent as
  `day`; when the broker expires it with nothing filled, it stays open here and goes
  back to `to_send` with a new client reference (broker_attempt + 1), sent at the next
  session open with every check of a first send (a reduce-only exit passes the kill
  switch on paper/demo links like any exit; a real-money link keeps its live gate: no
  send with the kill switch on, the order is then rejected). The expiry is final (no
  double exit) and the broker read is done before the resend; an OCO sibling filled
  meanwhile cancels it locally. Recorded as `broker.order_renewed` (activity, info).
  Expired after a partial fill it is not renewed (the fills stand): it expires and
  `broker.renewal_failed` is notified (the protection alarm then sees the gap).
- valid_until (plan 16): an order is never sent once its `expires_at` (the earlier of
  the day expiry and valid_until) has passed; one at the broker is canceled through
  `cancel_requested` (the processor asks for it) and a cancel confirmed once
  valid_until has passed is recorded as `expired` (`order.expired`, live note
  «scaduto»), the fills before it standing.

Real money (plan 8: an adapter with `live`, a LIVE_KINDS link). BrokerSync builds the
link's LiveClient itself (every request through the live gate, on this sessionmaker) and
knows which order requests passed the gate (they reached its inner transport):

- before claiming an order: link `armed`/`dry_run` (else LIVE_NOT_ARMED), the kill switch
  for every order but a live-flatten one (KILL_SWITCH_ACTIVE), the aggregate no-short
  check of a sell that is not reduce-only, then the aggregate cash cap of a buy (the
  other open buys ahead of it taken off, risk.live_checks) with a broker snapshot of at
  most SNAPSHOT_MAX_AGE_S not marked stale (read again with `get_account`, GET only,
  outside any transaction, at most once every ACCOUNT_READ_MIN_INTERVAL; the order waits
  meanwhile) and the fuse; a refusal rejects the order. Every quote and FX rate the
  checks need is prefetched into a frozen memo before the lock (no network I/O under it);
  a miss, or a quote outage, makes the order wait like a stale snapshot. An order still
  waiting after LIVE_WAIT_MAX (account unreadable, another account behind the keys, no
  quote) is rejected. Every accepted real buy marks the snapshot stale, so the next buy
  reads the account first. A never-sent order of a portfolio unlinked from a live link is
  rejected (never the simulator);
- `LiveDryRun`: rejected LIVE_DRY_RUN «prova a secco: ordine non inviato», the payload in
  its `live.order.dry_run` activity, no `broker_sent_at`;
- `LiveGateClosed` (the state changed after the checks): never sent, definitive:
  rejected with the gate's code, never `unknown`; any other failure before the request
  reached the network (the gate's own database read): rejected PROVIDER_UNAVAILABLE,
  never `unknown`, never sent again; a RuntimeError before it (or a 429): retried next
  pass like a `prepare` failure. Only a request that passed the gate can be ambiguous;
- a send accepted: `live.order.sent` and live_audit `order_sent` (payload and response,
  never headers); then `live.order.partial|filled|canceled|rejected` for every outcome
  (the planner skips the plain order.* rows of these orders); a fill with a broker fee
  uses it (`fee_override`, the broker's cumulative fee less what earlier fills took,
  converted to the same currency);
- an unknown outcome of a live order (`_mark_unknown`, and again at `_give_up`, Ruling R7):
  the portfolio goes reduce-only at once (`trip_reduce_only(cause="order_unknown")`, a
  critical alert) besides the rejection; `live.order.sent` is recorded only once the
  broker order is linked to the order (`_adopt`);
- `_must_stop`: an order found at the broker on a link no longer armed/dry run, or with
  the kill switch on (not flatten), is canceled.

«Piani» (goals.plans): an order that would add to a position of an ended piano still
being closed, or turn it, is never sent (rejected RISK_LIMIT_EXCEEDED, plan_grace, by the
send-time checks); one already at the broker is canceled when read (`cancel_plan_additions`,
«plan.orders_canceled» activity), like an adopted one that must not proceed. Its fills
before the cancel stand (they happened at the broker).

`reconcile` compares the broker's positions with ours; it never corrects anything (a
difference is recorded in the activity feed; from *Riconcilia ora* it is sent to Telegram
only when it differs from the portfolio's previous reconciliation). The periodic
`reconcile_all` (scheduler, every 5 minutes) records it quietly, never on Telegram: the
owner's alerts on broker differences come only from the reconciliation job
(`smtrading.reconciliation`). A portfolio no agent can access (the owner's alone) gets no
activity row at all: a changed difference goes to the owner on Telegram instead.
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import ROUND_UP, Decimal, localcontext
from uuid import UUID

import httpx
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.activity.mapping import ActivityDraft
from smtrading.activity.models import ActivityLevel
from smtrading.activity.service import record_activity
from smtrading.agents.access import portfolios_with_agents
from smtrading.autorules.broker_times import is_automatic, record_broker_fill_time
from smtrading.brokers.base import (
    BROKER_REJECTED_MESSAGE,
    BrokerAdapter,
    BrokerOrder,
    OutcomeUnknown,
    RateLimited,
    market_view,
)
from smtrading.brokers.live import LIVE_KINDS
from smtrading.brokers.live.account import account_fingerprint, snapshot_record
from smtrading.brokers.live.audit import record_live_audit
from smtrading.brokers.live.gate import (
    ORDER_EXTENSION,
    READ_METHODS,
    LiveClient,
    LiveDryRun,
    LiveGateClosed,
    _default_inner,
    _payload,
    make_live_client,
)
from smtrading.brokers.live.protect import record_live_order_activity, trip_reduce_only
from smtrading.brokers.models import BrokerLink, Reconciliation
from smtrading.brokers.registry import adapter_class
from smtrading.brokers.service import get_adapter, open_adapter
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.execution import (
    NOTHING_TO_REDUCE,
    WAIT,
    _held,
    activate_linked,
    broker_send_check,
    credited_qty,
    execute_fill,
    market_only_sell,
    write_off_dust,
)
from smtrading.engine.models import (
    LIQUIDATION_ROLE,
    OPEN_STATUSES,
    BrokerState,
    Fill,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
)
from smtrading.engine.prefetch import prefetch_portfolio
from smtrading.engine.pricing import QuoteView, effective_bid_ask
from smtrading.engine.quotesource import MemoQuoteSource, QuoteSource
from smtrading.engine.service import (
    cancel_held_children,
    cancel_plan_additions,
    order_closed,
    reject_broker_order,
)
from smtrading.events import (
    discard_post_commit,
    dispatch_post_commit,
    notify,
    queue_event,
)
from smtrading.fees.resolver import load_rules
from smtrading.instruments.market_hours import market_status
from smtrading.instruments.models import Instrument
from smtrading.ledger.models import Account, AccountBalance, AccountKind, JournalType
from smtrading.marketdata.types import Quote
from smtrading.portfolios.locks import lock_portfolio
from smtrading.portfolios.models import Portfolio
from smtrading.risk.daily_loss import check_daily_loss
from smtrading.risk.live_checks import (
    EUR,
    SENDABLE_STATES,
    check_live_cash,
    check_live_fuse,
    check_live_state,
    check_no_short,
    estimate_buy,
    mark_snapshot_stale,
    snapshot_fresh,
    to_currency,
)
from smtrading.risk.models import Notification
from smtrading.risk.settings import (
    get_default_spread_bps,
    get_stale_threshold,
    is_kill_switch_on,
)
from smtrading.telegram.outbox import enqueue

log = logging.getLogger("smtrading.brokers.sync")

SYNC_INTERVAL_S = 2.0
WAKE_CHANNEL = "brokersync:wake"
HEARTBEAT_KEY = "brokersync:last_pass"
HEARTBEAT_TTL_S = 30
HOT_POLL_S = 0.5
HOT_FOR_S = 10
HOT_MAX = 4
_WAKE_BATCH = 100  # wake messages read at once (the rest at the next wait)
MATCH_WINDOW = timedelta(minutes=5)
FILL_PRICE_WAIT = timedelta(minutes=10)
# a liquidation still waiting on the symbol's other broker orders after this: notify once
LIQUIDATION_WAIT_NOTIFY = timedelta(minutes=10)
ADVISORY_LOCK_KEY = 0x534D5442524B  # "SMTBRK"
UNSUPPORTED = "Ordine non supportato dal broker collegato"
# demo kinds whose fills take the broker's fee, as their real account does (eToro: the
# demo reports the fees of the real price list); the others take the fee rules
BROKER_FEE_DEMO_KINDS = frozenset({"etoro_demo"})
_SYNCED = tuple(s for s in OrderStatus if s in OPEN_STATUSES - {OrderStatus.held})
_FINAL = {"canceled": OrderStatus.canceled, "rejected": OrderStatus.rejected,
          "expired": OrderStatus.expired}
_PREC = 60
_LIMIT_TYPES = (OrderType.limit, OrderType.stop_limit)
ACCOUNT_READ_MIN_INTERVAL = timedelta(seconds=5)  # Trading 212: 1 account read / 5 s
DRY_RUN_MESSAGE = "prova a secco: ordine non inviato"
NOT_SENT_MESSAGE = "cancello live non disponibile: ordine non inviato"
LIVE_UNLINKED_MESSAGE = "collegamento reale rimosso: ordine non inviato"
NOT_ARMED_STOP = "denaro reale non attivo"
_STALE = object()  # a real buy waits for a fresh broker snapshot
_WAIT = object()  # a real order waits for a quote / FX rate (not prefetched, outage)
_NO_BALANCE = object()  # a sell waits for the broker's balance of the asset (read failed)
_COIN_STEP = Decimal("1e-9")  # Alpaca's quantity increment of a crypto pair
LIVE_WAIT_MAX = timedelta(minutes=30)
WAIT_TOO_LONG_MESSAGE = ("dati del conto o quotazioni non disponibili da 30 minuti:"
                         " ordine non inviato")


class _SentRecorder(httpx.AsyncBaseTransport):
    """The inner transport of BrokerSync's live clients: notes the body of every order
    request (non-GET with `smt_order_id`) that passed the gate, i.e. could reach the
    broker, in order (an eToro sell sends one close per position). An order whose
    request is not noted was never sent."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner
        self.reached: dict[str, list[dict]] = {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        order_id = request.extensions.get(ORDER_EXTENSION)
        if order_id is not None and request.method.upper() not in READ_METHODS:
            await request.aread()
            self.reached.setdefault(str(order_id), []).append(_payload(request))
        return await self._inner.handle_async_request(request)


def _sent_payload(sent: list[dict] | None) -> dict | None:
    """The live audit's request payload: the one request, or {"requests": [...]} for
    several (an eToro sell closing several positions)."""
    if not sent:
        return None
    return sent[0] if len(sent) == 1 else {"requests": sent}

    async def aclose(self) -> None:
        await self._inner.aclose()


def delta_price(order: Order, bo: BrokerOrder, delta: Decimal) -> Decimal:
    """Price of the newly filled `delta`, from the broker's and our average prices."""
    assert bo.avg_fill_price is not None
    with localcontext() as ctx:
        ctx.prec = _PREC
        before = order.filled_qty or Decimal(0)
        old_avg = order.avg_fill_price or Decimal(0)
        price = (bo.avg_fill_price * bo.filled_qty - old_avg * before) / delta
        price = price.quantize(Decimal("1e-18")).normalize()
    if price <= 0:
        log.warning(
            "order %s: delta price %s <= 0 (broker avg %s x %s, ours %s x %s); using the"
            " broker average", order.id, price, bo.avg_fill_price, bo.filled_qty,
            old_avg, before,
        )
        return bo.avg_fill_price
    return price


async def _lock(session: AsyncSession, order_id: UUID) -> Order | None:
    """Portfolio row first, then the order row (Ruling R11)."""
    portfolio_id = await session.scalar(select(Order.portfolio_id).where(Order.id == order_id))
    if portfolio_id is None:
        return None
    await lock_portfolio(session, portfolio_id)
    return (
        await session.execute(
            select(Order).where(Order.id == order_id).with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def _linked_ids(session: AsyncSession, link_id: UUID, but: UUID) -> frozenset[str]:
    """Broker order ids already linked to other orders on `link_id` (C1)."""
    return frozenset(await session.scalars(
        select(Order.broker_order_id).where(
            Order.broker_link_id == link_id, Order.broker_order_id.is_not(None),
            Order.id != but,
        )
    ))


async def wake_broker_sync(redis, order_id: UUID) -> None:
    """Asks BrokerSync to carry out `order_id` now; call it after the order's commit."""
    await redis.publish(WAKE_CHANNEL, str(order_id))


def _wake_ids(messages: list[dict]) -> list[UUID]:
    """The order ids of the wake messages, deduplicated, in order (others ignored)."""
    ids: dict[UUID, None] = {}
    for message in messages:
        if message.get("type") != "message":
            continue
        data = message.get("data")
        try:
            ids[UUID(data.decode() if isinstance(data, bytes) else str(data))] = None
        except (ValueError, UnicodeDecodeError):
            log.warning("broker sync: wake message ignored (not an order id)")
    return list(ids)


class BrokerSync:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        redis,
        qs: QuoteSource,
        http,
        *,
        interval_s: float = SYNC_INTERVAL_S,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sm = sessionmaker
        self._redis = redis
        self._qs = qs
        self._http = http
        self._interval_s = interval_s
        self._clock = clock
        self._adapters: dict[UUID, BrokerAdapter] = {}
        self._followups: list[UUID] = []  # cancels to send in this pass
        self._loss_checks: set[UUID] = set()  # portfolios with a fill to check
        self._fill_qs: QuoteSource = qs  # the prefetched memo during an _update
        # real money: one LiveClient per link, kept across passes (the gate reads the
        # state again at every request), with the recorder of what passed the gate
        self._live_clients: dict[UUID, tuple[LiveClient, _SentRecorder]] = {}
        self._link_kinds: dict[UUID, str] = {}
        self._account_read_at: dict[UUID, datetime] = {}
        # real orders waiting for account data / quotes since (kept in memory: a restart
        # starts the wait again, never shorter)
        self._waiting: dict[UUID, datetime] = {}
        self._unknown_reads: set[UUID] = set()  # orders marked unknown by a read (_get_order)
        self._wake_sub = None  # the WAKE_CHANNEL subscription, while holding the lock
        self._wake_down = False  # its failure was logged
        self._heartbeat_down = False  # a heartbeat failure was logged (once until back)
        self._hot: dict[UUID, float] = {}  # automatic order sent by a wake -> hot until
        self._hot_next = 0.0  # loop time of the next hot read

    async def aclose(self) -> None:
        """Closes the per-link LiveClients and the wake subscription (run() does it when
        it ends)."""
        await self._close_wake()
        clients, self._live_clients, self._adapters = self._live_clients, {}, {}
        for client, _ in clients.values():
            with contextlib.suppress(Exception):
                await client.aclose()

    async def run(self) -> None:
        """Every interval_s a pass, while holding the advisory lock (single writer).
        The lock lives on a dedicated connection, checked before every pass: if that
        connection is lost (and the lock with it) the passes stop until it is retaken."""
        try:
            await self._run()
        finally:
            await self.aclose()

    async def _run(self) -> None:
        engine = self._sm.kw["bind"]
        warned = False
        loop = asyncio.get_running_loop()
        while True:
            try:
                async with engine.connect() as conn:
                    try:
                        held = False
                        while True:
                            passed = False  # a pass of the lock holder completed
                            if not held:
                                held = bool(await conn.scalar(
                                    text("select pg_try_advisory_lock(:k)"),
                                    {"k": ADVISORY_LOCK_KEY},
                                ))
                                await conn.commit()
                                if held:
                                    log.info("broker sync: lock acquired")
                                    warned = False
                                    passed = await self._pass(recover=True)
                                elif not warned:
                                    log.warning(
                                        "broker sync: another worker holds the lock; waiting"
                                    )
                                    warned = True
                            else:
                                await conn.scalar(text("select 1"))  # still holding it
                                await conn.commit()
                                passed = await self._pass()
                            if held:
                                next_pass = loop.time() + self._interval_s
                                if passed:
                                    await self._heartbeat()
                                await self._between_passes(conn, next_pass)
                            else:
                                await asyncio.sleep(self._interval_s)
                    finally:
                        await self._close_wake()  # no lock, no listening
                        self._hot.clear()
                        # never return a connection that may hold the lock to the pool
                        with contextlib.suppress(Exception):
                            await conn.invalidate()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("broker sync: lock connection lost; reconnecting")
                await asyncio.sleep(self._interval_s)

    async def _pass(self, *, recover: bool = False) -> bool:
        try:
            await self.tick(recover=recover)
        except Exception:
            log.exception("broker sync pass failed")
            return False
        return True

    async def _heartbeat(self) -> None:
        """HEARTBEAT_KEY = now (ISO UTC): the lock holder completed a pass (its time is
        part of the interval). With Redis down, one warning until it is written again."""
        try:
            await asyncio.wait_for(self._redis.set(
                HEARTBEAT_KEY, datetime.now(UTC).isoformat(), ex=HEARTBEAT_TTL_S,
            ), timeout=self._interval_s)
        except Exception:
            if not self._heartbeat_down:
                log.warning("broker sync: heartbeat not written", exc_info=True)
                self._heartbeat_down = True
            return
        if self._heartbeat_down:
            log.info("broker sync: heartbeat written again")
            self._heartbeat_down = False

    # -- wake-up between passes (lock held) ------------------------------------------

    async def _between_passes(self, conn, deadline: float) -> None:
        """What `asyncio.sleep(interval_s)` was: waits for the interval, carrying out the
        woken orders and the hot reads meanwhile until loop time `deadline` (the next
        full pass), never delayed past it by them (they stop there)."""
        loop = asyncio.get_running_loop()
        await self._subscribe(deadline - loop.time())
        while (left := deadline - loop.time()) > 0:
            if self._hot:
                left = min(left, max(self._hot_next - loop.time(), 0.0))
            ids = await self._wait(left)
            hot_due = bool(self._hot) and loop.time() >= self._hot_next
            if not ids and not hot_due:
                continue
            await conn.scalar(text("select 1"))  # still holding the lock (single writer)
            await conn.commit()
            try:
                if ids:
                    await self._tick_ids(ids, until=deadline, heat=True)
                if hot_due:
                    await self._read_hot(until=deadline)
            except Exception:
                log.exception("broker sync of woken orders failed")

    async def _subscribe(self, timeout: float) -> None:
        """Opens the WAKE_CHANNEL subscription once (a Redis error: none, the wait is
        a plain sleep until the next interval tries again)."""
        if self._wake_sub is not None or timeout <= 0:
            return
        sub = None
        try:
            sub = self._redis.pubsub()
            await asyncio.wait_for(sub.subscribe(WAKE_CHANNEL), timeout=timeout)
        except Exception:
            if not self._wake_down:
                log.warning("broker sync: wake channel unavailable; passes only",
                            exc_info=True)
                self._wake_down = True
            if sub is not None:
                with contextlib.suppress(Exception):
                    await sub.aclose()
            return
        self._wake_sub = sub
        if self._wake_down:
            log.info("broker sync: wake channel available again")
            self._wake_down = False

    async def _close_wake(self) -> None:
        sub, self._wake_sub = self._wake_sub, None
        if sub is not None:
            with contextlib.suppress(Exception):
                await sub.aclose()

    async def _wait(self, timeout: float) -> list[UUID]:
        """Up to `timeout` s for wake messages: the order ids received ([] at timeout).
        Without a subscription, or on its error, a plain sleep."""
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while (left := end - loop.time()) > 0:
            if self._wake_sub is None:
                await asyncio.sleep(left)
                return []
            try:
                message = await self._wake_sub.get_message(
                    ignore_subscribe_messages=True, timeout=left)
                if message is None:
                    continue  # a subscribe confirmation, or the timeout
                messages = [message]
                while len(messages) < _WAKE_BATCH and (message := await self._wake_sub
                                                       .get_message(timeout=0)):
                    messages.append(message)
            except Exception:
                log.warning("broker sync: wake channel lost; passes only", exc_info=True)
                self._wake_down = True
                await self._close_wake()
                continue
            if ids := _wake_ids(messages):
                return ids
        return []

    async def tick_orders(self, ids: list[UUID]) -> None:
        """A pass over the given orders only (those still open at a broker), like `tick`."""
        await self._tick_ids(ids, heat=True)

    async def _tick_ids(self, ids: list[UUID], *, until: float | None = None,
                        heat: bool = False, sent_only: bool = False) -> set[UUID]:
        """`_step` on the given orders still open at a broker, oldest first (stopping at
        loop time `until`); with `heat`, an automatic order sent here becomes hot; with
        `sent_only`, only orders already at the broker (a read, never a send).
        Returns the ids found open."""
        if not ids:
            return set()
        async with self._sm() as session:
            rows = (await session.execute(
                select(Order.id, Order.broker_status, Order.broker_order_id,
                       Order.broker_sent_at, Order.auto_rule_id)
                .where(Order.id.in_(ids), Order.broker_link_id.is_not(None),
                       Order.status.in_(_SYNCED))
                .order_by(Order.created_at, Order.id)
            )).all()
        loop = asyncio.get_running_loop()
        for order_id, status, broker_order_id, sent_at, auto_rule_id in rows:
            if until is not None and loop.time() >= until:
                break  # the full pass is due: it takes the rest
            if sent_only and broker_order_id is None:
                continue
            try:
                await self._step(order_id, status, broker_order_id, sent_at, False)
            except Exception:
                log.exception("broker sync of order %s failed", order_id)
            while self._followups:
                follow = self._followups.pop(0)
                try:
                    await self._cancel(follow)
                except Exception:
                    log.exception("broker cancel of order %s failed", follow)
            if (heat and auto_rule_id is not None and broker_order_id is None
                    and order_id not in self._hot and len(self._hot) < HOT_MAX
                    and await self._at_broker(order_id)):
                if not self._hot:
                    self._hot_next = loop.time() + HOT_POLL_S
                self._hot[order_id] = loop.time() + HOT_FOR_S
        return {row[0] for row in rows}

    async def _at_broker(self, order_id: UUID) -> bool:
        async with self._sm() as session:
            return await session.scalar(
                select(Order.broker_order_id).where(Order.id == order_id)) is not None

    async def _read_hot(self, *, until: float) -> None:
        """Reads the hot orders again; those no longer open or past HOT_FOR_S cool down."""
        loop = asyncio.get_running_loop()
        now = loop.time()
        self._hot = {k: v for k, v in self._hot.items() if v > now}
        self._hot_next = now + HOT_POLL_S
        open_ids = await self._tick_ids(list(self._hot), until=until, sent_only=True)
        self._hot = {k: v for k, v in self._hot.items() if k in open_ids}

    async def tick(self, *, recover: bool = False) -> None:
        """One pass over the open broker-managed orders, oldest first. With `recover`
        (startup), `sending` orders left by a crash are reclaimed."""
        self._adapters = {}  # configs may change between passes
        async with self._sm() as session:
            rows = (await session.execute(
                select(Order.id, Order.broker_status, Order.broker_order_id,
                       Order.broker_sent_at)
                .where(Order.broker_link_id.is_not(None), Order.status.in_(_SYNCED))
                .order_by(Order.created_at, Order.id)
            )).all()
        open_ids = {row[0] for row in rows}  # the waits of orders no longer open end
        self._waiting = {k: v for k, v in self._waiting.items() if k in open_ids}
        for order_id, status, broker_order_id, sent_at in rows:
            try:
                await self._step(order_id, status, broker_order_id, sent_at, recover)
            except Exception:
                log.exception("broker sync of order %s failed", order_id)
            while self._followups:
                follow = self._followups.pop(0)
                try:
                    await self._cancel(follow)
                except Exception:
                    log.exception("broker cancel of order %s failed", follow)

    async def _step(self, order_id, status, broker_order_id, sent_at, recover) -> None:
        if status == BrokerState.SENDING:
            if recover:
                await self._recover_sending(order_id)
            return  # otherwise a send in flight
        if broker_order_id is not None:
            if status in (BrokerState.CANCEL_REQUESTED, BrokerState.REPLACE_REQUESTED):
                await self._cancel(order_id)
            else:
                await self._poll(order_id)
            return
        if sent_at is None and status == BrokerState.UNKNOWN:
            # a send attempted before broker_sent_at existed (pre-0011): attempted
            await self._update(order_id, self._backfill_sent_at)
            sent_at = True
        if sent_at is not None:
            await self._match(order_id)
        elif status == BrokerState.CANCEL_REQUESTED:  # never attempted: nothing at broker
            await self._update(order_id, self._cancel_unsent)
        else:
            await self._send(order_id)

    # -- helpers ---------------------------------------------------------------------

    async def _adapter(self, session: AsyncSession, link_id: UUID) -> BrokerAdapter:
        if link_id not in self._adapters:
            http = self._http
            if await self._is_live(session, link_id):
                http = self._live_client(link_id)[0]
            self._adapters[link_id] = await get_adapter(session, link_id, http)
        return self._adapters[link_id]

    def _live_client(self, link_id: UUID) -> tuple[LiveClient, _SentRecorder]:
        if link_id not in self._live_clients:
            recorder = _SentRecorder(_default_inner())
            self._live_clients[link_id] = (
                make_live_client(self._sm, link_id, inner=recorder), recorder)
        return self._live_clients[link_id]

    async def _link_kind(self, session: AsyncSession, link_id: UUID | None) -> str | None:
        """The link's kind (it never changes: cached), None for no or a missing link."""
        if link_id is None:
            return None
        if link_id not in self._link_kinds:
            kind = await session.scalar(select(BrokerLink.kind).where(BrokerLink.id == link_id))
            if kind is None:
                return None
            self._link_kinds[link_id] = kind
        return self._link_kinds[link_id]

    async def _is_live(self, session: AsyncSession, link_id: UUID | None) -> bool:
        """The link moves real money."""
        return await self._link_kind(session, link_id) in LIVE_KINDS

    async def _broker_fees(self, session: AsyncSession, link_id: UUID | None) -> bool:
        """A fill takes the broker's fee (`BrokerOrder.fees`): real money and the demo
        kinds that charge like their real account (BROKER_FEE_DEMO_KINDS)."""
        kind = await self._link_kind(session, link_id)
        return kind in LIVE_KINDS or kind in BROKER_FEE_DEMO_KINDS

    async def _live_note(self, session: AsyncSession, order: Order, phase: str, *,
                         bo: BrokerOrder | None = None, message: str | None = None,
                         payload: dict | None = None) -> None:
        """The live.order.<phase> activity of a real order (nothing for paper/demo)."""
        if await self._is_live(session, order.broker_link_id):
            await record_live_order_activity(session, order, phase, bo=bo,
                                             message=message, payload=payload)

    async def _commit(self, session: AsyncSession) -> None:
        await session.commit()
        await dispatch_post_commit(session, self._redis)

    async def _update(self, order_id: UUID, fn, bo: BrokerOrder | None = None) -> None:
        """Run `fn(session, locked order)` in its own transaction (open orders only),
        then the daily loss checks of the portfolios it filled. When `bo` brings new
        fills, the quotes/FX the fill needs are prefetched before any lock (I1)."""
        async with self._sm() as session:
            try:
                self._fill_qs = await self._prefetch_fill(session, order_id, bo)
                order = await _lock(session, order_id)
                if order is None or order.status not in _SYNCED:
                    return
                await fn(session, order)
                await session.commit()
            except BaseException:
                await session.rollback()
                discard_post_commit(session)
                self._loss_checks.clear()
                self._followups.clear()  # requested by the rolled back work
                raise
            finally:
                self._fill_qs = self._qs
            await dispatch_post_commit(session, self._redis)
        await self._run_loss_checks()

    async def _prefetch_fill(
        self, session: AsyncSession, order_id: UUID, bo: BrokerOrder | None
    ) -> QuoteSource:
        """A memo with what a broker fill of `bo` needs, fetched with no lock held
        (the read transaction is ended before returning); `self._qs` if no fill."""
        if bo is None or bo.avg_fill_price is None:
            return self._qs
        row = (await session.execute(
            select(Order.portfolio_id, Order.symbol, Order.filled_qty, Order.broker_link_id)
            .where(Order.id == order_id)
        )).first()
        if row is None or bo.filled_qty <= (row.filled_qty or Decimal(0)):
            await session.commit()
            return self._qs
        memo = MemoQuoteSource.wrap(self._qs)
        await prefetch_portfolio(session, memo, row.portfolio_id, symbols=[row.symbol])
        if bo.fees is not None and await self._broker_fees(session, row.broker_link_id):
            ccy = await self._account_currency(session, row.broker_link_id)  # the fee's
            if ccy:
                await memo.prefetch([], {(ccy, EUR)} - {(EUR, EUR)})
        await session.commit()
        return memo

    async def _run_loss_checks(self) -> None:
        """Daily loss check after a broker fill: own transaction, quotes prefetched
        before the portfolio lock (I1). Best-effort."""
        while self._loss_checks:
            portfolio_id = self._loss_checks.pop()
            try:
                async with self._sm() as session:
                    memo = MemoQuoteSource.wrap(self._qs)
                    await prefetch_portfolio(session, memo, portfolio_id, symbols=[])
                    portfolio = await lock_portfolio(session, portfolio_id)
                    await check_daily_loss(
                        session, memo, portfolio, self._clock(), redis=self._redis
                    )
                    await self._commit(session)
            except SMTError as e:
                log.warning("daily loss check of %s skipped: %s", portfolio_id, e.code)
            except Exception:
                log.exception("daily loss check of %s failed", portfolio_id)

    async def _reject(
        self, session: AsyncSession, order: Order, code: ErrorCode, message: str | None,
        *, phase: str = "rejected", payload: dict | None = None,
    ) -> None:
        """engine.service.reject_broker_order (held children, notify, untrack,
        liquidation unlock) plus the live.order.<phase> activity of a real order."""
        await reject_broker_order(session, order, code, message)
        await self._live_note(session, order, phase, message=message, payload=payload)
        log.warning("order %s rejected: %s %s", order.id, code, message)

    async def _set_final(self, session: AsyncSession, order: Order,
                         status: OrderStatus) -> None:
        order.status = status
        order.updated_at = datetime.now(UTC)
        queue_event(session, f"order.{status}", {
            "order_id": order.id, "portfolio_id": order.portfolio_id,
            "agent_id": order.agent_id, "symbol": order.symbol, "by": "broker",
        })
        await self._live_note(session, order, "canceled",
                              message="scaduto" if status == OrderStatus.expired else None)

    def _canceled(self, order: Order) -> OrderStatus:
        """The final state of a canceled order: `expired` once its valid_until passed."""
        if order.valid_until is not None and order.valid_until <= self._clock():
            return OrderStatus.expired
        return OrderStatus.canceled

    async def _closed(self, session: AsyncSession, order: Order) -> None:
        await order_closed(session, order)

    async def _refusal(
        self, session: AsyncSession, portfolio: Portfolio, order: Order, *, clamp: bool
    ) -> SMTError | None:
        """The fill-time local checks again, right before sending (Ruling R11; a
        liquidation skips the status checks only). A reduce_only order is clamped to the position
        (`clamp`); nothing to reduce is returned as an INVALID_ORDER error."""
        try:
            if not await broker_send_check(
                session, portfolio, order, clamp=clamp,
                status_checks=order.role != LIQUIDATION_ROLE,
            ):
                return SMTError(ErrorCode.INVALID_ORDER, NOTHING_TO_REDUCE)
        except SMTError as e:
            return e
        return None

    async def _must_stop(self, session: AsyncSession, order: Order) -> str | None:
        """Why an order found at the broker must not proceed, or None."""
        portfolio = await session.get(Portfolio, order.portfolio_id)
        link = await session.get(BrokerLink, order.broker_link_id)
        if order.expires_at is not None and order.expires_at <= self._clock():
            return "expired"
        if link is None or not link.enabled or portfolio.broker_link_id != link.id:
            return "collegamento al broker disattivato o rimosso"
        if link.kind in LIVE_KINDS:
            if link.live_state not in SENDABLE_STATES:
                return NOT_ARMED_STOP
            if not order.live_flatten and await is_kill_switch_on(session):
                return "kill switch attivo"
        refusal = await self._refusal(session, portfolio, order, clamp=False)
        return None if refusal is None else refusal.message

    async def _mark_unknown(self, session: AsyncSession, order: Order, reason: str) -> None:
        """The outcome of a send is unknown (Ruling R7). A real order may exist at the
        broker: the portfolio goes reduce-only at once, with its critical alert."""
        if order.broker_status == BrokerState.SENDING:
            order.broker_status = BrokerState.UNKNOWN
        order.updated_at = datetime.now(UTC)
        if await self._is_live(session, order.broker_link_id):
            await trip_reduce_only(session, order.portfolio_id, cause="order_unknown",
                                   details={"order_id": order.id, "symbol": order.symbol,
                                            "reason": reason})
        await notify(
            session, portfolio_id=order.portfolio_id, agent_id=order.agent_id,
            kind="broker.order_unknown",
            payload={"order_id": order.id, "symbol": order.symbol, "reason": reason},
        )
        log.error("order %s: broker outcome unknown (%s); not sent again", order.id, reason)

    async def _backfill_sent_at(self, session: AsyncSession, order: Order) -> None:
        if order.broker_sent_at is None and order.broker_order_id is None:
            order.broker_sent_at = order.broker_last_sync_at or order.updated_at

    async def _cancel_unsent(self, session: AsyncSession, order: Order) -> None:
        if order.broker_order_id is None and order.broker_sent_at is None:
            await self._set_final(session, order, self._canceled(order))
            await cancel_held_children(session, order, by="system")
            await self._closed(session, order)

    # -- send ------------------------------------------------------------------------

    async def _send(self, order_id: UUID, *, retry: bool = True,
                    refreshed: bool = False) -> None:
        """Send a never-attempted order. A liquidation waits until the symbol's other
        orders at the broker are final (their cancels are sent and read back first, in
        this pass, so their fills are applied) and is then clamped to the position.
        A real-money buy whose broker snapshot is too old waits for a fresh one (read
        here, with no lock held, then the order is checked again: `refreshed`). The wait
        clock (`_waiting`) runs only over consecutive waiting or failing passes: any other
        outcome (sent, rejected, market closed, a liquidation waiting...) resets it. A
        pass that raises is not an outcome: the clock is kept (or started, on the first
        failing pass), and a real order never sent that has been waiting longer than
        LIVE_WAIT_MAX is expired there too (one failing every pass from the start still
        ends)."""
        waiting = [False]
        try:
            await self._send_once(order_id, retry=retry, refreshed=refreshed, waiting=waiting)
        except Exception:
            await self._expire_after_error(order_id)
            raise
        if not waiting[0]:
            self._waiting.pop(order_id, None)

    async def _expire_after_error(self, order_id: UUID) -> None:
        now = self._clock()
        since = self._waiting.setdefault(order_id, now)  # a first failing pass starts it
        if now - since <= LIVE_WAIT_MAX:
            return
        try:
            await self._update(order_id, self._wait_expired)
        except Exception:
            log.exception("order %s: expiring the wait failed", order_id)
            return
        self._waiting.pop(order_id, None)

    async def _send_once(self, order_id: UUID, *, retry: bool, refreshed: bool,
                         waiting: list[bool]) -> None:
        now = self._clock()
        memo = await self._live_memo(order_id)
        quote = await self._limit_quote(order_id)
        balance = await self._sell_balance(order_id)
        if balance is _NO_BALANCE:
            return
        async with self._sm() as session:
            order = await _lock(session, order_id)
            if (
                order is None or order.status not in _SYNCED
                or order.broker_status != BrokerState.TO_SEND
                or order.broker_sent_at is not None or order.broker_order_id is not None
            ):
                return
            portfolio = await session.get(Portfolio, order.portfolio_id)
            instrument = await session.get(Instrument, order.symbol)
            link = await session.get(BrokerLink, order.broker_link_id)
            if order.expires_at is not None and order.expires_at <= now:
                await self._set_final(session, order, OrderStatus.expired)
                await cancel_held_children(session, order, by="system")
                await self._closed(session, order)
                await self._commit(session)
                return
            live = await self._is_live(session, order.broker_link_id)
            if portfolio.broker_link_id is None:  # the admin unlinked it: simulator
                if live:  # a real order never goes to the simulator
                    await self._reject(session, order, ErrorCode.LIVE_NOT_ARMED,
                                       LIVE_UNLINKED_MESSAGE)
                else:
                    self._release(order)
                await self._commit(session)
                return
            if link is None or not link.enabled or portfolio.broker_link_id != link.id:
                await self._reject(
                    session, order, ErrorCode.PROVIDER_UNAVAILABLE,
                    "Collegamento al broker disattivato o cambiato",
                )
                await self._commit(session)
                return
            if not market_status(instrument.market, now).is_open:
                return
            if order.role == LIQUIDATION_ROLE:
                still_open, to_cancel = await self._cancel_others(session, order)
                if still_open:
                    await self._liquidation_waiting(session, order, still_open, now)
                    await self._commit(session)
                    if retry:
                        await self._cancel_now(to_cancel)
                        waiting[0] = True  # the retry manages the wait clock
                        await self._send(order_id, retry=False)
                    return
            if (order.synthetic and order.type in _LIMIT_TYPES
                    and not await self._marketable(session, order, instrument, quote, now)):
                self._watch_again(order)  # plan 29 (C): never sent at market blindly
                await self._commit(session)
                return
            adapter = await self._adapter(session, link.id)
            verdict = (await market_only_sell(session, order)
                       if adapter.market_only or order.synthetic else None)
            if verdict == WAIT:  # plan 29 (B): its position is being sold by another
                await self._commit(session)
                return
            refusal = (SMTError(ErrorCode.INVALID_ORDER, NOTHING_TO_REDUCE)
                       if verdict == NOTHING_TO_REDUCE
                       else await self._refusal(session, portfolio, order, clamp=True))
            if refusal is not None and refusal.message == NOTHING_TO_REDUCE:
                await self._set_final(session, order, OrderStatus.canceled)
                order.reject_message = NOTHING_TO_REDUCE
                await cancel_held_children(session, order, by="system")
                await self._closed(session, order)
                await self._commit(session)
                return
            if refusal is None and balance is not None and await self._size_to_balance(
                    session, order, adapter, balance):
                await self._commit(session)  # the residue was closed here: nothing to send
                return
            if refusal is None and live:
                refusal = await self._live_refusal(session, memo, portfolio, order, link,
                                                   instrument, now)
                if refusal is _STALE or refusal is _WAIT:
                    await self._commit(session)  # nothing changed: the lock is released
                    waiting[0] = True  # the clock is kept (or handled by the retry below)
                    if (refusal is _STALE and not refreshed
                            and await self._refresh_account(link.id)):
                        await self._send(order_id, retry=retry, refreshed=True)
                        return
                    since = self._waiting.setdefault(order_id, now)
                    if now - since > LIVE_WAIT_MAX:  # after the refresh attempt
                        await self._update(order_id, self._wait_expired)
                        self._waiting.pop(order_id, None)
                    return
            if refusal is not None:
                await self._reject(session, order, refusal.code, refusal.message)
                await self._commit(session)
                return
            order.broker_status = BrokerState.SENDING
            order.broker_sent_at = now
            order.broker_last_sync_at = now
            await self._commit(session)  # claimed before any network I/O

        # plan 29: a triggered synthetic order goes out at market, its own type kept here
        sent = market_view(order) if order.synthetic else order
        try:
            await adapter.prepare([instrument])
            supported = adapter.supports(sent, instrument)
        except RuntimeError as e:  # nothing was sent: try again next pass
            log.warning("order %s: broker not ready: %s", order_id, e)
            await self._update(order_id, self._unclaim_unsent)
            return
        if not supported:
            await self._update(order_id, self._unsupported)
            return
        live = adapter.live
        if live:
            self._live_client(link.id)[1].reached.pop(str(order_id), None)
        try:
            bo = await adapter.place_order(sent, instrument)
        except LiveDryRun as dry:
            payload = dry.payload
            await self._update(order_id, lambda s, o: self._not_sent(
                s, o, ErrorCode.LIVE_DRY_RUN, DRY_RUN_MESSAGE, phase="dry_run",
                payload=payload))
            return
        except LiveGateClosed as closed:  # the gate read a new state: definitive, not sent
            code, message = closed.code, closed.message
            log.warning("order %s: live gate closed: %s %s", order_id, code, message)
            await self._update(order_id, lambda s, o: self._not_sent(s, o, code, message))
            return
        except Exception as e:
            if not (live or isinstance(e, RuntimeError)):
                raise
            reason = str(e) or type(e).__name__
            reached = live and self._live_client(link.id)[1].reached.pop(
                str(order_id), None) is not None
            if live and (not reached or isinstance(e, RateLimited)):
                # nothing reached the broker (or it refused to process it: HTTP 429)
                log.warning("order %s: not sent: %s", order_id, reason)
                if isinstance(e, RuntimeError):  # retried next pass, checks first
                    await self._update(order_id, self._unclaim_unsent)
                else:  # the gate itself failed: never sent, never retried blindly
                    await self._update(order_id, lambda s, o: self._not_sent(
                        s, o, ErrorCode.PROVIDER_UNAVAILABLE, NOT_SENT_MESSAGE))
                return
            log.warning("order %s: send failed: %s", order_id, reason)
            if adapter.idempotent_place:  # looked up by client reference next pass
                await self._update(order_id, self._attempted)
            else:
                await self._update(order_id, lambda s, o: self._mark_unknown(s, o, reason))
            return
        payload = _sent_payload(
            self._live_client(link.id)[1].reached.pop(str(order_id), None)) if live else None
        await self._update(order_id, lambda s, o: self._placed(s, o, bo, payload), bo)
        if live and order.side == OrderSide.buy and bo.status != "rejected":
            await self._mark_stale(link.id)

    # -- fee in the asset (Alpaca crypto) ----------------------------------------------

    async def _sell_balance(self, order_id: UUID):
        """(held, free) of the asset in the broker account for a sell about to be sent on
        a broker whose fee is taken in the asset (`asset_balance`), read with no
        transaction open; None for any other order; _NO_BALANCE when the read failed."""
        async with self._sm() as session:
            order = await session.get(Order, order_id)
            if (order is None or order.side != OrderSide.sell or order.broker_link_id is None
                    or order.broker_status != BrokerState.TO_SEND):
                await session.commit()
                return None
            instrument = await session.get(Instrument, order.symbol)
            adapter = await self._adapter(session, order.broker_link_id)
            await session.commit()
        if instrument is None:
            return None
        try:
            got = await adapter.asset_balance(order.symbol, instrument)
            if got is not None:
                await adapter.load_fee_dust([order.symbol])
        except RuntimeError as e:
            log.warning("order %s: broker balance not read (%s); retried next pass",
                        order_id, e)
            return _NO_BALANCE
        return got

    async def _size_to_balance(self, session: AsyncSession, order: Order,
                               adapter: BrokerAdapter,
                               balance: tuple[Decimal, Decimal]) -> bool:
        """Size a sell to the broker account's balance of the asset (module docstring).
        True when the residue was closed here instead (the order is filled, not sent)."""
        if order.qty is None:
            return False
        total, free = balance
        filled = order.filled_qty or Decimal(0)
        remaining = order.qty - filled
        held = await _held(session, order.portfolio_id, order.symbol)
        tolerance = adapter.fee_dust(order.symbol, max(remaining, held))
        min_qty = adapter.fee_dust(order.symbol, Decimal(0))
        if remaining > free:
            if remaining - free > tolerance:
                return False  # not a fee residue: sent as is, the broker answers
            if free <= 0 or free < min_qty:
                if not await write_off_dust(session, order):
                    return False
                order.broker_status = "filled"
                log.info("order %s: %s %s left here only (fee residue): closed here",
                         order.id, remaining, order.symbol)
                await self._closed(session, order)
                return True
            log.info("order %s: sized %s -> %s (what the account holds)",
                     order.id, remaining, free)
            order.qty = filled + free
            return False
        if held > 0 and remaining == held:  # a full exit: the account's residue goes too
            ours = await session.scalar(
                select(func.coalesce(func.sum(AccountBalance.balance), 0))
                .join(Account, AccountBalance.account_id == Account.id)
                .join(Portfolio, Portfolio.id == Account.portfolio_id)
                .where(Portfolio.broker_link_id == order.broker_link_id,
                       Account.asset == order.symbol, Account.kind == AccountKind.position))
            extra = total - Decimal(ours)
            if 0 < extra <= tolerance and remaining + extra <= free:
                log.info("order %s: sized %s -> %s (the account's fee residue too)",
                         order.id, remaining, remaining + extra)
                order.qty = order.qty + extra
        return False

    async def _rate_fee(self, session: AsyncSession, order: Order, bo: BrokerOrder,
                        delta: Decimal, price: Decimal
                        ) -> tuple[Decimal, Decimal | None, Decimal, str]:
        """(quantity that moves the position, the order's progress when it differs, fee,
        fee currency) of a fill of `delta` charged `bo.fee_rate` (module docstring)."""
        ccy = await session.scalar(
            select(Instrument.currency).where(Instrument.symbol == order.symbol))
        ccy = str(ccy or "USD").upper()
        rate = bo.fee_rate
        with localcontext() as ctx:
            ctx.prec = _PREC
            if order.side == OrderSide.buy:
                coin = min((delta * rate).quantize(_COIN_STEP, rounding=ROUND_UP), delta)
                return delta - coin, delta, coin * price, ccy
            fee = delta * price * rate
            held = await _held(session, order.portfolio_id, order.symbol)
            adapter = await self._adapter(session, order.broker_link_id)
            tolerance = adapter.fee_dust(order.symbol, held)
            left = held - delta
            if (bo.status == "filled" and 0 < left <= tolerance and (
                    order.reduce_only or left < adapter.fee_dust(order.symbol, Decimal(0)))):
                return held, delta, fee + left * price, ccy  # the residue goes with it
            if held > 0 and 0 < -left <= tolerance:
                return held, delta, fee, ccy  # the account's residue, never booked here
            return delta, None, fee, ccy

    # -- synthetic limits (plan 29, C) ------------------------------------------------

    async def _limit_quote(self, order_id: UUID) -> Quote | None:
        """The current quote of a triggered synthetic limit / stop-limit about to be sent
        (read with no transaction open); None for any other order, or when unavailable."""
        async with self._sm() as session:
            row = (await session.execute(
                select(Order.symbol, Order.type, Order.synthetic)
                .where(Order.id == order_id)
            )).first()
            await session.commit()
        if row is None or not row.synthetic or row.type not in _LIMIT_TYPES:
            return None
        try:
            return (await self._qs.get_quotes([row.symbol])).get(row.symbol)
        except Exception as e:  # noqa: BLE001  (no usable quote: watched again)
            log.warning("order %s: no quote before sending: %s", order_id, e)
            return None

    async def _marketable(self, session: AsyncSession, order: Order,
                          instrument: Instrument, quote: Quote | None,
                          now: datetime) -> bool:
        """The limit is still reachable at `quote` (buy: ask <= limit; sell: bid >= limit),
        with the processor's guards: a missing, stale or suspect quote is not."""
        if quote is None or quote.suspect is not None or order.limit_price is None:
            return False
        if (now - quote.ts).total_seconds() > await get_stale_threshold(
                session, instrument.asset_class):
            return False
        try:
            bid, ask = effective_bid_ask(
                QuoteView(quote.bid, quote.ask, quote.last, quote.ts),
                await get_default_spread_bps(session, instrument.asset_class))
        except SMTError:
            return False
        if order.side == OrderSide.buy:
            return ask <= order.limit_price
        return bid >= order.limit_price

    def _watch_again(self, order: Order) -> None:
        """A triggered synthetic limit no longer marketable: back to the processor, which
        hands it over again when its limit is reached (a stop-limit stays triggered)."""
        order.broker_link_id = None
        order.broker_status = None
        if order.type == OrderType.limit:
            order.status = OrderStatus.pending
        order.updated_at = datetime.now(UTC)
        log.info("order %s: limit no longer reachable, watched again", order.id)

    # -- real money ------------------------------------------------------------------

    async def _live_memo(self, order_id: UUID) -> QuoteSource:
        """For a real-money order, a frozen memo with every quote/FX rate its checks need
        (its portfolio, its symbol and those of the portfolio's other open buys, the fee
        rule and broker snapshot currencies to EUR, and through prefetch_portfolio the cash
        currencies of the link's other portfolios to the account currency for their
        quotas), fetched before any lock (I1, I2):
        a miss under the lock makes the order wait. `self._qs` otherwise. A real sell needs
        no quote or FX rate (its checks are the position and the fuse): an empty frozen
        memo, so an FX outage never makes it wait (Review Focus 3, plan 9)."""
        async with self._sm() as session:
            row = (await session.execute(
                select(Order.portfolio_id, Order.symbol, Order.broker_link_id, Order.side)
                .where(Order.id == order_id)
            )).first()
            if row is None or not await self._is_live(session, row.broker_link_id):
                await session.commit()
                return self._qs
            if row.side == OrderSide.sell:
                await session.commit()
                memo = MemoQuoteSource(self._qs)
                memo.freeze()
                return memo
            buys = set(await session.scalars(
                select(Order.symbol).where(
                    Order.portfolio_id == row.portfolio_id, Order.side == OrderSide.buy,
                    Order.status.in_(_SYNCED), Order.broker_link_id.is_not(None))
            ))
            link = await session.get(BrokerLink, row.broker_link_id)
            snap = link.last_account_snapshot if link is not None else None
            ccys = {r.currency for r in await load_rules(session)}
            if isinstance(snap, dict) and snap.get("currency"):
                ccys.add(str(snap["currency"]).upper())
            memo = MemoQuoteSource.wrap(self._qs)
            await prefetch_portfolio(session, memo, row.portfolio_id,
                                     symbols=sorted({row.symbol, *buys}))
            await memo.prefetch([], {(c, EUR) for c in ccys if c != EUR})
            await session.commit()
        memo.freeze()
        return memo

    async def _live_refusal(
        self, session: AsyncSession, qs: QuoteSource, portfolio: Portfolio, order: Order,
        link: BrokerLink, instrument: Instrument, now: datetime,
    ):
        """The real-money checks right before sending (risk.live_checks): an SMTError,
        _STALE (a buy needs a fresh broker snapshot first), _WAIT (a quote or FX rate is
        missing: not prefetched, or a quote outage) or None."""
        try:
            await check_live_state(session, link, live_flatten=order.live_flatten)
            if order.side == OrderSide.sell and not order.reduce_only:
                await check_no_short(
                    session, portfolio=portfolio, symbol=order.symbol, order=order,
                    qty=None if order.qty is None
                    else order.qty - (order.filled_qty or Decimal(0)))
            if order.side == OrderSide.buy:
                if not snapshot_fresh(link, now):
                    return _STALE
                try:
                    # plan 29: a triggered synthetic order goes at market (the ask)
                    value, fees = await estimate_buy(
                        session, qs, portfolio=portfolio, instrument=instrument,
                        order=market_view(order) if order.synthetic else order)
                    await check_live_cash(session, qs, portfolio=portfolio, link=link,
                                          est_value=value, est_fees=fees, now=now,
                                          order=order)
                except SMTError as e:
                    if e.code == ErrorCode.PROVIDER_UNAVAILABLE:
                        log.warning("order %s waits: %s", order.id, e.message)
                        return _WAIT
                    raise
            await check_live_fuse(session, portfolio=portfolio, link=link, now=now,
                                  reduce_only=order.reduce_only,
                                  live_flatten=order.live_flatten, exclude=order.id)
        except SMTError as e:
            return e
        return None

    async def _refresh_account(self, link_id: UUID) -> bool:
        """Read the broker account (GET only, no transaction open) into the link's
        snapshot; at most once per ACCOUNT_READ_MIN_INTERVAL per link. False when it was
        not read (too soon, a failure, or another account behind the keys: the snapshot
        is then left alone for the live monitor to handle)."""
        now = self._clock()
        last = self._account_read_at.get(link_id)
        if last is not None and now - last < ACCOUNT_READ_MIN_INTERVAL:
            return False
        self._account_read_at[link_id] = now
        async with self._sm() as session:
            adapter = await self._adapter(session, link_id)
            await session.commit()
        try:
            snap = await adapter.get_account()
        except Exception as e:  # noqa: BLE001  (the order waits; the monitor sees health)
            log.warning("broker link %s: account read failed: %s", link_id, e)
            return False
        async with self._sm() as session:
            link = await session.get(BrokerLink, link_id)
            if link is None:
                return False
            if (link.account_fingerprint is not None and link.account_fingerprint
                    != account_fingerprint(link.kind, snap.account_id)):
                log.error("broker link %s: another account behind the keys; not sending",
                          link_id)
                await session.commit()
                return False
            link.last_account_snapshot = snapshot_record(snap)
            link.last_snapshot_at = now
            await session.commit()
        return True

    async def _wait_expired(self, session: AsyncSession, order: Order) -> None:
        """A real order waited LIVE_WAIT_MAX for account data / quotes: rejected (only if
        still never sent; paper/demo orders are never expired by the clock)."""
        if (order.broker_status == BrokerState.TO_SEND and order.broker_sent_at is None
                and order.broker_order_id is None
                and await self._is_live(session, order.broker_link_id)):
            await self._reject(session, order, ErrorCode.PROVIDER_UNAVAILABLE,
                               WAIT_TOO_LONG_MESSAGE)

    async def _mark_stale(self, link_id: UUID) -> None:
        """A real buy was sent: the snapshot's cash no longer counts it (its own short
        transaction, after the order's: the link row is never locked after a portfolio)."""
        async with self._sm() as session:
            link = await session.get(BrokerLink, link_id)
            if link is not None:
                mark_snapshot_stale(link, await session.scalar(select(func.now())))
            await session.commit()

    async def _not_sent(self, session: AsyncSession, order: Order, code: ErrorCode,
                        message: str, *, phase: str = "rejected",
                        payload: dict | None = None) -> None:
        """A claimed real order whose request never reached the broker: rejected (a
        cancel or a modify requested meanwhile cancels it: nothing is at the broker);
        `broker_sent_at` is cleared."""
        stop = (BrokerState.CANCEL_REQUESTED, BrokerState.REPLACE_REQUESTED)
        if order.broker_status != BrokerState.SENDING and order.broker_status not in stop:
            return
        order.broker_sent_at = None
        if order.broker_status in stop:
            await self._cancel_unsent(session, order)
            return
        order.broker_status = "rejected"  # reached BrokerSync: told by its live.order.* row
        await self._reject(session, order, code, message, phase=phase, payload=payload)

    async def _account_currency(self, session: AsyncSession, link_id: UUID | None
                                ) -> str | None:
        """The currency of the link's broker account (that of its fees): the live
        snapshot's, else the adapter's ACCOUNT_CURRENCY (eToro: USD), else None."""
        link = await session.get(BrokerLink, link_id) if link_id is not None else None
        if link is None:
            return None
        snap = link.last_account_snapshot
        if isinstance(snap, dict) and snap.get("currency"):
            return str(snap["currency"]).upper()
        try:
            return adapter_class(link.kind).ACCOUNT_CURRENCY
        except SMTError:  # a kind with no adapter: no declared currency
            return None

    async def _fee_override(self, session: AsyncSession, order: Order,
                            bo: BrokerOrder) -> tuple[Decimal | None, str | None]:
        """The broker's fee for this fill (`_broker_fees` links): its cumulative fee for the
        order (in the account currency) less what earlier fills took, every earlier fee
        converted to that currency first (through EUR, the prefetched memo), or (None,
        None)."""
        if bo.fees is None or not await self._broker_fees(session, order.broker_link_id):
            return None, None
        ccy = await self._account_currency(session, order.broker_link_id)
        if not ccy:
            ccy = await session.scalar(
                select(Instrument.currency).where(Instrument.symbol == order.symbol))
        ccy = str(ccy).upper()
        taken = Decimal(0)
        for fee_ccy, amount in (await session.execute(
            select(Fill.fee_currency, func.coalesce(func.sum(Fill.fee_amount), 0))
            .where(Fill.order_id == order.id).group_by(Fill.fee_currency)
        )).all():
            if amount:
                taken += await to_currency(self._fill_qs, Decimal(amount),
                                           str(fee_ccy or ccy), ccy)
        return max(bo.fees - taken, Decimal(0)), ccy

    async def _cancel_others(
        self, session: AsyncSession, order: Order
    ) -> tuple[list[UUID], list[UUID]]:
        """A broker liquidation: request the cancel of the portfolio's other open orders
        on the symbol at the broker (never-attempted ones are canceled locally).
        Returns (still open, those with a broker order to cancel right away)."""
        still_open, to_cancel = [], []
        for other in await session.scalars(
            select(Order).where(
                Order.portfolio_id == order.portfolio_id, Order.symbol == order.symbol,
                Order.id != order.id, Order.broker_link_id.is_not(None),
                Order.status.in_(_SYNCED),
            ).order_by(Order.created_at, Order.id).with_for_update()
        ):
            if (
                other.broker_order_id is None and other.broker_sent_at is None
                and other.broker_status != BrokerState.SENDING
            ):
                await self._set_final(session, other, OrderStatus.canceled)
                await cancel_held_children(session, other, by="system")
                continue
            still_open.append(other.id)
            if other.broker_status != BrokerState.SENDING:
                other.broker_status = BrokerState.CANCEL_REQUESTED
                other.updated_at = datetime.now(UTC)
            if other.broker_order_id is not None:
                to_cancel.append(other.id)
        return still_open, to_cancel

    async def _liquidation_waiting(
        self, session: AsyncSession, order: Order, still_open: list[UUID], now: datetime
    ) -> None:
        """A liquidation blocked by cancels the broker keeps refusing: tell the admin once
        (after LIQUIDATION_WAIT_NOTIFY) instead of only logging every pass."""
        if now - order.created_at <= LIQUIDATION_WAIT_NOTIFY:
            return
        sent = await session.scalar(
            select(Notification.id).where(
                Notification.portfolio_id == order.portfolio_id,
                Notification.kind == "broker.liquidation_waiting",
                Notification.payload["order_id"].astext == str(order.id),
            ).limit(1)
        )
        if sent is None:
            await notify(
                session, portfolio_id=order.portfolio_id, agent_id=order.agent_id,
                kind="broker.liquidation_waiting",
                payload={"order_id": order.id, "symbol": order.symbol,
                         "waiting_on": [str(i) for i in still_open]},
            )
            log.error("liquidation %s waits on broker cancels of %s", order.id, still_open)

    async def _cancel_now(self, order_ids: list[UUID]) -> None:
        for other in order_ids:
            try:
                await self._cancel(other)
            except Exception:
                log.exception("broker cancel of order %s before a liquidation failed", other)

    def _release(self, order: Order) -> None:
        """Back to the simulator: only for a portfolio the admin unlinked, never sent."""
        order.broker_link_id = None
        order.broker_status = None
        order.broker_replicated = False
        if order.type == OrderType.market and order.status == OrderStatus.pending:
            order.status = OrderStatus.queued  # executed by the processor's sweep
        order.updated_at = datetime.now(UTC)
        log.info("order %s released to the simulator", order.id)

    async def _unclaim_unsent(self, session: AsyncSession, order: Order) -> None:
        if order.broker_status == BrokerState.SENDING:
            order.broker_status = BrokerState.TO_SEND
            order.broker_sent_at = None
        elif order.broker_status == BrokerState.CANCEL_REQUESTED:
            order.broker_sent_at = None  # never reached the broker

    async def _attempted(self, session: AsyncSession, order: Order) -> None:
        if order.broker_status == BrokerState.SENDING:
            order.broker_status = BrokerState.TO_SEND  # broker_sent_at stays: find first

    async def _unsupported(self, session: AsyncSession, order: Order) -> None:
        if order.broker_status not in (BrokerState.SENDING, BrokerState.CANCEL_REQUESTED):
            return
        order.broker_sent_at = None  # nothing was sent
        if order.broker_status == BrokerState.CANCEL_REQUESTED:
            await self._cancel_unsent(session, order)
            return
        order.broker_status = None
        await self._reject(session, order, ErrorCode.INVALID_ORDER, UNSUPPORTED)

    async def _placed(self, session: AsyncSession, order: Order, bo: BrokerOrder,
                      payload: dict | None = None) -> None:
        if bo.status == "rejected":
            await self._reject(
                session, order, ErrorCode.PROVIDER_UNAVAILABLE,
                bo.message or BROKER_REJECTED_MESSAGE,
            )
            return
        await self._adopt(session, order, bo, live_payload=payload)

    async def _live_sent(self, session: AsyncSession, order: Order, bo: BrokerOrder,
                         payload: dict | None, *, matched: bool = False) -> None:
        """A real order is at the broker: its live.order.sent activity and the
        `order_sent` live audit (request payload and broker response, never headers)."""
        await record_live_order_activity(
            session, order, "sent", bo=bo,
            message="trovato al broker dopo un esito incerto" if matched else None)
        await record_live_audit(
            session, action="order_sent", link_id=order.broker_link_id, actor="system",
            portfolio_id=order.portfolio_id, order_id=order.id,
            details={"payload": payload, "response": bo.raw,
                     "broker_order_id": bo.broker_order_id, "matched": matched},
        )

    async def _adopt(self, session: AsyncSession, order: Order, bo: BrokerOrder, *,
                     live_payload: dict | None = None, matched: bool = False) -> bool:
        """Link `bo` to the order (refused if another order already has it, C1); a real
        order's `live.order.sent` is recorded once linked, before its fills."""
        taken = await session.scalar(
            select(Order.id).where(
                Order.broker_link_id == order.broker_link_id,
                Order.broker_order_id == bo.broker_order_id, Order.id != order.id,
            ).limit(1)
        )
        if taken is not None:
            log.error("order %s: broker order %s already belongs to order %s; not linked",
                      order.id, bo.broker_order_id, taken)
            return False
        order.broker_order_id = bo.broker_order_id
        order.broker_replicated = True
        if await self._is_live(session, order.broker_link_id):
            await self._live_sent(session, order, bo, live_payload, matched=matched)
        if order.broker_status in (BrokerState.SENDING, BrokerState.TO_SEND,
                                   BrokerState.UNKNOWN):
            order.broker_status = bo.status
        queue_event(session, "order.broker_accepted", {
            "order_id": order.id, "portfolio_id": order.portfolio_id,
            "agent_id": order.agent_id, "symbol": order.symbol,
            "broker_order_id": bo.broker_order_id,
        })
        await self._apply(session, order, bo)
        return True

    # -- ambiguous sends (C2, Ruling R7) ---------------------------------------------

    async def _recover_sending(self, order_id: UUID) -> None:
        async with self._sm() as session:
            order = await _lock(session, order_id)
            if order is None or order.broker_status != BrokerState.SENDING:
                return
            adapter = await self._adapter(session, order.broker_link_id)
            if order.broker_sent_at is None:
                order.broker_sent_at = order.broker_last_sync_at or order.updated_at
            if adapter.idempotent_place:
                order.broker_status = BrokerState.TO_SEND
            else:
                await self._mark_unknown(session, order, "interrupted while sending")
            await self._commit(session)
        if adapter.idempotent_place:
            await self._match(order_id)

    async def _match(self, order_id: UUID) -> None:
        async with self._sm() as session:
            order = await session.get(Order, order_id)
            if (
                order is None or order.broker_link_id is None or order.status not in _SYNCED
                or order.broker_order_id is not None or order.broker_sent_at is None
            ):
                return
            instrument = await session.get(Instrument, order.symbol)
            adapter = await self._adapter(session, order.broker_link_id)
            exclude = await _linked_ids(session, order.broker_link_id, order.id)
            await session.commit()
        since = order.broker_sent_at
        sent = market_view(order) if order.synthetic else order
        bo = await adapter.find_order(sent, instrument, since, exclude)
        if bo is not None:  # _adopt checks again, under the lock, that it is unused
            await self._update(order_id, lambda s, o: self._matched(s, o, bo), bo)
            return
        if adapter.idempotent_place:  # definitive: not at the broker
            await self._update(order_id, self._not_found)
        elif self._clock() - since > MATCH_WINDOW:
            await self._update(order_id, self._give_up)

    async def _matched(self, session: AsyncSession, order: Order, bo: BrokerOrder) -> None:
        if order.broker_order_id is not None or order.broker_sent_at is None:
            return
        if not await self._adopt(session, order, bo, matched=True):
            return
        log.warning("order %s matched broker order %s", order.id, bo.broker_order_id)
        if order.status in _SYNCED:
            reason = (
                "cancel requested"
                if order.broker_status == BrokerState.CANCEL_REQUESTED
                else await self._must_stop(session, order)
            )
            if reason is not None:
                log.warning("order %s adopted but must not proceed (%s): canceling",
                            order.id, reason)
                order.broker_status = BrokerState.CANCEL_REQUESTED
                self._followups.append(order.id)

    async def _not_found(self, session: AsyncSession, order: Order) -> None:
        if order.broker_order_id is not None:
            return
        order.broker_sent_at = None
        if order.broker_status == BrokerState.CANCEL_REQUESTED:
            await self._cancel_unsent(session, order)
        else:
            order.broker_status = BrokerState.TO_SEND  # the next pass decides afresh

    async def _give_up(self, session: AsyncSession, order: Order) -> None:
        if order.broker_order_id is not None:
            return
        if await self._is_live(session, order.broker_link_id):  # Review Focus 1 (plan 8)
            # a real order may still exist at the broker: reduce-only until verified
            await trip_reduce_only(session, order.portfolio_id, cause="order_unknown",
                                   details={"order_id": order.id, "symbol": order.symbol})
        if order.broker_status == BrokerState.CANCEL_REQUESTED:
            await self._set_final(session, order, self._canceled(order))
            await cancel_held_children(session, order, by="system")
            await self._closed(session, order)
            return
        await self._reject(
            session, order, ErrorCode.PROVIDER_UNAVAILABLE,
            "broker outcome unknown: no matching broker order found; check the reconciliation",
        )

    # -- live orders -----------------------------------------------------------------

    async def _read(self, order_id: UUID) -> tuple[Order, BrokerAdapter] | None:
        """The order and its adapter, read without locks (no transaction is kept open
        across the network call that follows)."""
        async with self._sm() as session:
            order = await session.get(Order, order_id)
            if (
                order is None or order.broker_link_id is None or order.status not in _SYNCED
                or order.broker_order_id is None
            ):
                return None
            adapter = await self._adapter(session, order.broker_link_id)
            await session.commit()
        return order, adapter

    async def _poll(self, order_id: UUID) -> None:
        read = await self._read(order_id)
        if read is None:
            return
        order, adapter = read
        bo = await self._get_order(order_id, order, adapter)
        if bo is not None:
            await self._update(order_id, lambda s, o: self._apply_same(s, o, bo), bo)

    async def _get_order(self, order_id: UUID, order: Order, adapter: BrokerAdapter
                         ) -> BrokerOrder | None:
        """The broker's view of the order; None when it cannot be read safely
        (OutcomeUnknown: the outcome is marked unknown, reduce-only; read again next pass)."""
        try:
            return await adapter.get_order(order.broker_order_id)
        except OutcomeUnknown as e:
            log.error("order %s: %s", order_id, e)
            if order_id not in self._unknown_reads:  # once per process, not every pass
                reason = str(e)
                await self._update(order_id, lambda s, o: self._mark_unknown(s, o, reason))
                self._unknown_reads.add(order_id)
            return None

    async def _cancel(self, order_id: UUID) -> None:
        read = await self._read(order_id)
        if read is None:
            return
        order, adapter = read
        try:
            await adapter.cancel_order(order.broker_order_id)
        except (RuntimeError, LiveGateClosed) as e:  # read it back anyway: fills (I2)
            log.warning("order %s: broker cancel failed (%s); retried next pass", order_id, e)
        bo = await self._get_order(order_id, order, adapter)
        if bo is not None:
            await self._update(order_id, lambda s, o: self._apply_same(s, o, bo), bo)

    async def _apply_same(self, session: AsyncSession, order: Order, bo: BrokerOrder) -> None:
        if order.broker_order_id == bo.broker_order_id:  # not resubmitted meanwhile
            await self._apply(session, order, bo)

    async def _price_missing(self, session: AsyncSession, order: Order, bo: BrokerOrder) -> None:
        """A final order with fills but no broker price: notify once after
        FILL_PRICE_WAIT; keep waiting (manual reconciliation, no invented price)."""
        if bo.status not in _FINAL and bo.status != "filled":
            return
        since = order.broker_last_sync_at or order.updated_at
        if self._clock() - since <= FILL_PRICE_WAIT:
            return
        sent = await session.scalar(
            select(Notification.id).where(
                Notification.portfolio_id == order.portfolio_id,
                Notification.kind == "broker.fill_price_missing",
                Notification.payload["order_id"].astext == str(order.id),
            ).limit(1)
        )
        if sent is None:
            await notify(
                session, portfolio_id=order.portfolio_id, agent_id=order.agent_id,
                kind="broker.fill_price_missing",
                payload={"order_id": order.id, "symbol": order.symbol,
                         "broker_filled_qty": bo.filled_qty, "filled_qty": order.filled_qty},
            )
            log.error("order %s: broker fill price missing for %s; waiting",
                      order.id, bo.filled_qty - order.filled_qty)

    @staticmethod
    async def _fill_time(session: AsyncSession, order: Order, bo: BrokerOrder) -> None:
        """The broker's fill time on the automatic trade, in a savepoint: a measure only,
        its failure never stops the fill from being applied (the Tracker falls back to
        the local time)."""
        try:
            async with session.begin_nested():
                await record_broker_fill_time(session, order, bo.raw)
        except Exception:
            log.warning("order %s: broker fill time not recorded", order.id, exc_info=True)

    async def _apply(self, session: AsyncSession, order: Order, bo: BrokerOrder) -> None:
        """Apply the broker's view of the order (caller holds the locks)."""
        delta = bo.filled_qty - (order.filled_qty or Decimal(0))
        if delta > 0:
            if bo.avg_fill_price is None:
                await self._price_missing(session, order, bo)
                return  # no broker price yet: wait for it
            price = delta_price(order, bo, delta)
            fee, fee_ccy = await self._fee_override(session, order, bo)
            qty, progress = delta, None
            if bo.fee_rate is not None:
                qty, progress, fee, fee_ccy = await self._rate_fee(
                    session, order, bo, delta, price)
            await execute_fill(
                session, self._fill_qs, order.id, fill_qty=qty, fill_price=price,
                progress_qty=progress,
                source="broker", final=bo.status == "filled", redis=self._redis,
                journal_type=(
                    JournalType.LIQUIDATION if order.role == LIQUIDATION_ROLE
                    else JournalType.TRADE
                ),
                fee_override=fee, fee_override_ccy=fee_ccy,
            )
            if is_automatic(order):  # mode B: the broker's fill time, for the measures
                await self._fill_time(session, order, bo)
            self._loss_checks.add(order.portfolio_id)
        order.broker_last_sync_at = self._clock()
        if order.status not in _SYNCED:  # filled by execute_fill
            order.broker_status = bo.status
            if order.status == OrderStatus.filled:
                await self._live_note(session, order, "filled", bo=bo)
            await self._after_fill(session, order)
            return
        if bo.status == "filled":  # e.g. a notional order, or the broker filled less
            order.status = OrderStatus.filled
            order.broker_status = bo.status
            order.updated_at = datetime.now(UTC)
            await activate_linked(session, order)
            await self._live_note(session, order, "filled", bo=bo)
            await self._after_fill(session, order)
            return
        if delta > 0:
            await self._live_note(session, order, "partial", bo=bo)
        if bo.status in _FINAL:
            await self._finish(session, order, bo)
            return
        if order.broker_status not in (
            BrokerState.CANCEL_REQUESTED, BrokerState.REPLACE_REQUESTED
        ):
            order.broker_status = bo.status
            if await cancel_plan_additions(session, [order]):  # canceled in this pass
                self._followups.append(order.id)

    async def _after_fill(self, session: AsyncSession, order: Order) -> None:
        """A filled order: its OCO siblings at the broker are canceled in this pass."""
        if order.oco_group_id is not None:
            await session.flush()
            self._followups += list(await session.scalars(
                select(Order.id).where(
                    Order.oco_group_id == order.oco_group_id, Order.id != order.id,
                    Order.broker_status == BrokerState.CANCEL_REQUESTED,
                    Order.status.in_(_SYNCED),
                )
            ))
        await self._closed(session, order)

    def _resubmit(self, order: Order) -> None:
        """The broker order is final with nothing filled: send again (new client ref)."""
        order.broker_order_id = None
        order.broker_replicated = False
        order.broker_sent_at = None
        order.broker_attempt = (order.broker_attempt or 0) + 1
        order.broker_status = BrokerState.TO_SEND
        order.updated_at = datetime.now(UTC)

    async def _renewable(self, session: AsyncSession, order: Order) -> bool:
        """A `gtc` order the broker took only as `day` (`renewed_daily`), not being
        canceled, its valid_until (if any) not passed."""
        if order.broker_status == BrokerState.CANCEL_REQUESTED:
            return False
        if order.valid_until is not None and order.valid_until <= self._clock():
            return False
        instrument = await session.get(Instrument, order.symbol)
        adapter = await self._adapter(session, order.broker_link_id)
        return instrument is not None and adapter.renewed_daily(order, instrument)

    async def _renew(self, session: AsyncSession, order: Order, bo: BrokerOrder) -> None:
        expired_id = bo.broker_order_id
        self._resubmit(order)
        record_activity(session, ActivityDraft(
            "broker.order_renewed", ActivityLevel.info, False,
            f"Ordine {order.symbol} valido per la giornata al broker (quantità frazionaria):"
            " scaduto a fine seduta, sarà inviato di nuovo all'apertura",
            {"order_id": order.id, "symbol": order.symbol, "role": order.role,
             "expired_broker_order_id": expired_id, "attempt": order.broker_attempt},
        ), portfolio_id=order.portfolio_id, agent_id=order.agent_id)
        log.info("order %s: day order %s expired at the broker; renewed (attempt %s)",
                 order.id, expired_id, order.broker_attempt)

    async def _finish(self, session: AsyncSession, order: Order, bo: BrokerOrder) -> None:
        replacing = order.broker_status == BrokerState.REPLACE_REQUESTED
        if replacing and bo.status == "canceled" and order.filled_qty == 0:
            self._resubmit(order)
            return
        if bo.status == "expired" and await self._renewable(session, order):
            if order.filled_qty == 0:
                await self._renew(session, order, bo)
                return
            await notify(
                session, portfolio_id=order.portfolio_id, agent_id=order.agent_id,
                kind="broker.renewal_failed",
                payload={"order_id": order.id, "symbol": order.symbol,
                         "filled_qty": order.filled_qty, "qty": order.qty},
            )
        order.broker_status = bo.status
        if bo.status == "rejected":
            await self._reject(
                session, order, ErrorCode.PROVIDER_UNAVAILABLE,
                bo.message or BROKER_REJECTED_MESSAGE,
            )
            return
        if replacing:
            order.reject_message = "replace failed: the order filled before the cancel"
            await notify(
                session, portfolio_id=order.portfolio_id, agent_id=order.agent_id,
                kind="order.replace_failed",
                payload={"order_id": order.id, "symbol": order.symbol,
                         "filled_qty": order.filled_qty},
            )
        final = _FINAL[bo.status]
        await self._set_final(
            session, order, self._canceled(order) if final == OrderStatus.canceled else final)
        if order.filled_qty > 0:  # a partly filled entry: its exits cover what filled
            credited = await credited_qty(session, order)  # net of a fee in the asset
            for child in await session.scalars(
                select(Order).where(Order.parent_id == order.id,
                                    Order.status == OrderStatus.held)
            ):
                child.qty = min(order.filled_qty, credited) if credited > 0 else (
                    order.filled_qty)
            await activate_linked(session, order)
        else:
            await cancel_held_children(session, order, by="system")
        await self._after_fill(session, order)


# -- reconciliation --------------------------------------------------------------------


def _num(v: Decimal) -> str:
    return format(v.normalize(), "f") if v != 0 else "0"


def fee_dust_equal(adapter: BrokerAdapter, symbol: str, ours: Decimal, broker: Decimal
                   ) -> bool:
    """Equal quantities, or apart by no more than the broker's fee residue in the asset
    (`adapter.fee_dust`, Alpaca crypto: never a difference to report)."""
    return ours == broker or abs(ours - broker) <= adapter.fee_dust(
        symbol, max(abs(ours), abs(broker)))


async def _position_balances(session: AsyncSession, portfolio_id: UUID) -> dict[str, Decimal]:
    rows = await session.execute(
        select(Account.asset, AccountBalance.balance)
        .join(AccountBalance, AccountBalance.account_id == Account.id)
        .where(
            Account.portfolio_id == portfolio_id, Account.kind == AccountKind.position,
            AccountBalance.balance != 0,
        )
    )
    return {asset: balance for asset, balance in rows}


async def reconcile(
    sessionmaker: async_sessionmaker[AsyncSession], link_id: UUID, http, *,
    notify: bool = True,
) -> list[Reconciliation]:
    """Compare the broker's positions with ours and store (committed) one
    Reconciliation per linked portfolio (diffs: [{symbol, ours, broker}], quantities as
    strings). Portfolios sharing a link share the broker account, so `ours` is their
    sum. Nothing is corrected. A difference is always recorded in the activity feed; with
    `notify` (default) it is also sent to Telegram when it changed since the previous
    reconciliation, without it never. A portfolio no agent can access gets no activity
    row: a difference changed since its previous reconciliation is sent to Telegram
    (category system) whatever `notify`. Reads, broker calls and writes use separate sessions:
    no transaction is open during network I/O. A client the adapter needed of its own
    (a live link's LiveClient) is closed after the broker calls."""
    async with contextlib.AsyncExitStack() as stack:
        async with sessionmaker() as session:
            link = await session.get(BrokerLink, link_id)
            if link is None:
                raise SMTError(ErrorCode.NOT_FOUND, f"broker link {link_id} not found")
            portfolio_ids = list(await session.scalars(
                select(Portfolio.id).where(Portfolio.broker_link_id == link_id)
                .order_by(Portfolio.id)
            ))
            if not portfolio_ids:
                return []
            ours: dict[str, Decimal] = {}
            for pid in portfolio_ids:
                for symbol, qty in (await _position_balances(session, pid)).items():
                    ours[symbol] = ours.get(symbol, Decimal(0)) + qty
            instruments = list(await session.scalars(
                select(Instrument).where(Instrument.symbol.in_(sorted(ours)))
            )) if ours else []
            adapter = await stack.enter_async_context(
                open_adapter(session, link_id, http, sessionmaker=sessionmaker))
        await adapter.prepare(instruments)
        broker = await adapter.get_positions()
        await adapter.load_fee_dust(set(ours) | set(broker))
    diffs = [
        {"symbol": symbol, "ours": _num(ours.get(symbol, Decimal(0))),
         "broker": _num(broker.get(symbol, Decimal(0)))}
        for symbol in sorted(set(ours) | set(broker))
        if not fee_dust_equal(adapter, symbol, ours.get(symbol, Decimal(0)),
                              broker.get(symbol, Decimal(0)))
    ]
    rows = [
        Reconciliation(broker_link_id=link_id, portfolio_id=pid, diffs=diffs, ok=not diffs)
        for pid in portfolio_ids
    ]
    async with sessionmaker() as session:
        if diffs:
            previous = await _previous_diffs(session, link_id, portfolio_ids)
            reachable = await portfolios_with_agents(session, portfolio_ids)
            noun = "differenza" if len(diffs) == 1 else "differenze"
            for row in rows:
                if row.portfolio_id not in reachable:  # the owner's alone: no agent sees it
                    if previous.get(row.portfolio_id) != diffs:
                        await enqueue(session, source="system", category="system",
                                      level="warning",
                                      text=await _owner_diff_text(session, row.portfolio_id,
                                                                  diffs))
                    continue
                # only a change is sent to Telegram; a repeat is recorded quietly
                record_activity(session, ActivityDraft(
                    "broker.reconciliation_diff", ActivityLevel.warning,
                    notify and previous.get(row.portfolio_id) != diffs,
                    f"Riconciliazione broker con {len(diffs)} {noun}",
                    {"broker_link_id": link_id, "diffs": diffs},
                ), portfolio_id=row.portfolio_id, agent_id=None)
        session.add_all(rows)
        await session.commit()
    return rows


OWNER_DIFFS_SHOWN = 5


async def _owner_diff_text(session: AsyncSession, portfolio_id: UUID, diffs: list[dict]) -> str:
    name = await session.scalar(select(Portfolio.name).where(Portfolio.id == portfolio_id))
    noun = "differenza" if len(diffs) == 1 else "differenze"
    shown = "; ".join(f"{d['symbol']}: nostra {d['ours']}, broker {d['broker']}"
                      for d in diffs[:OWNER_DIFFS_SHOWN])
    more = f" e altre {len(diffs) - OWNER_DIFFS_SHOWN}" if len(diffs) > OWNER_DIFFS_SHOWN else ""
    return (f"⚠️ Riconciliazione broker — {name or portfolio_id}: {len(diffs)} {noun} "
            f"({shown}{more}). Nessuna correzione automatica; nessun agente ha accesso a "
            "questo portafoglio: avviso solo a te.")


async def _previous_diffs(
    session: AsyncSession, link_id: UUID, portfolio_ids: list[UUID]
) -> dict[UUID, list]:
    """The diffs of the last reconciliation of each portfolio on the link ([] if ok)."""
    rows = await session.execute(
        select(Reconciliation.portfolio_id, Reconciliation.diffs)
        .where(Reconciliation.broker_link_id == link_id,
               Reconciliation.portfolio_id.in_(portfolio_ids))
        .ext(distinct_on(Reconciliation.portfolio_id))
        .order_by(Reconciliation.portfolio_id, Reconciliation.ts.desc())
    )
    return {pid: diffs or [] for pid, diffs in rows}


async def reconcile_all(sessionmaker: async_sessionmaker[AsyncSession], http) -> int:
    """Reconcile every enabled paper/demo link (scheduler job); the link status records
    the outcome. Real-money links (live_state not null) are left to the LiveMonitor,
    which reconciles them through the live gate. Returns how many reconciliations found
    differences. A difference is recorded in the activity feed but never sent to Telegram
    (`notify=False`): the reconciliation job (`smtrading.reconciliation`) alerts the owner."""
    async with sessionmaker() as session:
        link_ids = list(await session.scalars(
            select(BrokerLink.id)
            .where(BrokerLink.enabled.is_(True), BrokerLink.live_state.is_(None))
            .order_by(BrokerLink.name)
        ))
    mismatched = 0
    for link_id in link_ids:
        try:
            rows = await reconcile(sessionmaker, link_id, http, notify=False)
            status, error = "ok", None
        except Exception as e:  # noqa: BLE001  (any failure goes to the link status)
            log.warning("reconciliation of broker link %s failed: %s", link_id, e)
            rows, status, error = [], "error", str(e)[:500]
        async with sessionmaker() as session:
            link = await session.get(BrokerLink, link_id)
            if link is not None:
                link.status, link.last_error = status, error
                await session.commit()
        bad = [r for r in rows if not r.ok]
        if bad:
            mismatched += len(bad)
            log.warning("broker link %s: positions differ: %s", link_id, bad[0].diffs)
    return mismatched
