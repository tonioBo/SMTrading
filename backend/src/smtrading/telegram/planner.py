"""TelegramPlanner: turns the activity rows marked `telegram='pending'` into outbox
messages (now), digest entries (quiet hours, non-critical) or `skipped`.

- Real money first (plan 8, Ruling R8.4 over R7.1): a `live.*` row is always queued at
  once (outbox category `live`), rendered by `brokers.live.protect.render_live_activity`:
  also while Telegram is disabled or not configured (the outbox keeps it until it is
  ready), unreadable or paused (a `paused` outbox row, never stale-skipped on resume), at
  night (sent at once, silent: the sender's quiet-hours rule) and however old; never
  digested, never skipped. A direct `order.*` row of an order of a live-linked portfolio
  that reached BrokerSync is `skipped`: its `live.order.*` row carries the message. A
  pre-trade rejection (it never reached BrokerSync) keeps its own message.
- Telegram never configured (no token or no group) or disabled: `skipped`.
- Configuration unreadable (the stored token cannot be decrypted, e.g. a new master key)
  while enabled with a group: nothing is planned, the rows stay `pending` until the token is entered again; then they
  are planned in id order. Non-critical rows older than STALE_AFTER are `skipped` then
  (old news); critical rows are always sent.
- Quiet hours are evaluated when the row is planned; the sender evaluates them again
  when it sends (silent or not).
- Fill notifications (`order.filled`, `live.order.filled`/`partial`) add the fill details
  of `telegram.details.fill_lines` (strategy, stop and target, risk at stop in EUR);
  details that cannot be read are left out, never block the message. The FX rates of the
  risk at stop (->EUR, kept FX_TTL_S in memory) never come from the network inside the
  planning transaction: when the locked batch holds fill rows whose currencies are not
  cached (or are older than FX_TTL_S), the transaction is rolled back, the rates are
  fetched with no transaction open and the batch is planned again (once; a rate that
  cannot be fetched only drops the figure). An idle tick runs no FX query at all.
- Each row is planned in its own savepoint: a row that cannot be planned is marked
  `failed` (and logged) instead of blocking the batch forever.
"""

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.activity.models import Activity, ActivityLevel, TelegramState
from smtrading.agents.models import Agent
from smtrading.brokers.live import LIVE_KINDS
from smtrading.brokers.live.protect import render_live_activity
from smtrading.brokers.models import BrokerLink
from smtrading.common.config import get_settings
from smtrading.engine.models import Order
from smtrading.instruments.models import Instrument
from smtrading.portfolios.models import Portfolio
from smtrading.telegram.config import TelegramConfig, is_ready, load_config
from smtrading.telegram.details import FILL_TYPES, fill_lines
from smtrading.telegram.outbox import LIVE_CATEGORY, STALE_AFTER, enqueue
from smtrading.telegram.quiet import is_quiet
from smtrading.telegram.render import Names, render_activity

LIVE_PREFIX = "live."
FX_TTL_S = 600
EUR = "EUR"

BATCH = 100
log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def _names(session: AsyncSession, rows: list[Activity]) -> Names:
    agent_ids = {a.agent_id for a in rows if a.agent_id}
    portfolio_ids = {a.portfolio_id for a in rows if a.portfolio_id}
    names = Names()
    if agent_ids:
        names.agents = dict((await session.execute(
            select(Agent.id, Agent.name).where(Agent.id.in_(agent_ids)))).all())
    if portfolio_ids:
        names.portfolios = dict((await session.execute(
            select(Portfolio.id, Portfolio.name).where(Portfolio.id.in_(portfolio_ids)))
        ).all())
    return names


def _order_uuid(a: Activity) -> uuid.UUID | None:
    value = (a.data or {}).get("order_id") if isinstance(a.data, dict) else None
    try:
        return uuid.UUID(str(value)) if value is not None else None
    except ValueError:
        return None


