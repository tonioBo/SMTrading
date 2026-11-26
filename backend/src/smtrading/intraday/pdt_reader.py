"""The broker's pattern-day-trader state, read with GET only and compared with our own count
(`pdt.link_budget`). Only links with a portfolio in `DAY_SCOPE` are read: a link of campaign
portfolios gets no new broker call. No transaction is open during a network call."""
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import exists, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.brokers.models import BrokerLink
from smtrading.brokers.service import open_adapter
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.instruments.market_hours import local_date
from smtrading.intraday.models import DAY_SCOPE
from smtrading.intraday.pdt import PDT_KINDS, PDT_MARKET, link_budget
from smtrading.jobs.runs import run_job_once
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.reconciliation.service import ReadOnlyHttp
from smtrading.telegram.outbox import enqueue

log = logging.getLogger("smtrading.intraday.pdt_reader")

PDT_READ_INTERVAL_S = 300


def _text(v) -> str | None:
    return None if v is None else str(v)


def _state_from(raw: dict) -> dict:
    """Only the five fields we need, never the account id."""
    count = raw.get("daytrade_count")
    try:
        count = None if count is None else int(count)
    except (TypeError, ValueError):
        count = None
    flag = raw.get("pattern_day_trader")
    return {
        "equity": _text(raw.get("equity")),
        "last_equity": _text(raw.get("last_equity")),
        "multiplier": _text(raw.get("multiplier")),
        "daytrade_count": count,
        "pattern_day_trader": None if flag is None else bool(flag),
    }


async def read_pdt_state(sessionmaker: async_sessionmaker, http, link_id: uuid.UUID) -> dict:
    """One read of the broker account (no open transaction during the call); saves and
    returns `pdt_state` and sets `pdt_state_at`. A read error: PROVIDER_UNAVAILABLE."""
    try:
        async with sessionmaker() as s, open_adapter(
                s, link_id, http, sessionmaker=sessionmaker) as adapter:
            await s.rollback()
            inner = adapter.http
            adapter.http = ReadOnlyHttp(inner)
            try:
                snap = await adapter.get_account()
            finally:
                adapter.http = inner
    except SMTError:
        raise
    except Exception as e:  # noqa: BLE001  (any broker failure; the text may hold secrets)
        raise SMTError(
            ErrorCode.PROVIDER_UNAVAILABLE, "Lettura del conto broker non riuscita",
            {"broker_link_id": str(link_id), "error": type(e).__name__},
        ) from None
    state = _state_from(snap.raw)
    async with sessionmaker() as s:
        await s.execute(update(BrokerLink).where(BrokerLink.id == link_id).values(
            pdt_state=state, pdt_state_at=datetime.now(UTC)))
        await s.commit()
    return state


async def _link_ids(sessionmaker: async_sessionmaker) -> list[uuid.UUID]:
    async with sessionmaker() as s:
        return list((await s.scalars(
            select(BrokerLink.id).where(
                BrokerLink.enabled.is_(True), BrokerLink.kind.in_(PDT_KINDS),
                exists().where(Portfolio.broker_link_id == BrokerLink.id, DAY_SCOPE,
                               Portfolio.status != PortfolioStatus.archived),
            ).order_by(BrokerLink.id))).all())


async def _compare(sessionmaker: async_sessionmaker, link_id: uuid.UUID, state: dict,
                   now: datetime) -> None:
    async with sessionmaker() as s:
        link = await s.get(BrokerLink, link_id)
        if link is None:
            return
        day = local_date(PDT_MARKET, now)
        broker = state["daytrade_count"]
        if broker is not None:
            ours = (await link_budget(s, link_id, now)).used
            if ours != broker:
                log.warning("day-trade count differs from the broker on link %s: ours %s, "
                            "broker %s (conteggio diverso)", link_id, ours, broker)
                if await run_job_once(s, "intraday_pdt_mismatch",
                                      f"{link_id}:{day}:{ours}:{broker}"):
                    await enqueue(
                        s, source="system", category="system", level="warning",
                        text=("Conteggio operazioni di giornata diverso dal broker su "
                              f"{link.name}: SMTrading {ours}, Alpaca {broker}"))
        if state["pattern_day_trader"] is True and await run_job_once(
                s, "intraday_pdt_flag", f"{link_id}:{day}"):
            await enqueue(
                s, source="system", category="system", level="warning",
                text=f"Conto segnato come day trader dal broker su {link.name}")
        await s.commit()


async def refresh_pdt_states(sessionmaker: async_sessionmaker, http, now: datetime) -> int:
    """Read every link in scope; the number of links read. A failure of one link is logged
    and retried next round; it never stops the others."""
    read = 0
    for link_id in await _link_ids(sessionmaker):
        try:
            state = await read_pdt_state(sessionmaker, http, link_id)
        except SMTError as e:
            log.warning("pdt state of link %s not read: %s", link_id,
                        (e.details or {}).get("error", e.code))
            continue
        except Exception:
            log.exception("pdt state of link %s failed", link_id)
            continue
        read += 1
        try:
            await _compare(sessionmaker, link_id, state, now)
        except Exception:
            log.exception("pdt comparison of link %s failed", link_id)
    return read