async def _covered_by_live(session: AsyncSession, rows: list[Activity]) -> set[int]:
    """Ids of the batch's `order.*` rows told by a `live.order.*` row instead: the order
    belongs to a portfolio linked to a live kind and reached BrokerSync (routed, sent or
    acknowledged). A pre-trade rejection never reached it and keeps its own message."""
    wanted = {a.id: oid for a in rows
              if a.portfolio_id and a.type.startswith("order.")
              and (oid := _order_uuid(a)) is not None}
    if not wanted:
        return set()
    synced = set(await session.scalars(
        select(Order.id)
        .join(Portfolio, Portfolio.id == Order.portfolio_id)
        .join(BrokerLink, BrokerLink.id == Portfolio.broker_link_id)
        .where(
            Order.id.in_(set(wanted.values())), BrokerLink.kind.in_(LIVE_KINDS),
            or_(Order.broker_status.is_not(None), Order.broker_sent_at.is_not(None),
                Order.broker_order_id.is_not(None)),
        )
    ))
    return {aid for aid, oid in wanted.items() if oid in synced}


class _CachedFx:
    """The FX rates fetched before the transaction: never a network call."""

    def __init__(self, rates: dict[str, tuple[Decimal, float]]) -> None:
        self._rates = rates

    async def get_fx_rate(self, base: str, quote: str) -> Decimal:
        if base.upper() == quote.upper():
            return Decimal(1)
        hit = self._rates.get(base.upper()) if quote.upper() == EUR else None
        if hit is None:
            raise LookupError(f"{base}->{quote} not cached")
        return hit[0]


class _NeedFx(Exception):
    """Raised inside the planning transaction (rolling it back) to fetch FX rates."""

    def __init__(self, ccys: set[str]) -> None:
        super().__init__(sorted(ccys))
        self.ccys = ccys


class TelegramPlanner:
    def __init__(self, sessionmaker: async_sessionmaker, *,
                 clock: Callable[[], datetime] = _utcnow, tz: str | None = None,
                 interval_s: float = 2.0, qs=None):
        self._sm = sessionmaker
        self._qs = qs  # FX of the risk at stop in EUR; None: shown in the instrument currency
        self._fx: dict[str, tuple[Decimal, float]] = {}  # ccy -> (rate to EUR, monotonic)
        self._cached_fx = _CachedFx(self._fx)
        self._clock = clock
        self._tz = tz or get_settings().tz
        self._interval_s = interval_s

    async def run(self) -> None:
        while True:
            if await self.tick() < BATCH:
                await asyncio.sleep(self._interval_s)

    def _stale_fx(self, ccys: set[str]) -> set[str]:
        now = time.monotonic()
        return {c for c in ccys
                if (hit := self._fx.get(c)) is None or now - hit[1] >= FX_TTL_S}

    async def _fill_currencies(self, session: AsyncSession, rows: list[Activity]) -> set[str]:
        """The non-EUR currencies of the batch's fill rows (the row's `currency` and its
        order's instrument currency): read inside the transaction, no network."""
        fills = [a for a in rows if a.type in FILL_TYPES]
        if not fills:
            return set()
        ccys = {str(c) for a in fills if isinstance(a.data, dict)
                and (c := a.data.get("currency"))}
        order_ids = {oid for a in fills if (oid := _order_uuid(a)) is not None}
        if order_ids:
            ccys |= set(await session.scalars(
                select(Instrument.currency)
                .join(Order, Order.symbol == Instrument.symbol)
                .where(Order.id.in_(order_ids))))
        return {c.upper() for c in ccys if c and c.upper() != EUR}

    async def _warm_fx(self, ccys: set[str]) -> None:
        """Fetches (no transaction open) the ->EUR rates of `ccys`."""
        now = time.monotonic()
        for ccy in sorted(ccys):
            try:
                self._fx[ccy] = (await self._qs.get_fx_rate(ccy, EUR), now)
            except Exception as e:  # noqa: BLE001 - a missing rate only drops the figure
                log.info("telegram planner: no %s->EUR rate (%s)", ccy, e)

    async def tick(self) -> int:
        """Plans up to BATCH pending rows (oldest first) in one transaction; when the batch
        needs FX rates that are not cached, fetches them outside it and plans again."""
        try:
            return await self._tick(check_fx=self._qs is not None)
        except _NeedFx as need:  # rolled back: nothing planned, no lock held
            await self._warm_fx(need.ccys)
        return await self._tick(check_fx=False)

    async def _tick(self, *, check_fx: bool) -> int:
        async with self._sm() as session, session.begin():
            cfg = await load_config(session)
            # disabled (or no group) wins over unreadable: skip, never send weeks later
            waiting = cfg.unreadable and cfg.enabled and cfg.chat_id
            stmt = select(Activity).where(Activity.telegram == TelegramState.pending)
            if waiting:  # only the real-money rows go on (to the outbox, where they wait)
                stmt = stmt.where(Activity.type.startswith(LIVE_PREFIX, autoescape=True))
            rows = list(await session.scalars(
                stmt.order_by(Activity.id).limit(BATCH).with_for_update(skip_locked=True)
            ))
            if not rows:
                return 0
            if check_fx and (missing := self._stale_fx(
                    await self._fill_currencies(session, rows))):
                raise _NeedFx(missing)
            ready = is_ready(cfg)
            quiet = is_quiet(self._clock(), self._tz)
            # ts comes from the database clock: compare it with the same clock
            stale_before = await session.scalar(select(func.now())) - STALE_AFTER
            names = await _names(session, rows)
            covered = await _covered_by_live(session, rows)
            for a in rows:
                activity_id = a.id  # read before a rollback can expire the row
                try:
                    async with session.begin_nested():
                        if a.type.startswith(LIVE_PREFIX):
                            await self._plan_live(session, cfg, a, names)
                        elif not ready:  # waiting (unreadable) selected live rows only
                            a.telegram = TelegramState.skipped
                        elif a.id in covered:
                            a.telegram = TelegramState.skipped  # its live.order.* row says it
                        else:
                            await self._plan(session, cfg, a, names, quiet, stale_before)
                except Exception:
                    log.exception("telegram planner: activity %s cannot be planned",
                                  activity_id)
                    a.telegram = TelegramState.failed
            return len(rows)

    async def _fill_lines(self, session: AsyncSession, a: Activity) -> list[str]:
        if a.type not in FILL_TYPES:
            return []
        activity_id = a.id  # read before a rollback can expire the row
        try:
            async with session.begin_nested():
                return await fill_lines(
                    session, a, qs=self._cached_fx if self._qs is not None else None)
        except Exception:
            log.exception("telegram planner: fill details of activity %s not read",
                          activity_id)
            return []

    async def _plan_live(self, session: AsyncSession, cfg: TelegramConfig, a: Activity,
                         names: Names) -> None:
        msg = await enqueue(session, source="system", category=LIVE_CATEGORY, level=a.level,
                            text=render_live_activity(a, names, self._tz,
                                                     await self._fill_lines(session, a)),
                            cfg=cfg)
        a.telegram = TelegramState.queued
        a.outbox_id = msg.id

    async def _plan(self, session: AsyncSession, cfg: TelegramConfig, a: Activity, names: Names,
                    quiet: bool, stale_before: datetime) -> None:
        if a.level != ActivityLevel.critical and a.ts is not None and a.ts < stale_before:
            a.telegram = TelegramState.skipped
        elif quiet and a.level != ActivityLevel.critical:
            a.telegram = TelegramState.digest
        else:
            msg = await enqueue(session, source="system", category="event", level=a.level,
                                text=render_activity(a, names, self._tz,
                                                     await self._fill_lines(session, a)),
                                cfg=cfg)
            a.telegram = TelegramState.queued
            a.outbox_id = msg.id
